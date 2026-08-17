"""调研历史存储：持久化每次调研的元数据、最终报告与人工批改意见。

为什么需要独立于 LangGraph checkpoint：
- checkpoint 保留每次 superstep 的历史状态快照（支持 time travel / 分支回放），
  但项目只读取「最新状态」；且 human_feedback 在节点消费后即被置空
  （nodes.py 中 return_state = {"human_feedback": None}），最新状态里没有历史意见；
- 历史快照是运行时状态，不是结构化、可直接查询的业务记录——要追溯每次意见
  得翻快照且依赖 checkpoint 存活，所以本库独立落盘为可查询的业务档案；
- SSE 事件历史只存在于内存（manager.py 的 ThreadChannel），完成后 1 小时清理，
  进程重启即丢失；
- 最终报告虽然也在 checkpoint 里，但为了历史功能不依赖 checkpoint 存活
  （换库/删 checkpoint 后仍可查看），完成时同步落库到本表。

存储位置：默认 data/history.sqlite（可经 HISTORY_DB 配置），独立文件，
避免与 langgraph 管理的 checkpoint.sqlite 混用。
"""
import json
import time
from typing import Any, Dict, List, Optional

import aiosqlite

# 调研状态取值
STATUS_RUNNING = "running"          # 正在执行/等待人工确认
STATUS_COMPLETED = "completed"      # 已完成，生成最终报告
STATUS_FAILED = "failed"            # 出错 / 意外终止
STATUS_INTERRUPTED = "interrupted"  # 上次进程退出时未完成（业务上不再自动续跑）


def _now() -> float:
    """当前 unix 时间戳。单独抽出便于测试替换。"""
    return time.time()


class ResearchHistory:
    """调研历史存储：aiosqlite 连接 + 建表，提供写入与查询接口。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """打开连接并建表；把上次进程遗留的 running 记录标记为 interrupted。"""
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_history (
                thread_id    TEXT PRIMARY KEY,
                topic        TEXT NOT NULL,
                status       TEXT NOT NULL,
                created_at   REAL NOT NULL,
                finished_at  REAL,
                final_report TEXT,
                feedbacks    TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        # 进程重启后，之前 running 的调研前端会话（SSE / thread_id）已断，
        # 业务上不再自动续跑，统一标记为 interrupted（checkpoint 仍在，
        # 可经 manager.restore() 重新接回继续）
        await self._conn.execute(
            "UPDATE research_history SET status = ? WHERE status = ?",
            (STATUS_INTERRUPTED, STATUS_RUNNING),
        )
        await self._conn.commit()

    async def close(self) -> None:
        """关闭连接（应用退出时释放）。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def create(self, thread_id: str, topic: str) -> None:
        """记录一次新调研（初始状态 running）。"""
        await self._conn.execute(
            "INSERT INTO research_history (thread_id, topic, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, topic, STATUS_RUNNING, _now()),
        )
        await self._conn.commit()

    async def add_feedback(
        self,
        thread_id: str,
        stage: Optional[str],
        approved: bool,
        feedback: Optional[str],
    ) -> None:
        """追加一条人工批改意见。

        stage 为人工介入阶段（plan / draft），取自 interrupt payload 的 type；
        feedback 为空表示直接批准。
        """
        row = await self._fetch(thread_id)
        if row is None:
            return
        feedbacks = json.loads(row["feedbacks"] or "[]")
        feedbacks.append(
            {
                "stage": stage or "unknown",
                "approved": bool(approved),
                "feedback": feedback,
                "at": _now(),
            }
        )
        await self._conn.execute(
            "UPDATE research_history SET feedbacks = ? WHERE thread_id = ?",
            (json.dumps(feedbacks, ensure_ascii=False), thread_id),
        )
        await self._conn.commit()

    async def finish(self, thread_id: str, final_report: str) -> None:
        """调研完成：写入最终报告全文并标记 completed。"""
        await self._conn.execute(
            "UPDATE research_history SET status = ?, finished_at = ?, final_report = ? "
            "WHERE thread_id = ?",
            (STATUS_COMPLETED, _now(), final_report, thread_id),
        )
        await self._conn.commit()

    async def fail(self, thread_id: str) -> None:
        """调研失败（error 事件 / 意外终止）。"""
        await self._conn.execute(
            "UPDATE research_history SET status = ?, finished_at = ? WHERE thread_id = ?",
            (STATUS_FAILED, _now(), thread_id),
        )
        await self._conn.commit()

    async def delete(self, thread_id: str) -> None:
        """删除一条调研历史记录。"""
        await self._conn.execute(
            "DELETE FROM research_history WHERE thread_id = ?", (thread_id,)
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def list_all(self) -> List[Dict[str, Any]]:
        """按创建时间倒序返回全部历史（不含 final_report 全文，列表页用）。"""
        cur = await self._conn.execute(
            "SELECT thread_id, topic, status, created_at, finished_at, feedbacks "
            "FROM research_history ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
        return [self._row_to_dict(r, with_report=False) for r in rows]

    async def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """返回单条历史详情（含 final_report 全文与全部批改意见）。"""
        row = await self._fetch(thread_id)
        if row is None:
            return None
        return self._row_to_dict(row, with_report=True)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _fetch(self, thread_id: str) -> Optional[aiosqlite.Row]:
        cur = await self._conn.execute(
            "SELECT thread_id, topic, status, created_at, finished_at, final_report, feedbacks "
            "FROM research_history WHERE thread_id = ?",
            (thread_id,),
        )
        return await cur.fetchone()

    def _row_to_dict(self, row: aiosqlite.Row, *, with_report: bool) -> Dict[str, Any]:
        """把行转为可 JSON 序列化的 dict，并解析 feedbacks JSON。"""
        data: Dict[str, Any] = {
            "thread_id": row["thread_id"],
            "topic": row["topic"],
            "status": row["status"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "feedbacks": json.loads(row["feedbacks"] or "[]"),
        }
        if with_report:
            data["final_report"] = row["final_report"]
        return data
