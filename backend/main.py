"""FastAPI 应用入口：提供 AutoResearch 的 REST + SSE 接口。

端点：
- POST /research/start                启动新调研，返回 thread_id
- GET  /research/{thread_id}/stream   SSE 实时事件流（node_start/node_end/interrupt/final）
- POST /research/{thread_id}/feedback 提交人工反馈并恢复执行
- GET  /research/{thread_id}/report   获取最终报告（Markdown）
- GET  /research/history              调研历史列表（倒序）
- GET  /research/{thread_id}/detail   单次调研详情（报告全文 + 批改意见）
"""
import asyncio
import io
import json
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, StringConstraints
from sse_starlette.sse import EventSourceResponse

from config import get_settings
from exporters import outline_to_docx, outline_to_markdown, report_to_docx
from graph import build_compiled_graph, close_checkpointer, create_checkpointer
from history import ResearchHistory
from manager import ResearchManager, _interrupt_info


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------
class StartRequest(BaseModel):
    """POST /research/start 请求体。"""

    # 使用 StringConstraints：Pydantic v2 中 Field(strip_whitespace=...) 已废弃不生效
    topic: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ]


class FeedbackRequest(BaseModel):
    """POST /research/{thread_id}/feedback 请求体。

    approved: 是否批准；false 时 feedback 为修改意见。
    outline / draft: 可选，人工编辑后的大纲/草稿（写回状态，保证一致性）。
    """

    approved: bool
    feedback: Optional[str] = None
    outline: Optional[dict] = None
    draft: Optional[str] = None


# ---------------------------------------------------------------------------
# 应用生命周期
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时创建 checkpointer 与编译图，构建线程管理器；退出时释放资源。"""
    settings = get_settings()
    checkpointer = await create_checkpointer()
    graph = build_compiled_graph(checkpointer)
    history = ResearchHistory(settings.history_db)
    await history.connect()
    app.state.history = history
    app.state.manager = ResearchManager(graph, history)
    yield
    # 应用退出：关闭 checkpointer 持有的连接（sqlite 连接 / postgres 连接池）
    await close_checkpointer(checkpointer)
    await history.close()


app = FastAPI(title="AutoResearch", version="1.0.0", lifespan=lifespan)

# 开发期放开跨域，便于独立前端页面/React 应用联调；生产可按需收紧 allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    """健康检查。"""
    return {"status": "ok"}


@app.post("/research/start")
async def research_start(req: StartRequest) -> dict:
    """启动新调研：立即返回 thread_id，图在后台异步执行。"""
    manager: ResearchManager = app.state.manager
    thread_id = manager.start(req.topic)
    return {"thread_id": thread_id}


@app.get("/research/{thread_id}/stream")
async def research_stream(thread_id: str, request: Request):
    """SSE 事件流：节点开始/结束、人工介入、最终报告。

    新连接会先补发该线程的历史事件，再实时推送。
    """
    manager: ResearchManager = app.state.manager
    if thread_id not in manager.threads:
        # 线程不在内存（后端重启 / 延时清理后）：尝试从 checkpoint 恢复，
        # 恢复成功则补发中断/最终报告等事件，前端刷新后即可继续
        if not await manager.restore(thread_id):
            raise HTTPException(status_code=404, detail="未知的 thread_id")
    q = manager.subscribe(thread_id)

    async def gen():
        try:
            # 历史事件快照已在 subscribe() 时一次性复制进订阅者队列，
            # 这里只消费队列，避免「补发历史 + 实时推送」的重复/遗漏竞态。
            # 注意：只 yield 纯 JSON 字符串，`data:` 前缀由 EventSourceResponse 添加。
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield json.dumps(evt, ensure_ascii=False)
                except asyncio.TimeoutError:
                    continue
        finally:
            manager.unsubscribe(thread_id, q)

    return EventSourceResponse(gen())


@app.post("/research/{thread_id}/feedback")
async def research_feedback(thread_id: str, req: FeedbackRequest) -> dict:
    """提交人工反馈（批准/修改意见），并恢复图执行。

    并发防护：线程正在执行/恢复时拒绝（409），避免双 resume 竞态。
    """
    manager: ResearchManager = app.state.manager
    if thread_id not in manager.threads:
        raise HTTPException(status_code=404, detail="未知的 thread_id")
    if manager.is_running(thread_id):
        raise HTTPException(
            status_code=409, detail="调研任务正在执行中，请等待其暂停后再提交反馈"
        )
    try:
        await manager.feedback(
            thread_id,
            approved=req.approved,
            feedback=req.feedback,
            outline=req.outline,
            draft=req.draft,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"status": "resumed", "thread_id": thread_id}


@app.get("/research/{thread_id}/report")
async def research_report(thread_id: str):
    """获取最终报告（Markdown）；未完成时返回 404。"""
    manager: ResearchManager = app.state.manager
    config = {"configurable": {"thread_id": thread_id}}
    st = await manager.graph.aget_state(config)
    report = (st.values or {}).get("final_report")
    if not report:
        return JSONResponse(status_code=404, content={"detail": "报告尚未完成"})
    return {"thread_id": thread_id, "final_report": report}


# ---------------------------------------------------------------------------
# 调研历史端点：列表 + 详情（报告全文与生成过程中的批改意见）
# ---------------------------------------------------------------------------
@app.get("/research/history")
async def research_history_list():
    """调研历史列表（按创建时间倒序；不含报告全文，详情走 detail 接口）。"""
    history: ResearchHistory = app.state.history
    return {"history": await history.list_all()}


@app.get("/research/{thread_id}/detail")
async def research_history_detail(thread_id: str):
    """单次调研历史详情：元数据 + 最终报告全文 + 生成过程中的全部批改意见。"""
    history: ResearchHistory = app.state.history
    rec = await history.get(thread_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="未知的 thread_id")
    return rec


@app.get("/research/{thread_id}/history/export")
async def export_history_report(
    thread_id: str, format: Literal["markdown", "docx"] = "markdown"
):
    """导出历史调研报告（Markdown / Word）。

    从调研历史库读取最终报告，不依赖 checkpoint（历史功能独立持久化的设计）。
    """
    history: ResearchHistory = app.state.history
    rec = await history.get(thread_id)
    if rec is None or not rec.get("final_report"):
        raise HTTPException(status_code=404, detail="该历史调研没有可导出的报告")
    return _file_response(
        kind="history-report",
        thread_id=thread_id,
        fmt=format,
        markdown=rec["final_report"],
        docx=report_to_docx(rec["final_report"]),
    )


@app.delete("/research/{thread_id}")
async def research_delete(thread_id: str):
    """删除一条调研历史（历史记录 + checkpoint 线程状态）；正在执行的调研拒绝删除。"""
    manager: ResearchManager = app.state.manager
    history: ResearchHistory = app.state.history
    rec = await history.get(thread_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="未知的 thread_id")
    if manager.is_running(thread_id):
        raise HTTPException(status_code=409, detail="调研正在执行中，无法删除")
    # 彻底清除：同时删除 checkpoint 中的线程状态
    try:
        await manager.graph.adelete_thread(thread_id)
    except Exception:  # noqa: BLE001 - checkpoint 删除失败不阻断历史记录删除
        pass
    await history.delete(thread_id)
    manager.drop_thread(thread_id)
    return {"status": "deleted", "thread_id": thread_id}


# ---------------------------------------------------------------------------
# 导出端点：大纲 / 最终报告 → Markdown / Word(.docx)
# ---------------------------------------------------------------------------
def _file_response(*, kind: str, thread_id: str, fmt: Literal["markdown", "docx"], markdown: str, docx: bytes):
    """封装下载响应：Content-Disposition 附件 + 正确的 media_type。"""
    if fmt == "markdown":
        return StreamingResponse(
            io.BytesIO(markdown.encode("utf-8")),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{thread_id}-{kind}.md"'},
        )
    return StreamingResponse(
        io.BytesIO(docx),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{thread_id}-{kind}.docx"'},
    )


@app.get("/research/{thread_id}/outline/export")
async def export_outline(
    thread_id: str, format: Literal["markdown", "docx"] = "markdown"
):
    """导出调研大纲（Markdown / Word）。

    outline 优先取 state.outline；首次中断未批准时 outline 尚未写入 state，
    改为从 pending interrupt payload 中提取。
    """
    manager: ResearchManager = app.state.manager
    if thread_id not in manager.threads:
        raise HTTPException(status_code=404, detail="未知的 thread_id")
    config = {"configurable": {"thread_id": thread_id}}
    st = await manager.graph.aget_state(config)
    values = st.values or {}
    outline = values.get("outline")
    if outline is None:
        info = _interrupt_info(st)
        if info is not None:
            _, iv = info
            if iv.get("type") == "plan":
                outline = iv.get("outline")
    if outline is None:
        raise HTTPException(status_code=404, detail="大纲尚未生成")
    topic = values.get("topic") or ""
    return _file_response(
        kind="outline",
        thread_id=thread_id,
        fmt=format,
        markdown=outline_to_markdown(outline, topic),
        docx=outline_to_docx(outline, topic),
    )


@app.get("/research/{thread_id}/report/export")
async def export_report(
    thread_id: str, format: Literal["markdown", "docx"] = "markdown"
):
    """导出最终报告（Markdown / Word）；未完成时返回 404。"""
    manager: ResearchManager = app.state.manager
    if thread_id not in manager.threads:
        raise HTTPException(status_code=404, detail="未知的 thread_id")
    config = {"configurable": {"thread_id": thread_id}}
    st = await manager.graph.aget_state(config)
    report = (st.values or {}).get("final_report")
    if not report:
        raise HTTPException(status_code=404, detail="报告尚未完成")
    return _file_response(
        kind="report",
        thread_id=thread_id,
        fmt=format,
        markdown=report,  # 报告本身已是 Markdown
        docx=report_to_docx(report),
    )
