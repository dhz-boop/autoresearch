"""LangGraph 图定义：组装节点、边与条件路由，并编译为可执行图。

图结构：
    START → supervisor_planner ──(人工确认)──▶ task_decomposer
        ▲                                    │
        └────(大纲需修改)────────────────────┘
    task_decomposer ──Send 扇出──▶ researcher ×N ──汇合──▶ analyst
    analyst → reflector ──(反思质检)──▶ writer ──(人工审核)──▶ finalizer → END
        ▲                    │
        │                (有数据缺口) Send 扇出
        └──── gap_researcher ×N ───────────┘
    writer 打回：──(数据类反馈)──analyst  /  (措辞类反馈)──writer

反思闭环：reflector 质检 extracted_data，有缺口且未达 _MAX_REFLECTIONS 时
Send 扇出 gap_researcher 定向补搜，汇合回 analyst 重新提取；通过/超限/异常
fail-open 直接进 writer。

依赖：langgraph 1.2.x。注意 add_edge / add_conditional_edges 为 StateGraph 的
实例方法（不再以模块顶层函数导出）。
"""
from pathlib import Path
from typing import List, Optional

import aiosqlite
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from config import get_settings
from nodes import (
    analyst,
    finalizer,
    gap_researcher,
    reflector,
    researcher,
    supervisor_planner,
    task_decomposer,
    writer,
)
from state import ResearchState

# 数据类修改反馈的关键词：命中则回 analyst 重新提取数据
_DATA_FEEDBACK_KEYWORDS = ("数据", "来源", "搜索", "准确", "事实", "数字", "统计", "对比", "引用")

# 反思质检循环上限（全线程累计）：达到后无论是否仍有缺口都直接进 writer。
# 该上限只在路由函数（route_after_reflection）中生效，故定义在本模块。
_MAX_REFLECTIONS = 2


# ---------------------------------------------------------------------------
# 条件路由
# ---------------------------------------------------------------------------
def route_after_plan(state: ResearchState) -> str:
    """大纲人工确认后：通过则拆解子任务，未通过则回 supervisor_planner 重新规划。"""
    return "task_decomposer" if state.get("plan_approved") else "supervisor_planner"


def _last_error_node(state: ResearchState) -> Optional[str]:
    """返回 error_log 中最近一条错误对应的节点名（条目格式为 `节点名: 错误`）。"""
    log = state.get("error_log") or []
    if not log:
        return None
    return str(log[-1]).split(":", 1)[0].strip()


def route_after_draft(state: ResearchState) -> str:
    """草稿人工审核后：通过则定稿；未通过按反馈内容路由到 analyst 或 writer。

    兜底：writer/analyst 执行失败且本轮无人工反馈时，直接定稿（用现有草稿），
    避免「失败 → 路由回自身重试 → 再失败」的无反馈死循环持续烧 LLM。
    """
    if state.get("draft_approved"):
        return "finalizer"
    if state.get("human_feedback") is None and _last_error_node(state) in ("writer", "analyst"):
        return "finalizer"
    feedback = state.get("human_feedback") or ""
    if any(kw in feedback for kw in _DATA_FEEDBACK_KEYWORDS):
        return "analyst"  # 数据类问题 → 重新提取数据
    return "writer"  # 措辞/结构类问题 → 直接重写


def continue_to_researchers(state: ResearchState):
    """使用 Send 扇出：为每个子任务派发一个并行 researcher 分支。

    所有 researcher 分支完成后自动汇合，analyst 仅执行一次（superstep 汇合）。
    禁止用 for 循环串行调用搜索工具。

    子任务为空（LLM 拆解失败/异常）时返回 "supervisor_planner"，回规划节点
    重新规划，避免空 Send 列表导致图静默终止、前端永久等待。
    """
    sub_tasks = state.get("sub_tasks") or []
    if not sub_tasks:
        return "supervisor_planner"
    return [Send("researcher", {"sub_task": t}) for t in sub_tasks]


def route_after_reflection(state: ResearchState):
    """反思质检后的路由：有数据缺口且未达上限 → Send 扇出 gap_researcher 补搜；
    否则进 writer。

    fail-open 兜底（一律直接进 writer，不循环）：
    - reflector 异常（reflection 为 None）；
    - 缺口查询为空（gap_queries 为空）——避免空 Send 列表导致图静默终止（BUGS #13）；
    - 已达 _MAX_REFLECTIONS——防止无限补搜持续烧额度（BUGS #16 同类问题）。

    reflection_count 为全线程累计值，不随人工反馈轮次重置：
    即使后续人工打回重走 analyst，也不会因上限失效而无限循环。
    """
    reflection = state.get("reflection")
    count = state.get("reflection_count") or 0
    if (
        reflection is not None
        and reflection.has_gaps
        and reflection.gap_queries
        and count < _MAX_REFLECTIONS
    ):
        return [Send("gap_researcher", {"gap_query": q}) for q in reflection.gap_queries]
    return "writer"


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------
def build_graph() -> StateGraph:
    """构建未编译的 StateGraph（节点、边、条件路由）。"""
    g = StateGraph(ResearchState)

    # 注册节点
    g.add_node("supervisor_planner", supervisor_planner)
    g.add_node("task_decomposer", task_decomposer)
    g.add_node("researcher", researcher)
    g.add_node("analyst", analyst)
    g.add_node("reflector", reflector)
    g.add_node("gap_researcher", gap_researcher)
    g.add_node("writer", writer)
    g.add_node("finalizer", finalizer)

    # 起始边
    g.add_edge(START, "supervisor_planner")

    # 大纲人工确认后的条件路由
    g.add_conditional_edges(
        "supervisor_planner",
        route_after_plan,
        {"supervisor_planner": "supervisor_planner", "task_decomposer": "task_decomposer"},
    )

    # Send 并行扇出 → researcher → 汇合到 analyst；
    # 子任务为空时回 supervisor_planner 重新规划（continue_to_researchers 返回 "supervisor_planner"）
    g.add_conditional_edges(
        "task_decomposer",
        continue_to_researchers,
        {"researcher": "researcher", "supervisor_planner": "supervisor_planner"},
    )
    g.add_edge("researcher", "analyst")

    # 反思质检：analyst 提取数据 → reflector 质检 →
    #   通过/超限/异常 → writer；有缺口 → Send 扇出 gap_researcher 补搜 → 回 analyst
    g.add_edge("analyst", "reflector")
    g.add_conditional_edges(
        "reflector",
        route_after_reflection,
        {"writer": "writer", "gap_researcher": "gap_researcher"},
    )
    g.add_edge("gap_researcher", "analyst")

    # 草稿人工审核后的条件路由
    g.add_conditional_edges(
        "writer",
        route_after_draft,
        {"finalizer": "finalizer", "analyst": "analyst", "writer": "writer"},
    )

    g.add_edge("finalizer", END)
    return g


def _build_serde() -> JsonPlusSerializer:
    """构建 checkpointer 使用的序列化器。

    state 中存放了 models 模块的 Pydantic 模型（Outline / SubTask 等），
    显式声明允许序列化的模块，避免 langgraph 的 msgpack 反序列化警告/报错。
    """
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("models", "Outline"),
            ("models", "SubTask"),
            ("models", "SubTaskList"),
            ("models", "DataReflection"),
        ]
    )


async def create_checkpointer():
    """按配置创建 LangGraph Checkpointer（跨请求持久化）。

    - memory: MemorySaver（进程内，重启丢失）
    - sqlite: AsyncSqliteSaver 基于文件持久化；手动管理 aiosqlite 连接，
      使 checkpointer 在 FastAPI 应用生命周期内持续可用。
    - postgres: AsyncPostgresSaver（生产），基于 psycopg 连接池，
      需配置 DATABASE_URL。
    """
    serde = _build_serde()
    settings = get_settings()
    if settings.checkpointer == "memory":
        return MemorySaver(serde=serde)

    if settings.checkpointer == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        if not settings.database_url:
            raise ValueError("CHECKPOINTER=postgres 时必须配置 DATABASE_URL")
        pool = AsyncConnectionPool(conninfo=settings.database_url)
        saver = AsyncPostgresSaver(pool, serde=serde)
        await saver.setup()
        return saver

    # sqlite（默认）
    db_path = Path(settings.checkpoint_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    saver = AsyncSqliteSaver(conn, serde=serde)
    await saver.setup()
    return saver


def build_compiled_graph(checkpointer):
    """编译为可执行图（需注入 checkpointer 以支持 interrupt / 状态恢复）。"""
    return build_graph().compile(checkpointer=checkpointer)


async def close_checkpointer(checkpointer) -> None:
    """关闭 checkpointer 持有的连接/连接池（应用退出时释放资源）。

    - MemorySaver：无连接，直接返回；
    - AsyncSqliteSaver：关闭底层 aiosqlite 连接；
    - AsyncPostgresSaver：关闭 psycopg 连接池（构造时以 conn 参数接收 pool）。
    """
    # 1) saver 自带的 close（若存在且为同步/异步方法）
    close = getattr(checkpointer, "close", None)
    if close is not None:
        try:
            res = close()
            if hasattr(res, "__await__"):
                await res
        except Exception:  # noqa: BLE001 - 关闭失败不影响应用退出
            pass
    # 2) 关闭底层连接/连接池（AsyncSqliteSaver.conn / AsyncPostgresSaver.conn）
    conn = getattr(checkpointer, "conn", None)
    conn_close = getattr(conn, "close", None) if conn is not None else None
    if conn_close is not None:
        try:
            res = conn_close()
            if hasattr(res, "__await__"):
                await res
        except Exception:  # noqa: BLE001
            pass
