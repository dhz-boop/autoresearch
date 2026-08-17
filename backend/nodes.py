"""节点实现：LangGraph 图中的全部节点函数。

约定：
- 每个节点输入 state: ResearchState，返回 dict（部分状态更新）；
- 所有节点用 try/except 包裹，异常记入 state["error_log"]，不中断整图；
- 含 interrupt() 的节点必须将 GraphInterrupt 重新抛出（interrupt 机制依赖异常传播，
  普通 except 会吞掉中断，导致图无法恢复）；
- 结构化输出走 function_calling 优先（with_structured_output(method=
  "function_calling")，langchain-openai 自动 tool_choice 强制模型调用虚拟工具），
  失败回退自研容错管线（json_object + JSON 提取 + 字段级清洗 + Pydantic 校验 +
  重试）。容错管线是永久兜底：json_object 模式在带修改意见的长 prompt 下，
  会把 sections 等「字符串数组」字段输出成对象数组（见 BUGS.md #21），
  且模型可能不产出 tool_call（parser 静默返回 None）。
- 反思闭环（reflection loop）：analyst 提取数据后由 reflector 质检，
  有缺口则 gap_researcher 定向补搜并回 analyst 重新提取，循环上限
  _MAX_REFLECTIONS（graph.py），超限/异常 fail-open 直接进 writer。
"""
import json
import re
from typing import Dict, List, Optional, Type, TypeVar

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from config import build_llm, get_settings
from models import DataReflection, Outline, SubTask, SubTaskList
from state import ResearchState
from tools import get_current_datetime, search_web

M = TypeVar("M", bound=BaseModel)

# 搜索结果摘要传给 LLM 的最大条数（控制 prompt 长度）
_MAX_RESULTS_FOR_LLM = 8


def _time_hint() -> str:
    """注入到各 LLM prompt 的当前时间提示。

    每次生成都实时获取本地时间，让模型明确知道「现在」是哪一天，
    从而在检索与撰写时优先采用最新信息、避免引用过时数据。
    """
    return f"当前时间：{get_current_datetime.invoke({})}（请据此判断信息时效，优先采用最近的数据）"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _as_text(content: object) -> str:
    """把 LLM 响应的 content 统一转成文本（兼容部分模型返回 list）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )
    return str(content)


def _coerce_str_list(items: object) -> List[str]:
    """把字段值清洗为字符串列表。

    真实 DeepSeek 在带修改意见的长 prompt 下，常把 sections/key_questions 等
    「字符串数组」输出成对象数组（如 [{"title": ...}, ...]），这里把 dict 元素
    取其常见文本字段转回字符串。
    """
    out: List[str] = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            # 对象元素 → 优先取常见文本字段，再取任意一个非空字符串值
            text = (
                it.get("title")
                or it.get("name")
                or it.get("description")
                or it.get("content")
                or it.get("text")
                or it.get("label")
                or it.get("tag")
            )
            if text is None:
                for v in it.values():
                    if isinstance(v, str) and v:
                        text = v
                        break
            out.append(str(text) if text else str(it))
        else:
            out.append(str(it))
    return out


def _coerce_model(model_cls: Type[BaseModel], data: dict):
    """容错构造 Pydantic 模型：直接校验失败时，对 List[str] 字段做元素清洗后重试。

    支持嵌套的模型列表（如 SubTaskList.tasks）。
    """
    try:
        return model_cls.model_validate(data)
    except ValidationError:
        pass
    cleaned: dict = {}
    for name, fdef in model_cls.model_fields.items():
        if name not in data:
            continue
        val = data[name]
        origin = getattr(fdef.annotation, "__origin__", None)
        args = getattr(fdef.annotation, "__args__", ())
        if origin is list and args:
            item = args[0]
            if item is str:
                cleaned[name] = _coerce_str_list(val)
            elif isinstance(item, type) and issubclass(item, BaseModel):
                cleaned[name] = [
                    _coerce_model(item, v) if isinstance(v, dict) else v for v in val
                ]
            else:
                cleaned[name] = val
        else:
            cleaned[name] = val
    return model_cls.model_validate(cleaned)


def _structured_invoke(model_cls: Type[M], prompt: str) -> M:
    """调用 LLM 生成结构化对象（function_calling 优先，json_object 容错兜底）。

    主路线：with_structured_output(model_cls, method="function_calling")。
      langchain-openai 1.4.2 会：自动 tool_choice 强制模型调用该「虚拟工具」、
      parallel_tool_calls=False、strict 默认不传（DeepSeek 兼容）。
    兜底路线：原 json_object + _extract_json + _coerce_model 管线（BUGS.md #21：
      长 prompt 下「字符串数组」字段易被输出成对象数组，需字段级清洗）。
    回退触发：OutputParserException / ValidationError / ValueError / 返回 None
      （模型未产出 tool_call 时 parser 静默返回 None）/ 任何其他异常。

    注：prompt 保留「JSON」字样是兜底路线的硬性要求（DeepSeek 的
    json_object response_format 要求 prompt 中出现 "json"）。
    """
    if "json" not in prompt.lower():
        prompt += "\n请以 JSON 格式输出结果。"
    try:
        structured = build_llm().with_structured_output(
            model_cls, method="function_calling"
        )
        parsed = structured.invoke(prompt)
        if parsed is not None:
            return parsed
    except Exception:  # noqa: BLE001 - 任何失败均回退兜底管线
        pass
    llm = build_llm().bind(response_format={"type": "json_object"})
    last_err: Optional[Exception] = None
    for _ in range(3):
        try:
            resp = llm.invoke(prompt)
            data = _extract_json(_as_text(resp.content))
            return _coerce_model(model_cls, data)
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
    raise ValueError(f"LLM 结构化输出解析失败（已重试 3 次）：{last_err}")


def _extract_json(text: str) -> dict:
    """从 LLM 输出文本中解析 JSON 字典（容错：剥离 ```json 代码块包裹）。

    先尝试整体解析；失败后再取「第一个 { 到最后一个 }」之间的子串解析；
    仍失败则抛出带原文片段的 ValueError，便于定位。
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.DOTALL)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    # 若输出包含前后杂讯，取第一个 { 到最后一个 } 之间的内容
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            pass
    raise ValueError(f"无法从 LLM 输出解析 JSON：{text[:200]!r}")


# ---------------------------------------------------------------------------
# 各节点的 LLM 生成逻辑（拆成纯函数，便于单独复用与测试）
# ---------------------------------------------------------------------------
def generate_outline(topic: str, feedback: Optional[str] = None) -> Outline:
    """supervisor_planner 使用：根据主题（及可选修改意见）生成调研大纲。"""
    prompt = (
        "你是资深市场调研专家。请为以下调研主题生成结构化调研大纲。\n"
        f"{_time_hint()}\n"
        f"调研主题：{topic}\n"
        + (f"用户的修改意见（请据此调整大纲）：{feedback}\n" if feedback else "")
        + "请以 JSON 格式输出，包含 sections（报告章节列表）和 key_questions（关键问题列表）两个字段。"
          "字段说明：sections 和 key_questions 均为字符串数组，每个元素是纯文本"
          "（sections 为章节标题，key_questions 为需要解答的问题），不要输出对象。"
    )
    return _structured_invoke(Outline, prompt)


def generate_subtasks(
    topic: str, outline: Outline, feedback: Optional[str] = None
) -> List[SubTask]:
    """task_decomposer 使用：基于已确认大纲拆解并行搜索子任务。"""
    prompt = (
        "你是调研任务规划师。请基于已确认的调研大纲，将调研任务拆解为若干个可并行执行的搜索子任务。\n"
        f"{_time_hint()}\n"
        f"调研主题：{topic}\n"
        f"报告章节：{'；'.join(outline.sections)}\n"
        f"关键问题：{'；'.join(outline.key_questions)}\n"
        + (f"用户的修改意见（请据此调整子任务划分）：{feedback}\n" if feedback else "")
        + "请以 JSON 格式输出 tasks 数组，每项包含 id（字符串）、description（要调研的问题）、"
          "keywords（用于搜索引擎的关键词列表，2-4 个）。注意 id、description 为字符串，"
          "keywords 为字符串数组，不要输出对象。"
    )
    result = _structured_invoke(SubTaskList, prompt)
    return result.tasks


def extract_market_data(
    topic: str,
    outline: Outline,
    search_results: List[dict],
    feedback: Optional[str] = None,
) -> dict:
    """analyst 使用：从聚合搜索结果中提取结构化市场数据。"""
    results_text = "\n".join(
        f"- {item['title']}: {item['content']}"
        for item in search_results[:_MAX_RESULTS_FOR_LLM]
    )
    prompt = (
        "你是市场数据分析师。请基于以下互联网搜索结果，提取关键市场数据。\n"
        f"{_time_hint()}\n"
        f"调研主题：{topic}\n"
        f"报告章节：{'；'.join(outline.sections)}\n"
        + (f"数据补充/修正要求（请据此重点补充/修正数据）：{feedback}\n" if feedback else "")
        + f"搜索结果（{len(search_results)} 条）：\n{results_text}\n"
        + "请以 JSON 格式输出结构化数据对象，尽量覆盖：市场规模估计、增长率、主要玩家与竞争格局、"
          "消费者画像与趋势、市场机会与风险。无法确定的项请标注为 null 而不是编造。"
    )
    llm = build_llm().bind(response_format={"type": "json_object"})
    resp = llm.invoke(prompt)
    return _extract_json(_as_text(resp.content))


def generate_reflection(
    topic: str, outline: Outline, extracted_data: dict
) -> DataReflection:
    """reflector 使用：评估已提取数据是否足以支撑大纲章节与关键问题。

    输出 DataReflection（has_gaps / missing_questions / gap_queries / summary），
    走 function_calling 优先的结构化输出（_structured_invoke）。
    """
    prompt = (
        "你是资深市场调研质检专家。请评估已提取的市场数据是否足以支撑"
        "调研大纲的每个章节与关键问题。\n"
        f"{_time_hint()}\n"
        f"调研主题：{topic}\n"
        f"报告章节：{'；'.join(outline.sections)}\n"
        f"关键问题：{'；'.join(outline.key_questions)}\n"
        "已提取数据（JSON）：\n"
        + json.dumps(extracted_data, ensure_ascii=False, indent=2)[:4000]
        + "\n请以 JSON 格式输出评估结果，包含四个字段：\n"
          "1. has_gaps（布尔）：任一章节/关键问题缺少支撑数据、数据为 null、"
          "或数据明显过时，判定为 true；否则 false。\n"
          "2. missing_questions（字符串数组）：未被数据覆盖或数据不充分的关键问题，"
          "无缺口时输出空数组。\n"
          "3. gap_queries（字符串数组）：为每个缺口拟 1 个面向搜索引擎的中文检索词"
          "（如「2026 中国咖啡机 市场规模」），1-3 个即可，无缺口时输出空数组。\n"
          "4. summary（字符串）：用 1-2 句中文概述数据质量结论。\n"
          "严格基于数据事实判断：不要为了通过而放低标准，"
          "也不要因个别细节缺失而过度检索。"
    )
    return _structured_invoke(DataReflection, prompt)


def generate_draft(
    topic: str,
    outline: Outline,
    extracted_data: dict,
    feedback: Optional[str] = None,
) -> str:
    """writer 使用：根据大纲与提炼数据撰写 Markdown 报告初稿。"""
    prompt = (
        "你是资深行业报告撰写人。请基于以下调研资料，撰写一份结构完整的 Markdown 市场调研报告。\n"
        f"{_time_hint()}\n"
        f"调研主题：{topic}\n"
        f"报告章节：\n" + "\n".join(f"- {s}" for s in outline.sections) + "\n"
        + (f"用户的修改意见（请据此调整报告）：{feedback}\n" if feedback else "")
        + "关键数据（JSON）：\n"
        + json.dumps(extracted_data, ensure_ascii=False, indent=2)[:6000]
        + "\n报告要求：使用 Markdown 标题/列表/表格；每个章节与大纲对应；"
          "涉及具体数字时注明数据来源或标注估算；语言专业、结构清晰。"
    )
    return _as_text(build_llm().invoke(prompt).content)


def finalize_report(
    topic: str,
    draft_report: str,
    extracted_data: dict,
    feedback: Optional[str] = None,
) -> str:
    """finalizer 使用：按最终反馈整合并输出定稿报告。"""
    if not feedback:
        return draft_report
    prompt = (
        "以下是已完成的市场调研报告草稿，请根据最终的修改意见进行整合修订，输出最终 Markdown 报告。\n"
        f"{_time_hint()}\n"
        f"调研主题：{topic}\n"
        f"修改意见：{feedback}\n"
        "报告草稿：\n" + draft_report
    )
    return _as_text(build_llm().invoke(prompt).content)


# ---------------------------------------------------------------------------
# 图节点
# ---------------------------------------------------------------------------
def supervisor_planner(state: ResearchState) -> dict:
    """节点 1：生成大纲 → interrupt 请求人工确认。

    interrupt 的 resume 值（Command(resume=...)）结构：
        {"approved": bool, "feedback": Optional[str]}
    - approved=True  → 大纲通过，路由到 task_decomposer；
    - approved=False → 携带修改意见，条件边路由回本节点重新规划。
    """
    try:
        feedback = state.get("human_feedback")
        if feedback:
            # 用户此前提出了修改意见 → 基于反馈重新生成大纲
            outline = generate_outline(state["topic"], feedback)
            return_state = {"human_feedback": None}  # 本次反馈已消费
        elif state.get("outline") is not None:
            # 恢复重跑：state 中已有人工确认的大纲（由 feedback 端点 update_state 写回）
            outline = state["outline"]
            return_state = {}
        else:
            # 首次运行：根据主题生成大纲
            outline = generate_outline(state["topic"])
            return_state = {}

        # 暂停图，等待前端确认/修改大纲
        resp = interrupt({"type": "plan", "outline": outline.model_dump()})
        approved = bool(resp.get("approved"))

        return {
            **return_state,
            "outline": outline,
            "plan_approved": approved,
            "human_feedback": None if approved else resp.get("feedback"),
        }
    except GraphInterrupt:
        raise  # interrupt 机制：必须重新抛出
    except Exception as e:  # noqa: BLE001
        return {"error_log": [f"supervisor_planner: {e}"]}


def task_decomposer(state: ResearchState) -> dict:
    """节点 2：基于已确认大纲，将调研拆解为多个并行搜索子任务。"""
    try:
        outline = state.get("outline")
        if outline is None:
            return {"error_log": ["task_decomposer: 缺少大纲，无法拆解子任务"], "sub_tasks": []}
        tasks = generate_subtasks(state["topic"], outline)
        return {"sub_tasks": tasks}
    except Exception as e:  # noqa: BLE001
        return {"error_log": [f"task_decomposer: {e}"], "sub_tasks": []}


def researcher(state: ResearchState) -> dict:
    """节点 3：单个子任务搜索（由 Send 并行扇出，共享 search_results 自动合并）。"""
    try:
        sub_task: Optional[SubTask] = state.get("sub_task")
        if sub_task is None:
            return {"search_results": [], "error_log": ["researcher: 缺少子任务输入"]}
        # 用关键词（或描述兜底）构造搜索 query；search_web 聚合国内外多源
        query = " ".join(sub_task.keywords) if sub_task.keywords else sub_task.description
        max_results = get_settings().tavily_max_results
        results = search_web.invoke({"query": query, "max_results": max_results})
        # results 为 List[dict]，与 search_results 的 reducer（operator.add）兼容
        return {"search_results": results}
    except Exception as e:  # noqa: BLE001
        return {"search_results": [], "error_log": [f"researcher: {e}"]}


def analyst(state: ResearchState) -> dict:
    """节点 4：聚合所有搜索结果，提取结构化市场数据。

    反馈来源有二：
    - human_feedback：人工打回草稿且反馈涉及数据（graph.py 关键词路由）时设置；
    - auto_feedback：reflection 环内 reflector 质检发现数据缺口时设置。
    两者实际互斥（人工反馈由 writer 消费后清除，自动反馈只存在于反思环内）；
    若并存，human_feedback 优先，auto_feedback 仍会被清除防止泄漏到下一轮。

    重新提取时清空旧草稿，强制 writer 重新生成。
    """
    try:
        feedback = state.get("human_feedback") or state.get("auto_feedback")
        # 已有数据且无新反馈 → 无需重算
        if state.get("extracted_data") and not feedback:
            return {}
        search_results: List[dict] = state.get("search_results") or []
        outline = state.get("outline")
        if outline is None:
            return {"error_log": ["analyst: 缺少大纲"], "extracted_data": {}}
        data = extract_market_data(state["topic"], outline, search_results, feedback)
        # 清草稿强制重写；auto_feedback 已消费
        return {"extracted_data": data, "draft_report": None, "auto_feedback": None}
    except Exception as e:  # noqa: BLE001
        return {"error_log": [f"analyst: {e}"], "extracted_data": {}}


def reflector(state: ResearchState) -> dict:
    """节点：反思质检已提取数据，决定是否需要定向补搜。

    输出 DataReflection 写入 state；有缺口时将 missing_questions 合并为
    auto_feedback 交给 analyst 下一轮提取时重点补充。路由决策
    （补搜 or 进 writer）由 graph.py route_after_reflection 依据
    reflection / reflection_count 做出。

    异常 fail-open：reflection 置 None，路由据此直接进 writer，不阻塞流程。
    """
    try:
        count = state.get("reflection_count") or 0
        outline = state.get("outline")
        extracted = state.get("extracted_data") or {}
        if outline is None:
            return {"error_log": ["reflector: 缺少大纲，跳过反思"]}
        reflection = generate_reflection(state["topic"], outline, extracted)
        has_gaps = bool(reflection.has_gaps)
        missing = "；".join(reflection.missing_questions)
        return {
            "reflection": reflection,
            "reflection_count": count + 1,
            "reflection_log": [
                {
                    "round": count + 1,
                    "has_gaps": has_gaps,
                    "summary": reflection.summary,
                    "gap_queries": reflection.gap_queries,
                }
            ],
            "auto_feedback": missing if has_gaps and missing else None,
            "data_approved": not has_gaps,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "reflection": None,  # 路由据此 fail-open 直接进 writer
            "reflection_count": (state.get("reflection_count") or 0) + 1,
            "reflection_log": [
                {"round": (state.get("reflection_count") or 0) + 1, "error": str(e)}
            ],
            "error_log": [f"reflector: {e}"],
            "auto_feedback": None,
        }


def gap_researcher(state: ResearchState) -> dict:
    """节点：反思缺口定向补搜（由 Send 并行扇出，结果并入 search_results）。

    与 researcher 同构：用 gap_query 构造搜索 query，返回统一格式结果，
    经 operator.add reducer 自动合并，随后路由回 analyst 重新提取数据。
    """
    try:
        gap_query = state.get("gap_query")
        if not gap_query:
            return {"search_results": [], "error_log": ["gap_researcher: 缺少缺口查询"]}
        max_results = get_settings().tavily_max_results
        results = search_web.invoke({"query": gap_query, "max_results": max_results})
        return {"search_results": results}
    except Exception as e:  # noqa: BLE001
        return {"search_results": [], "error_log": [f"gap_researcher: {e}"]}


def writer(state: ResearchState) -> dict:
    """节点 5：根据大纲与数据撰写报告初稿 → interrupt 请求人工审核。

    resume 值结构同 supervisor_planner：
        {"approved": bool, "feedback": Optional[str]}
    - approved=True  → 草稿通过，路由到 finalizer；
    - approved=False → 根据反馈内容路由回 analyst（数据类）或 writer（措辞类）。
    """
    try:
        feedback = state.get("human_feedback")
        if feedback:
            # 用户提出修改意见 → 基于反馈重新撰写
            draft = generate_draft(
                state["topic"], state["outline"], state["extracted_data"], feedback
            )
            return_state = {"human_feedback": None}  # 本次反馈已消费
        elif state.get("draft_report") is not None:
            # 恢复重跑：state 中已有人工审核的草稿（由 feedback 端点 update_state 写回）
            draft = state["draft_report"]
            return_state = {}
        else:
            draft = generate_draft(state["topic"], state["outline"], state["extracted_data"])
            return_state = {}

        # 暂停图，等待前端审核草稿
        resp = interrupt({"type": "draft", "report": draft})
        approved = bool(resp.get("approved"))

        return {
            **return_state,
            "draft_report": draft,
            "draft_approved": approved,
            "human_feedback": None if approved else resp.get("feedback"),
        }
    except GraphInterrupt:
        raise  # interrupt 机制：必须重新抛出
    except Exception as e:  # noqa: BLE001
        return {"error_log": [f"writer: {e}"]}


def finalizer(state: ResearchState) -> dict:
    """节点 6：按最终反馈整合草稿，输出 final_report（Markdown）。"""
    try:
        draft = state.get("draft_report") or ""
        feedback = state.get("human_feedback")
        final = finalize_report(state["topic"], draft, state["extracted_data"], feedback)
        return {"final_report": final}
    except Exception as e:  # noqa: BLE001
        # 兜底：即使整合失败也保留草稿作为最终报告
        return {"final_report": state.get("draft_report") or "", "error_log": [f"finalizer: {e}"]}
