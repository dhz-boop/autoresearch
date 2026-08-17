"""阶段 1 单元测试补充：nodes.py 结构化输出（FC 优先 + 兜底）与反思闭环。

不依赖真实 LLM：用 monkeypatch 替换 build_llm / extract_market_data / search_web。
"""
import pytest
from langchain_core.exceptions import OutputParserException
from langgraph.types import Send
from pydantic import ValidationError

import nodes
from config import get_settings
from graph import continue_to_researchers, route_after_reflection
from models import DataReflection, Outline, SubTask, SubTaskList


# ---------------------------------------------------------------------------
# DataReflection 模型
# ---------------------------------------------------------------------------
def test_datareflection_basic():
    r = DataReflection(
        has_gaps=True,
        summary="数据覆盖不足",
        missing_questions=["市场规模多少?"],
        gap_queries=["2026 中国咖啡机 市场规模"],
    )
    assert r.has_gaps is True
    assert r.missing_questions == ["市场规模多少?"]
    assert r.gap_queries[0] == "2026 中国咖啡机 市场规模"

    ok = DataReflection(has_gaps=False, summary="覆盖充分", missing_questions=[], gap_queries=[])
    assert ok.has_gaps is False


def test_datareflection_rejects_object_array():
    """字符串数组字段收到对象数组应校验失败（与 Outline.sections 同约束）。"""
    with pytest.raises(ValidationError):
        DataReflection(
            has_gaps=True,
            summary="x",
            missing_questions=["q1"],
            gap_queries=[{"title": "查询"}],
        )


# ---------------------------------------------------------------------------
# 容错管线纯函数（兜底路线仍然使用）
# ---------------------------------------------------------------------------
def test_extract_json():
    # 剥 ```json 代码块
    assert nodes._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    # 容忍前后杂讯：取第一个 { 到最后一个 }
    assert nodes._extract_json('说明：结果如下 {"a": 1} 完毕') == {"a": 1}
    with pytest.raises(ValueError):
        nodes._extract_json("这不是 JSON")


def test_coerce_str_list():
    items = ["行业概况", {"title": "竞争格局"}, {"name": "市场规模"}, 123]
    assert nodes._coerce_str_list(items) == ["行业概况", "竞争格局", "市场规模", "123"]
    assert nodes._coerce_str_list(None) == []


def test_coerce_model_nested():
    """嵌套模型（SubTaskList → SubTask.keywords）字段级清洗后成功构造。"""
    data = {"tasks": [{"id": "t1", "description": "调研竞品", "keywords": [{"title": "k1"}, "k2"]}]}
    result = nodes._coerce_model(SubTaskList, data)
    assert result.tasks[0].keywords == ["k1", "k2"]


# ---------------------------------------------------------------------------
# _structured_invoke：FC 优先 + 兜底回退
# ---------------------------------------------------------------------------
class _FakeStructured:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def invoke(self, prompt):
        if self.error:
            raise self.error
        return self.result


class _FakeBound:
    """模拟 bind(response_format={"type": "json_object"}) 后的 runnable。"""

    def __init__(self, text):
        self.text = text

    def invoke(self, prompt):
        class _Resp:
            content = ""

        resp = _Resp()
        resp.content = self.text
        return resp


class _FakeLLM:
    def __init__(self, fc_result=None, fc_error=None, fallback_text=""):
        self.fc_result = fc_result
        self.fc_error = fc_error
        self.fallback_text = fallback_text
        self.bind_called = False

    def with_structured_output(self, model_cls, method="function_calling"):
        assert method == "function_calling", "应优先走 function_calling"
        return _FakeStructured(self.fc_result, self.fc_error)

    def bind(self, **kwargs):
        self.bind_called = True
        assert kwargs.get("response_format") == {"type": "json_object"}
        return _FakeBound(self.fallback_text)


def _patch_llm(monkeypatch, **kwargs):
    fake = _FakeLLM(**kwargs)
    monkeypatch.setattr(nodes, "build_llm", lambda: fake)
    return fake


def test_structured_invoke_fc_success(monkeypatch):
    """FC 主路线成功：直接返回模型实例，不落兜底。"""
    expected = Outline(sections=["行业概况"], key_questions=["市场规模多少?"])
    fake = _patch_llm(monkeypatch, fc_result=expected)
    result = nodes._structured_invoke(Outline, "生成大纲")
    assert result is expected
    assert fake.bind_called is False, "FC 成功时不应触发兜底 bind"


def test_structured_invoke_fc_error_falls_back(monkeypatch):
    """FC 抛异常（如 OutputParserException）→ 兜底管线清洗对象数组后成功。"""
    fallback = '{"sections": [{"title": "行业概况"}, "竞争格局"], "key_questions": ["市场规模多少?"]}'
    fake = _patch_llm(monkeypatch, fc_error=OutputParserException("bad"), fallback_text=fallback)
    result = nodes._structured_invoke(Outline, "生成大纲")
    assert result.sections == ["行业概况", "竞争格局"], "兜底应清洗对象数组字段"
    assert fake.bind_called is True


def test_structured_invoke_fc_none_falls_back(monkeypatch):
    """FC 返回 None（模型未产出 tool_call，parser 静默返回 None）→ 兜底。"""
    fallback = '{"sections": ["a"], "key_questions": ["b"]}'
    fake = _patch_llm(monkeypatch, fc_result=None, fallback_text=fallback)
    result = nodes._structured_invoke(Outline, "生成大纲")
    assert result.sections == ["a"]
    assert fake.bind_called is True


# ---------------------------------------------------------------------------
# analyst：auto_feedback 消费与清除
# ---------------------------------------------------------------------------
def _outline():
    return Outline(sections=["行业概况"], key_questions=["市场规模多少?"])


def test_analyst_consumes_auto_feedback(monkeypatch):
    captured = {}

    def fake_extract(topic, outline, search_results, feedback=None):
        captured["feedback"] = feedback
        return {"market_size": 100}

    monkeypatch.setattr(nodes, "extract_market_data", fake_extract)
    out = nodes.analyst(
        {
            "topic": "咖啡机",
            "outline": _outline(),
            "search_results": [{"title": "t"}],
            "extracted_data": {"old": 1},
            "auto_feedback": "缺口一；缺口二",
        }
    )
    assert captured["feedback"] == "缺口一；缺口二", "auto_feedback 应传入提取环节"
    assert out["auto_feedback"] is None, "消费后应清除 auto_feedback"
    assert out["draft_report"] is None, "重新提取应清空草稿强制重写"


def test_analyst_skips_without_feedback(monkeypatch):
    """已有数据且无任何反馈 → 直接 return {}，不调用 LLM。"""
    called = False

    def fake_extract(topic, outline, search_results, feedback=None):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(nodes, "extract_market_data", fake_extract)
    out = nodes.analyst(
        {
            "topic": "咖啡机",
            "outline": _outline(),
            "search_results": [{"title": "t"}],
            "extracted_data": {"market_size": 100},
        }
    )
    assert out == {}, "无反馈时应跳过重算"
    assert called is False


def test_analyst_human_feedback_wins(monkeypatch):
    """human_feedback 与 auto_feedback 并存时 human 优先，且 auto 仍被清除。"""
    captured = {}

    def fake_extract(topic, outline, search_results, feedback=None):
        captured["feedback"] = feedback
        return {}

    monkeypatch.setattr(nodes, "extract_market_data", fake_extract)
    out = nodes.analyst(
        {
            "topic": "咖啡机",
            "outline": _outline(),
            "search_results": [{"title": "t"}],
            "extracted_data": {"old": 1},
            "human_feedback": "数据不准确",
            "auto_feedback": "缺口一",
        }
    )
    assert captured["feedback"] == "数据不准确"
    assert out["auto_feedback"] is None


# ---------------------------------------------------------------------------
# reflector / gap_researcher 节点
# ---------------------------------------------------------------------------
def test_reflector_writes_state(monkeypatch):
    fake_ref = DataReflection(
        has_gaps=True, summary="缺口", missing_questions=["q1", "q2"], gap_queries=["补搜词"]
    )
    monkeypatch.setattr(nodes, "generate_reflection", lambda topic, outline, data: fake_ref)
    out = nodes.reflector(
        {
            "topic": "咖啡机",
            "outline": _outline(),
            "extracted_data": {"market_size": 100},
            "reflection_count": 1,
        }
    )
    assert out["reflection"] is fake_ref
    assert out["reflection_count"] == 2
    assert out["data_approved"] is False
    assert out["auto_feedback"] == "q1；q2"
    assert out["reflection_log"] == [
        {"round": 2, "has_gaps": True, "summary": "缺口", "gap_queries": ["补搜词"]}
    ]


def test_reflector_pass(monkeypatch):
    ok = DataReflection(has_gaps=False, summary="覆盖充分", missing_questions=[], gap_queries=[])
    monkeypatch.setattr(nodes, "generate_reflection", lambda topic, outline, data: ok)
    out = nodes.reflector(
        {"topic": "咖啡机", "outline": _outline(), "extracted_data": {"market_size": 100}}
    )
    assert out["data_approved"] is True
    assert out["auto_feedback"] is None


def test_reflector_failure_fail_open(monkeypatch):
    """generate_reflection 异常 → reflection=None + error_log，路由据此进 writer。"""

    def boom(topic, outline, data):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(nodes, "generate_reflection", boom)
    out = nodes.reflector({"topic": "咖啡机", "outline": _outline(), "extracted_data": {}})
    assert out["reflection"] is None
    assert out["error_log"] == ["reflector: LLM 挂了"]
    assert out["auto_feedback"] is None


class _FakeTool:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def invoke(self, kwargs):
        self.calls.append(kwargs)
        return self.results


def test_gap_researcher(monkeypatch):
    fake = _FakeTool([{"title": "补搜结果"}])
    monkeypatch.setattr(nodes, "search_web", fake)
    out = nodes.gap_researcher({"gap_query": "2026 咖啡机 市场"})
    assert fake.calls == [{"query": "2026 咖啡机 市场", "max_results": get_settings().tavily_max_results}]
    assert out == {"search_results": [{"title": "补搜结果"}]}


def test_gap_researcher_missing_query(monkeypatch):
    fake = _FakeTool([])
    monkeypatch.setattr(nodes, "search_web", fake)
    out = nodes.gap_researcher({})
    assert out["search_results"] == []
    assert out["error_log"] == ["gap_researcher: 缺少缺口查询"]
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 路由函数
# ---------------------------------------------------------------------------
def _reflection(has_gaps, queries=None):
    return DataReflection(
        has_gaps=has_gaps,
        summary="s",
        missing_questions=["q"] if has_gaps else [],
        gap_queries=queries if queries is not None else (["补搜词"] if has_gaps else []),
    )


def test_route_after_reflection_sends_gap_researchers():
    r = route_after_reflection(
        {"reflection": _reflection(True), "reflection_count": 1}
    )
    assert isinstance(r, list) and len(r) == 1
    assert r[0].node == "gap_researcher"
    assert r[0].arg == {"gap_query": "补搜词"}


def test_route_after_reflection_reaches_cap():
    assert route_after_reflection(
        {"reflection": _reflection(True), "reflection_count": 2}
    ) == "writer"


def test_route_after_reflection_no_gaps():
    assert route_after_reflection(
        {"reflection": _reflection(False), "reflection_count": 0}
    ) == "writer"


def test_route_after_reflection_empty_queries():
    """缺口但查询为空 → writer（避免空 Send 列表，BUGS #13）。"""
    assert route_after_reflection(
        {"reflection": _reflection(True, queries=[]), "reflection_count": 0}
    ) == "writer"


def test_route_after_reflection_none():
    """reflector 异常（reflection=None）→ fail-open 直接进 writer。"""
    assert route_after_reflection({"reflection": None, "reflection_count": 0}) == "writer"


def test_continue_to_researchers():
    tasks = [SubTask(id="t1", description="d1", keywords=["k1"]),
             SubTask(id="t2", description="d2", keywords=["k2"])]
    r = continue_to_researchers({"sub_tasks": tasks})
    assert isinstance(r, list) and len(r) == 2
    assert all(s.node == "researcher" for s in r)
    assert r[1].arg["sub_task"] is tasks[1]

    # 空任务 → 回 supervisor_planner 重新规划（避免空 Send）
    assert continue_to_researchers({"sub_tasks": []}) == "supervisor_planner"
