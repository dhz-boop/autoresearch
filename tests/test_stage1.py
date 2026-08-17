"""阶段 1 单元测试：配置加载 / Pydantic 模型 / 状态 reducer / 工具封装。

不依赖真实 LLM 或外部 API（tavily 无 key 时走 mock），可随时运行。
"""
import operator

from config import get_settings
from models import Outline, SearchResult, SubTask
from state import ResearchState

import tools


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_settings_loaded_from_env():
    """.env 应正确加载 DeepSeek 配置。"""
    s = get_settings()
    assert s.llm_provider == "deepseek"
    assert s.deepseek_model == "deepseek-v4-flash"
    assert s.deepseek_api_key.startswith("sk-")
    assert s.siliconflow_api_key.startswith("sk-")


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------
def test_pydantic_models_basic():
    o = Outline(sections=["行业概况", "竞争格局"], key_questions=["市场规模多少?"])
    assert len(o.sections) == 2
    assert o.key_questions[0] == "市场规模多少?"

    st = SubTask(id="t1", description="调研竞品", keywords=["咖啡机", "竞品"])
    assert st.id == "t1"

    sr = SearchResult(title="t", url="u", content="c", score=0.8, query="q")
    assert sr.score == 0.8
    # 可 JSON 序列化
    assert sr.model_dump()["title"] == "t"


# ---------------------------------------------------------------------------
# ResearchState 与 reducer 合并
# ---------------------------------------------------------------------------
def _empty_state() -> ResearchState:
    return {
        "topic": "咖啡机",
        "plan_approved": False,
        "sub_tasks": [],
        "search_results": [],
        "extracted_data": {},
        "draft_approved": False,
        "error_log": [],
        "sub_task": None,
        "outline": None,
        "draft_report": None,
        "final_report": None,
        "human_feedback": None,
    }


def test_search_results_reducer_merges():
    """operator.add reducer 应自动合并并行分支的 search_results。"""
    st = _empty_state()
    merged = operator.add(st["search_results"], [{"title": "a", "query": "q"}])
    merged = operator.add(merged, [{"title": "b", "query": "q"}])
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def test_get_current_datetime():
    """get_current_datetime 应返回含当前年份的日期时间字符串。"""
    import datetime

    out = tools.get_current_datetime.invoke({})
    now = datetime.datetime.now()
    assert f"{now.year}年" in out
    assert "月" in out and "日" in out
    assert ":" in out  # 含时分


def test_tavily_search_mock_when_no_key(monkeypatch):
    """无 TAVILY_API_KEY 时应返回模拟结果（保证流程可本地跑通）。

    注意：开发环境可能已配置真实 key，这里用 monkeypatch 强制无 key 验证 mock 路径。
    """
    monkeypatch.setattr(get_settings(), "tavily_api_key", "")
    res = tools.tavily_search.invoke({"query": "咖啡机市场", "max_results": 3})
    assert len(res) == 3
    assert res[0]["query"] == "咖啡机市场"
    assert res[0]["title"].startswith("[MOCK]")


def test_python_repl_basic_exec():
    out = tools.python_repl.invoke({"code": "nums=[1,2,3,4,5]\nprint('sum=', sum(nums))"})
    assert "sum= 15" in out


def test_python_repl_blocks_import():
    """AST 校验 + 受限内置应拦截 import 等危险操作。"""
    out = tools.python_repl.invoke({"code": "import os\nprint(os.getcwd())"})
    assert "REPL 禁止 import 语句" in out


def test_python_repl_blocks_dunder_escape():
    """AST 校验应阻断 class 链沙箱逃逸（dunder 属性访问）。"""
    code = (
        "w = [c for c in ().__class__.__bases__[0].__subclasses__() "
        "if c.__name__ == 'catch_warnings']"
    )
    out = tools.python_repl.invoke({"code": code})
    assert "REPL 禁止访问 dunder 属性" in out


def test_python_repl_reports_error():
    out = tools.python_repl.invoke({"code": "print(1/0)"})
    assert "ZeroDivisionError" in out
