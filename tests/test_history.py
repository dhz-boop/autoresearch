"""调研历史存储（ResearchHistory）单元测试。

不依赖 LLM / 外部 API；使用 tmp_path 生成临时 SQLite 文件。
"""
import asyncio

from history import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    ResearchHistory,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 建表与状态
# ---------------------------------------------------------------------------
def test_connect_creates_table_and_marks_stale_running(tmp_path):
    """connect 应建表，并把上次遗留的 running 记录标记为 interrupted。"""
    db = tmp_path / "history.sqlite"
    h = ResearchHistory(str(db))

    async def _check():
        await h.connect()
        # 模拟上次进程遗留的 running 记录
        await h._conn.execute(
            "INSERT INTO research_history (thread_id, topic, status, created_at) "
            "VALUES ('t-stale', '旧主题', 'running', 1.0)"
        )
        await h._conn.commit()
        # 重新 connect（模拟进程重启）→ 遗留 running 应被标记为 interrupted
        await h.close()
        await h.connect()
        rows = await h.list_all()
        assert rows[0]["thread_id"] == "t-stale"
        assert rows[0]["status"] == STATUS_INTERRUPTED
        await h.close()

    _run(_check())


# ---------------------------------------------------------------------------
# 写入：create / add_feedback / finish / fail
# ---------------------------------------------------------------------------
def test_create_list_and_get(tmp_path):
    db = tmp_path / "history.sqlite"
    h = ResearchHistory(str(db))

    async def _check():
        await h.connect()
        await h.create("t1", "咖啡机市场")
        await h.create("t2", "新能源车市场")

        rows = await h.list_all()  # 倒序：t2 在前
        assert len(rows) == 2
        assert rows[0]["thread_id"] == "t2"
        assert rows[0]["status"] == STATUS_RUNNING
        assert rows[0]["feedbacks"] == []
        assert "final_report" not in rows[0], "列表接口不应返回报告全文"

        detail = await h.get("t1")
        assert detail["topic"] == "咖啡机市场"
        assert detail["final_report"] is None
        await h.close()

    _run(_check())


def test_add_feedback_appends_and_parses_json(tmp_path):
    db = tmp_path / "history.sqlite"
    h = ResearchHistory(str(db))

    async def _check():
        await h.connect()
        await h.create("t1", "咖啡机市场")
        await h.add_feedback("t1", "plan", False, "请补充海外市场对比章节")
        await h.add_feedback("t1", "plan", True, None)
        await h.add_feedback("t1", "draft", True, None)

        detail = await h.get("t1")
        assert len(detail["feedbacks"]) == 3
        fb = detail["feedbacks"][0]
        assert fb["stage"] == "plan"
        assert fb["approved"] is False
        assert fb["feedback"] == "请补充海外市场对比章节"
        assert fb["at"] > 0
        # 批准项 feedback 为 None（前端不填意见时）
        assert detail["feedbacks"][1]["approved"] is True
        assert detail["feedbacks"][1]["feedback"] is None
        await h.close()

    _run(_check())


def test_add_feedback_unknown_thread_is_noop(tmp_path):
    """对不存在的 thread_id 追加意见应安全无副作用（不抛异常）。"""
    db = tmp_path / "history.sqlite"
    h = ResearchHistory(str(db))

    async def _check():
        await h.connect()
        await h.add_feedback("nope", "plan", False, "意见")  # 不应抛
        rows = await h.list_all()
        assert rows == []
        await h.close()

    _run(_check())


def test_finish_writes_report_and_completed(tmp_path):
    db = tmp_path / "history.sqlite"
    h = ResearchHistory(str(db))

    async def _check():
        await h.connect()
        await h.create("t1", "咖啡机市场")
        report = "# 2026年咖啡机市场\n## 市场规模\n约 50 亿元"
        await h.finish("t1", report)

        rows = await h.list_all()
        assert rows[0]["status"] == STATUS_COMPLETED
        assert rows[0]["finished_at"] is not None
        detail = await h.get("t1")
        assert detail["status"] == STATUS_COMPLETED
        assert detail["final_report"] == report
        await h.close()

    _run(_check())


def test_fail_marks_failed(tmp_path):
    db = tmp_path / "history.sqlite"
    h = ResearchHistory(str(db))

    async def _check():
        await h.connect()
        await h.create("t1", "咖啡机市场")
        await h.fail("t1")
        rows = await h.list_all()
        assert rows[0]["status"] == STATUS_FAILED
        assert rows[0]["finished_at"] is not None
        await h.close()

    _run(_check())


def test_delete_removes_record(tmp_path):
    """delete 应移除记录，删除后再查询返回 None / 空列表。"""
    db = tmp_path / "history.sqlite"
    h = ResearchHistory(str(db))

    async def _check():
        await h.connect()
        await h.create("t1", "咖啡机市场")
        await h.create("t2", "新能源车市场")
        await h.delete("t1")
        assert await h.get("t1") is None
        rows = await h.list_all()
        assert [r["thread_id"] for r in rows] == ["t2"]
        # 删除不存在的记录应安全（不抛异常）
        await h.delete("nope")
        assert len(await h.list_all()) == 1
        await h.close()

    _run(_check())
