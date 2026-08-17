"""ResearchManager：管理每个调研线程的运行、SSE 事件广播与人工反馈恢复。

职责：
- ThreadChannel：线程级事件广播通道（历史事件 + 活跃 SSE 订阅者队列）；
- NodeStartRecorder：异步回调，捕获节点 on_chain_start 推送 node_start 事件；
- ResearchManager：启动/恢复图运行（后台任务），并在节点完成/中断/定稿时推送事件；
  同时把「调研元数据 / 最终报告 / 人工批改意见」写入 ResearchHistory（历史功能）。
"""
import asyncio
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import AsyncCallbackHandler
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from history import ResearchHistory
from models import Outline

# 单线程保留的事件历史上限（防止历史无界累积导致内存泄漏）
_MAX_HISTORY = 300
# 线程完成后（final/error）保留多久再清理，给迟到的 SSE 订阅者留出重连窗口
_THREAD_TTL_SECONDS = 3600
# LLM 流式增量的缓冲阈值：累积超过该字符数，或距上次推送超过该秒数，就推一条 stream 事件
_STREAM_FLUSH_CHARS = 80
_STREAM_FLUSH_INTERVAL = 0.3


def _to_jsonable(obj: Any) -> Any:
    """递归将对象转为可 JSON 序列化的结构（Pydantic 模型 → dict）。"""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _stream_text(message: Any) -> str:
    """从 AIMessageChunk 提取增量文本（兼容 content 为 str / list）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )
    return str(content)


def _interrupt_info(state: Any) -> Optional[tuple]:
    """从 checkpoint 状态中定位「当前待确认」的中断，返回 (node, payload)。

    - 遍历 st.tasks 找第一个带 interrupts 的任务（而非简单取最后一个，
      避免理论多分支场景下取错）；
    - node 优先取任务自身的 name（节点名），回退到 st.next[0]。
    """
    for task in state.tasks or ():
        if task.interrupts:
            node = getattr(task, "name", None) or (
                state.next[0] if state.next else "?"
            )
            return node, task.interrupts[0].value
    return None


class ThreadChannel:
    """单个调研线程的 SSE 广播通道。

    维护已发生的事件历史（新连接的订阅者先补发历史），并广播给所有活跃订阅者。
    历史为有界 deque，避免无界累积。
    """

    def __init__(self) -> None:
        self.history: deque = deque(maxlen=_MAX_HISTORY)
        self._subscribers: List[asyncio.Queue] = []
        # 线程内事件单调递增序号：前端据此区分「断线重连重放的历史」与
        # 「打回后新产生的同内容中断」，避免按内容去重误伤
        self._seq = 0

    def subscribe(self) -> asyncio.Queue:
        """注册一个订阅者队列，并把当前历史一次性复制进该队列。

        本方法为同步方法，执行期间不会让出事件循环，因此「复制历史快照」与
        后续 publish 的实时事件之间不会交错，既不会遗漏也不会重复。
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        for evt in self.history:
            q.put_nowait(evt)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """SSE 连接断开时移除订阅者。"""
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event: dict) -> None:
        """推送一条事件：写入历史并广播给所有订阅者。"""
        event = {**event, "seq": self._seq}
        self._seq += 1
        self.history.append(event)
        for q in self._subscribers:
            await q.put(event)

    def publish_live(self, event: dict) -> None:
        """推送瞬时事件（LLM 流式增量）：仅广播给活跃订阅者，不写入历史。

        流式 token 是高频、瞬时的，不应进入历史快照——否则会污染历史、
        塞满历史 deque，且断线重连会重放大量无意义的 token 文本。
        """
        for q in self._subscribers:
            q.put_nowait(event)


class NodeStartRecorder(AsyncCallbackHandler):
    """异步回调：捕获节点的 on_chain_start，推送 node_start 事件。

    通过 kwargs["name"] == metadata["langgraph_node"] 精确识别「节点本身」的 run，
    过滤掉图自身的 run（langgraph_node 为 None）以及节点内部 LLM 的嵌套 run。
    """

    def __init__(self, channel: ThreadChannel) -> None:
        self.channel = channel

    async def on_chain_start(
        self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs
    ) -> None:
        meta = kwargs.get("metadata") or {}
        node = meta.get("langgraph_node")
        if node is None or kwargs.get("name") != node:
            return  # 非节点本身的 run，忽略
        await self.channel.publish({"event": "node_start", "node": node, "data": {}})


class ResearchManager:
    """多调研线程管理器：持有编译图，负责启动/恢复执行与事件广播。

    history: ResearchHistory，用于持久化调研记录与人工批改意见（历史功能）。
    """

    def __init__(self, graph: Any, history: ResearchHistory) -> None:
        self.graph = graph
        self.history = history
        self.threads: Dict[str, ThreadChannel] = {}
        # 正在执行/恢复的 thread_id 集合，防止并发 feedback 导致双 resume 竞态
        self._running: set = set()

    # ------------------------------------------------------------------
    # 启动与执行
    # ------------------------------------------------------------------
    def start(self, topic: str) -> str:
        """启动新调研：创建线程通道与 thread_id，后台运行图并立即返回 thread_id。"""
        thread_id = uuid.uuid4().hex
        self.threads[thread_id] = ThreadChannel()
        self._running.add(thread_id)
        config = {"configurable": {"thread_id": thread_id}}
        asyncio.create_task(self._start_and_execute(thread_id, topic, config))
        return thread_id

    async def _start_and_execute(self, thread_id: str, topic: str, config: dict) -> None:
        """先落库调研历史（running），再后台执行图。

        保证 history.create 严格先于 feedback/finish 写入，避免并发竞态；
        历史写入失败不阻断调研主流程。
        """
        await self._safe_history(self.history.create(thread_id, topic))
        await self._execute(thread_id, {"topic": topic}, config)

    def is_running(self, thread_id: str) -> bool:
        """该线程是否正在执行/恢复（用于 feedback 端点的并发防护）。"""
        return thread_id in self._running

    async def _execute(self, thread_id: str, graph_input: Any, config: dict) -> None:
        """后台执行图（首次运行或 interrupt 恢复），边跑边推送 SSE 事件。

        使用 stream_mode=["updates", "messages"]：
        - updates：节点完成的完整输出（推送 node_end）；
        - messages：LLM 逐 token 增量（缓冲后推送 stream 事件，前端实时显示生成过程）。
        流式增量按「字符数阈值 OR 时间阈值」聚合，避免海量小事件压垮 SSE 与历史。
        """
        channel = self.threads.get(thread_id)
        if channel is None:
            self._running.discard(thread_id)
            return
        recorder = NodeStartRecorder(channel)
        run_config = {**config, "callbacks": [recorder]}

        # ---- LLM 流式增量缓冲 ----
        stream_buf = ""
        stream_node: Optional[str] = None
        last_flush = time.monotonic()

        def flush_stream() -> None:
            nonlocal stream_buf
            if stream_buf:
                channel.publish_live(
                    {
                        "event": "stream",
                        "node": stream_node or "?",
                        "data": {"text": stream_buf},
                    }
                )
                stream_buf = ""

        try:
            async for mode, chunk in self.graph.astream(
                graph_input, run_config, stream_mode=["updates", "messages"]
            ):
                if mode == "messages":
                    # chunk 形如 (messages, metadata)；messages 可能是单个 message 或列表
                    if isinstance(chunk, tuple) and len(chunk) == 2:
                        messages, metadata = chunk
                    else:
                        messages, metadata = chunk, {}
                    if not isinstance(messages, (list, tuple)):
                        messages = [messages]
                    node = (metadata or {}).get("langgraph_node") or stream_node or "?"
                    for message in messages:
                        text = _stream_text(message)
                        if not text:
                            continue
                        if node != stream_node:
                            # 节点切换：先刷出上一个节点的残留增量
                            flush_stream()
                            stream_node = node
                        stream_buf += text
                        now = time.monotonic()
                        if (
                            len(stream_buf) >= _STREAM_FLUSH_CHARS
                            or (now - last_flush) >= _STREAM_FLUSH_INTERVAL
                        ):
                            flush_stream()
                            last_flush = now
                else:  # updates
                    # 节点输出产生前先刷出该节点残留的流式增量
                    flush_stream()
                    stream_node = None
                    last_flush = time.monotonic()
                    for node, output in chunk.items():
                        if node == "__interrupt__":
                            continue  # interrupt 事件在流结束后单独推送
                        # 节点输出可能含 Pydantic 模型（如 outline），转成可 JSON 序列化结构
                        await channel.publish(
                            {"event": "node_end", "node": node, "data": _to_jsonable(output)}
                        )
            flush_stream()  # 图流结束，刷出剩余增量

            # 流结束后判断：是暂停在 interrupt、已完成、还是意外终止
            st = await self.graph.aget_state(config)
            info = _interrupt_info(st)
            if info is not None:
                node, iv = info
                await channel.publish(
                    {"event": "interrupt", "node": node, "data": _to_jsonable(iv)}
                )
            elif st.values.get("final_report"):
                await channel.publish(
                    {
                        "event": "final",
                        "node": "finalizer",
                        "data": {"final_report": st.values["final_report"]},
                    }
                )
                # 历史记录：写入最终报告全文
                await self._safe_history(
                    self.history.finish(thread_id, st.values["final_report"])
                )
                self._schedule_cleanup(thread_id)
            else:
                # 无中断、无最终报告却结束：通常是子任务拆解失败/为空导致图静默终止
                await channel.publish(
                    {
                        "event": "error",
                        "node": "system",
                        "data": {
                            "error": "调研流程意外终止：未生成可确认的结果，请更换主题后重试"
                        },
                    }
                )
                await self._safe_history(self.history.fail(thread_id))
                self._schedule_cleanup(thread_id)
        except Exception as e:  # noqa: BLE001
            await channel.publish(
                {"event": "error", "node": "system", "data": {"error": str(e)}}
            )
            await self._safe_history(self.history.fail(thread_id))
        finally:
            self._running.discard(thread_id)

    @staticmethod
    async def _safe_history(coro: Any) -> None:
        """执行一次历史写入；失败仅静默忽略，不影响调研主流程。"""
        try:
            await coro
        except Exception:  # noqa: BLE001
            pass

    def _schedule_cleanup(self, thread_id: str) -> None:
        """线程完成后延时移除，避免 threads 字典与历史事件无界累积。"""
        asyncio.create_task(self._cleanup_after(thread_id, _THREAD_TTL_SECONDS))

    async def _cleanup_after(self, thread_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.threads.pop(thread_id, None)

    # ------------------------------------------------------------------
    # 线程恢复与删除
    # ------------------------------------------------------------------
    async def restore(self, thread_id: str) -> bool:
        """把「不在内存但 checkpoint 中存在」的线程恢复到内存管理。

        场景：后端进程重启 / 线程被延时清理后，SSE 订阅或 feedback 前调用。
        根据 checkpoint 中该线程的状态重建 ThreadChannel 并推送对应事件：
        - 暂停在人工确认点（interrupt）→ node_start + interrupt，可继续确认；
        - 已完成（final_report）→ final，重放最终报告；
        - 其他（意外终止）→ error。
        恢复失败（checkpoint 中不存在）返回 False。
        """
        if thread_id in self.threads:
            return True
        config = {"configurable": {"thread_id": thread_id}}
        try:
            st = await self.graph.aget_state(config)
        except Exception:  # noqa: BLE001 - 线程不存在等情况
            return False
        if not st or not st.values:
            return False
        channel = ThreadChannel()
        info = _interrupt_info(st)
        if info is not None:
            node, iv = info
            await channel.publish({"event": "node_start", "node": node, "data": {}})
            await channel.publish({"event": "interrupt", "node": node, "data": _to_jsonable(iv)})
        elif st.values.get("final_report"):
            await channel.publish(
                {
                    "event": "final",
                    "node": "finalizer",
                    "data": {"final_report": st.values["final_report"]},
                }
            )
            self._schedule_cleanup(thread_id)
        else:
            await channel.publish(
                {
                    "event": "error",
                    "node": "system",
                    "data": {"error": "该调研已中断且无法继续，请重新发起调研"},
                }
            )
        self.threads[thread_id] = channel
        return True

    def drop_thread(self, thread_id: str) -> None:
        """从内存线程表移除该线程（删除调研后调用，避免残留）。"""
        self.threads.pop(thread_id, None)

    # ------------------------------------------------------------------
    # 人工反馈恢复
    # ------------------------------------------------------------------
    async def feedback(
        self,
        thread_id: str,
        approved: bool,
        feedback: Optional[str],
        outline: Optional[dict] = None,
        draft: Optional[str] = None,
    ) -> None:
        """提交人工反馈：写回已审核内容到状态，并以 Command(resume) 恢复图执行。

        大纲/草稿的「已确认版本」必须写回 state，否则 interrupt 恢复重跑时
        节点会重新生成，导致最终内容与人工确认的不一致。

        并发防护：先同步检查并标记「运行中」，再执行恢复。检查与标记之间
        无 await，事件循环不会切换，因此并发 feedback 请求不会同时通过。
        """
        if thread_id not in self.threads:
            raise ValueError("未知的 thread_id")
        if thread_id in self._running:
            raise RuntimeError("调研任务正在执行中，请等待其暂停后再提交反馈")
        self._running.add(thread_id)
        try:
            config = {"configurable": {"thread_id": thread_id}}

            # 校验当前确实存在待确认的中断，否则 resume 无意义（图已完成/未暂停）
            st = await self.graph.aget_state(config)
            if not any(t.interrupts for t in (st.tasks or ())):
                raise RuntimeError("当前线程没有待确认的人工介入点")

            # 记录本次人工批改意见（阶段取自中断 payload 的 type：plan / draft）
            info = _interrupt_info(st)
            stage = info[1].get("type") if info else None
            await self._safe_history(
                self.history.add_feedback(thread_id, stage, approved, feedback)
            )

            # 若请求未携带大纲/草稿，从当前中断 payload 中提取，保证一致性
            if outline is None or draft is None:
                info = _interrupt_info(st)
                if info is not None:
                    _, iv = info
                    if iv.get("type") == "plan" and outline is None:
                        outline = iv.get("outline")
                    if iv.get("type") == "draft" and draft is None:
                        draft = iv.get("report")

            update: dict = {}
            if outline is not None:
                try:
                    update["outline"] = Outline(**outline)
                except ValidationError as e:  # noqa: PERF203
                    raise ValueError(f"大纲内容不合法：{e}") from e
            if draft is not None:
                update["draft_report"] = draft
            if update:
                await self.graph.aupdate_state(config, update)

            # 后台恢复执行，后续事件继续推送到该线程通道
            asyncio.create_task(
                self._execute(
                    thread_id,
                    Command(resume={"approved": approved, "feedback": feedback}),
                    config,
                )
            )
        except BaseException:
            self._running.discard(thread_id)
            raise

    # ------------------------------------------------------------------
    # SSE 订阅辅助
    # ------------------------------------------------------------------
    def subscribe(self, thread_id: str) -> Optional[asyncio.Queue]:
        channel = self.threads.get(thread_id)
        return channel.subscribe() if channel else None

    def unsubscribe(self, thread_id: str, q: asyncio.Queue) -> None:
        channel = self.threads.get(thread_id)
        if channel:
            channel.unsubscribe(q)

    def history(self, thread_id: str) -> List[dict]:
        channel = self.threads.get(thread_id)
        return list(channel.history) if channel else []
