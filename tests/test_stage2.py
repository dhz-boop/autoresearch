"""阶段 2 集成测试：用真实 LLM 跑通 LangGraph 完整图流程。

覆盖四条路径：
- flow_main:           大纲批准 → 并行搜索 → 反思质检 → 草稿批准 → 最终报告（happy path）
- flow_revise_outline: 大纲被要求修改 → 重新规划 → 再次确认
- flow_revise_draft:   草稿收到数据类反馈 → 路由回 analyst 重新提取 → 重写 → 定稿
- flow_restore_restart:后端重启后从 checkpoint 恢复暂停的调研并继续确认

各流程隐式覆盖新增的反思闭环（analyst → reflector → [gap_researcher → analyst]
或 → writer）：结构化输出已改 function_calling 优先（_structured_invoke），
注意每流程至少新增 1 次（至多 3 次）LLM 调用。

运行方式（需 .env 中配置 API key）：
    pytest tests/test_stage2.py -m integration
"""
import asyncio
import os
import time

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from graph import build_compiled_graph
from history import ResearchHistory
from manager import ResearchManager
from models import Outline


def _new_graph():
    """每个流程使用独立的 MemorySaver，隔离状态。"""
    return build_compiled_graph(MemorySaver())


async def _resume(graph, cfg, approved, feedback=None, update=None):
    """封装一次人工确认：先 update_state 写回已审核内容，再 Command(resume) 恢复。"""
    if update:
        await graph.aupdate_state(cfg, update)
    await graph.ainvoke(Command(resume={"approved": approved, "feedback": feedback}), cfg)


async def _flow_main():
    graph = _new_graph()
    cfg = {"configurable": {"thread_id": "main"}}

    # 1. 首次运行 → 暂停在 plan interrupt
    await graph.ainvoke({"topic": "2026年国内咖啡机市场机会"}, cfg)
    st = await graph.aget_state(cfg)
    iv = st.tasks[-1].interrupts[0].value
    assert iv["type"] == "plan"
    assert st.next == ("supervisor_planner",)
    outline = Outline(**iv["outline"])
    assert outline.sections

    # 2. 批准大纲 → 并行搜索 + 分析 + 反思质检 → 暂停在 draft interrupt
    await _resume(graph, cfg, approved=True, update={"outline": outline})
    st = await graph.aget_state(cfg)
    iv = st.tasks[-1].interrupts[0].value
    assert iv["type"] == "draft"
    draft = iv["report"]
    vals = st.values
    assert len(vals.get("search_results") or []) > 0, "Send 并行搜索应产出结果"
    assert isinstance(vals.get("extracted_data"), dict)
    assert (vals.get("reflection_count") or 0) >= 1, "数据提取后应经过反思质检"
    assert isinstance(vals.get("data_approved"), bool), "反思质检应产出结论"

    # 3. 批准草稿 → finalizer 输出最终报告
    await _resume(graph, cfg, approved=True, update={"draft_report": draft})
    vals = (await graph.aget_state(cfg)).values
    assert vals.get("final_report")
    assert not vals.get("error_log"), f"不应有异常记录: {vals['error_log']}"


async def _flow_revise_outline():
    graph = _new_graph()
    cfg = {"configurable": {"thread_id": "revise"}}

    await graph.ainvoke({"topic": "2025年智能家居市场趋势"}, cfg)
    iv = (await graph.aget_state(cfg)).tasks[-1].interrupts[0].value
    outline_a = Outline(**iv["outline"])

    # 提出修改意见 → 条件边应路由回 supervisor_planner 重新规划并再次暂停
    await _resume(graph, cfg, approved=False, feedback="请补充海外市场对比章节",
                  update={"outline": outline_a})
    st = await graph.aget_state(cfg)
    iv2 = st.tasks[-1].interrupts[0].value
    assert iv2["type"] == "plan", "修改大纲后应再次请求确认"
    assert st.next == ("supervisor_planner",)

    # 通过修改后的大纲 → 进入草稿阶段
    await _resume(graph, cfg, approved=True, update={"outline": Outline(**iv2["outline"])})
    st = await graph.aget_state(cfg)
    assert st.tasks[-1].interrupts[0].value["type"] == "draft"


async def _flow_revise_draft():
    graph = _new_graph()
    cfg = {"configurable": {"thread_id": "revise-draft"}}

    await graph.ainvoke({"topic": "2026年国内咖啡机市场机会"}, cfg)
    iv = (await graph.aget_state(cfg)).tasks[-1].interrupts[0].value
    await _resume(graph, cfg, approved=True, update={"outline": Outline(**iv["outline"])})
    iv = (await graph.aget_state(cfg)).tasks[-1].interrupts[0].value
    draft1 = iv["report"]

    # 数据类反馈 → 路由回 analyst 重新提取 → writer 重写 → 再次暂停
    await _resume(graph, cfg, approved=False, feedback="市场规模数据需要更准确来源",
                  update={"draft_report": draft1})
    st = await graph.aget_state(cfg)
    iv2 = st.tasks[-1].interrupts[0].value
    assert iv2["type"] == "draft", "应重新提取数据并重写草稿"
    assert iv2["report"] != draft1, "重写后的草稿应不同于原稿"

    # 通过重写草稿 → 定稿
    await _resume(graph, cfg, approved=True, update={"draft_report": iv2["report"]})
    vals = (await graph.aget_state(cfg)).values
    assert vals.get("final_report")


async def _flow_restore_after_restart():
    """后端重启后：线程不在内存，restore 应从 checkpoint 恢复并可继续确认。"""
    # 用临时 history 库，避免污染开发数据
    db = "/tmp/autoresearch_restore_test.sqlite"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)

    graph = _new_graph()
    cfg = {"configurable": {"thread_id": "restore"}}

    # 1. 首次运行 → 暂停在 plan interrupt
    await graph.ainvoke({"topic": "2026年国内咖啡机市场机会"}, cfg)
    iv = (await graph.aget_state(cfg)).tasks[-1].interrupts[0].value
    assert iv["type"] == "plan"

    # 2. 模拟进程重启：新建 manager（threads 为空），restore 从 checkpoint 恢复
    history = ResearchHistory(db)
    await history.connect()
    try:
        mgr = ResearchManager(graph, history)
        assert "restore" not in mgr.threads
        assert await mgr.restore("restore") is True
        types = [e["event"] for e in mgr.threads["restore"].history]
        assert "interrupt" in types, f"恢复后应补发 interrupt 事件: {types}"
        assert types[0] == "node_start", "恢复后应先补发 node_start"

        # 3. 恢复后继续确认（批准大纲 → 应推进到草稿确认点）
        #    轮询等待：真实 LLM 下「搜索 + 分析 + 撰写草稿」较慢，最多等 300 秒
        await mgr.feedback("restore", approved=True, feedback=None, outline=iv["outline"])
        deadline = time.monotonic() + 300
        got_draft = False
        while time.monotonic() < deadline:
            ivs_now = [
                e["data"]
                for e in mgr.threads["restore"].history
                if e["event"] == "interrupt"
            ]
            if ivs_now and ivs_now[-1]["type"] == "draft":
                got_draft = True
                break
            await asyncio.sleep(1.0)
        assert got_draft, "批准大纲后应推进到草稿确认点（draft 中断）"
    finally:
        await history.close()


@pytest.mark.integration
def test_full_flow_main():
    """happy path：大纲批准 → 并行搜索 → 草稿批准 → 最终报告。"""
    asyncio.run(_flow_main())


@pytest.mark.integration
def test_revise_outline():
    """大纲修改分支：反馈 → 重新规划 → 再次确认。"""
    asyncio.run(_flow_revise_outline())


@pytest.mark.integration
def test_revise_draft():
    """草稿数据类反馈分支：路由回 analyst → 重写 → 定稿。"""
    asyncio.run(_flow_revise_draft())


@pytest.mark.integration
def test_restore_after_restart():
    """后端重启后恢复线程：checkpoint 中暂停的调研可恢复并继续确认。"""
    asyncio.run(_flow_restore_after_restart())
