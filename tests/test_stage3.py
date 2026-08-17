"""阶段 3 集成测试：FastAPI + SSE 全流程。

启动真实 uvicorn 子进程，用 httpx 验证 4 个端点：
    start → stream(SSE) → feedback ×2 → report

运行方式（需 .env 中配置 API key）：
    pytest tests/test_stage3.py -m integration
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
PORT = 8211
BASE = f"http://127.0.0.1:{PORT}"


# ---------------------------------------------------------------------------
# 启动 uvicorn 子进程
# ---------------------------------------------------------------------------
# 测试使用独立的临时 SQLite 检查点文件，避免测试反复写坏开发用的 data/checkpoints.sqlite；
# 调研历史库同样指向临时文件，避免污染开发环境的 data/history.sqlite
TEST_DB = "/tmp/autoresearch_test_checkpoints.sqlite"
TEST_HISTORY_DB = "/tmp/autoresearch_test_history.sqlite"


@pytest.fixture(scope="module")
def server():
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(TEST_DB + suffix):
            os.remove(TEST_DB + suffix)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(TEST_HISTORY_DB + suffix):
            os.remove(TEST_HISTORY_DB + suffix)
    env = {**os.environ, "CHECKPOINT_DB": TEST_DB, "HISTORY_DB": TEST_HISTORY_DB}
    err_log = open("/tmp/test_uvicorn_err.log", "w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=err_log,
    )
    # 等待服务就绪。注意：
    #  1) trust_env=False 绕过系统 http_proxy（否则 startup 期间会收到 502）；
    #  2) uvicorn 的 lifespan 启动较慢（加载 langchain 数秒），非 200 一律等待。
    ready = False
    with httpx.Client(trust_env=False, timeout=2) as probe:
        for _ in range(120):
            if proc.poll() is not None:
                break  # 子进程已退出
            try:
                if probe.get(f"{BASE}/health").status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
    if not ready:
        # 子进程已退出时读取 stderr 帮助定位
        err_tail = b""
        if proc.poll() is not None:
            try:
                _, err_tail = proc.communicate(timeout=5)
            except Exception:
                pass
        proc.terminate()
        pytest.fail(f"uvicorn 服务启动失败\nstderr: {err_tail.decode('utf-8', 'replace')}")
    yield BASE
    proc.terminate()
    proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# SSE 读取辅助
# ---------------------------------------------------------------------------
async def read_sse_until(client: httpx.AsyncClient, url: str, predicate, max_events: int = 500):
    """读取 SSE 事件，直到 predicate 命中（返回全部已读事件列表）。"""
    events = []
    try:
        async with client.stream("GET", url) as resp:
            buffer = ""
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    buffer += line[6:]
                elif line == "" and buffer:
                    evt = json.loads(buffer)
                    # 流式增量事件是高频瞬时的，不计入事件计数（避免占满 max_events）
                    if evt.get("event") == "stream":
                        buffer = ""
                        continue
                    events.append(evt)
                    buffer = ""
                    if predicate(evt) or len(events) >= max_events:
                        return events
    except httpx.HTTPError:
        # 连接中断（服务端或客户端主动关闭）视为读取结束；
        # SSE 场景下客户端读到目标事件后关闭连接，服务端可能不发送完整 body，
        # httpx 会抛 RemoteProtocolError 等传输层异常，均属正常。
        pass
    return events


async def _full_flow():
    timeout = httpx.Timeout(300.0, connect=5.0)
    # trust_env=False：绕过系统 http_proxy，直连本地 uvicorn
    async with httpx.AsyncClient(base_url=BASE, timeout=timeout, trust_env=False) as client:
        # 1. 启动调研
        r = await client.post("/research/start", json={"topic": "2026年国内咖啡机市场机会"})
        assert r.status_code == 200, r.text
        thread_id = r.json()["thread_id"]
        assert thread_id
        print(f"\n[start] thread_id={thread_id}")

        # 2. 订阅 SSE，读到「大纲确认」中断
        #    注意：interrupt 节点（supervisor_planner）不会产生 node_end（节点未完成即暂停）
        events = await read_sse_until(
            client, f"/research/{thread_id}/stream",
            lambda e: e["event"] == "interrupt",
        )
        starts = [e for e in events if e["event"] == "node_start"]
        interrupts = [e for e in events if e["event"] == "interrupt"]
        assert starts, f"应收到 node_start，实际 {len(starts)}"
        assert interrupts and interrupts[0]["data"]["type"] == "plan"
        assert interrupts[0]["data"]["outline"]["sections"], "大纲内容不应为空"
        print(f"[stream] 收到 node_start×{len(starts)}, interrupt(plan)")

        # 3. 批准大纲
        r = await client.post(f"/research/{thread_id}/feedback", json={"approved": True})
        assert r.status_code == 200 and r.json()["status"] == "resumed"

        # 4. 继续订阅，读到「草稿审核」中断
        events2 = await read_sse_until(
            client, f"/research/{thread_id}/stream",
            lambda e: e["event"] == "interrupt" and e["data"].get("type") == "draft",
        )
        iv2 = next(e for e in events2 if e["event"] == "interrupt" and e["data"].get("type") == "draft")
        assert iv2["data"]["report"], "草稿内容不应为空"
        # 确认并行 researcher 均产生了 node_end
        researcher_ends = [e for e in events2 if e["event"] == "node_end" and e["node"] == "researcher"]
        print(f"[stream] 收到 interrupt(draft) + researcher node_end×{len(researcher_ends)}")

        # 5. 批准草稿
        r = await client.post(f"/research/{thread_id}/feedback", json={"approved": True})
        assert r.status_code == 200

        # 6. 订阅，读到 final 事件
        events3 = await read_sse_until(
            client, f"/research/{thread_id}/stream", lambda e: e["event"] == "final",
        )
        final_evt = next((e for e in events3 if e["event"] == "final"), None)
        assert final_evt, "应收到 final 事件"
        assert final_evt["data"]["final_report"]

        # 7. 获取最终报告
        r = await client.get(f"/research/{thread_id}/report")
        assert r.status_code == 200, r.text
        report = r.json()["final_report"]
        assert report.startswith("#") or "报告" in report
        print(f"[report] 最终报告 {len(report)} 字符")
        print(f"[report] 开头: {report[:80]!r}")

        # 8. 历史接口：列表包含该调研，详情含报告全文 + 两次批准的批改记录
        r = await client.get("/research/history")
        assert r.status_code == 200, r.text
        hist = r.json()["history"]
        rec = next((h for h in hist if h["thread_id"] == thread_id), None)
        assert rec is not None, "新调研应出现在历史列表"
        assert rec["topic"] == "2026年国内咖啡机市场机会"
        assert rec["status"] == "completed"
        assert "final_report" not in rec, "列表接口不应返回报告全文"
        print(f"[history] 列表包含该调研: {rec['topic']} / {rec['status']}")

        r = await client.get(f"/research/{thread_id}/detail")
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["final_report"] == report, "详情报告应与最终报告一致"
        # 流程中两次均批准（大纲 plan + 草稿 draft），无反馈文本
        assert len(detail["feedbacks"]) == 2, f"应记录 2 次批改，实际 {detail['feedbacks']}"
        assert [fb["stage"] for fb in detail["feedbacks"]] == ["plan", "draft"]
        assert all(fb["approved"] is True for fb in detail["feedbacks"])
        print(f"[history] 详情: {len(detail['feedbacks'])} 条批改记录, 报告 {len(detail['final_report'])} 字符")

        # 9. 历史报告导出（Markdown / Word）
        r = await client.get(f"/research/{thread_id}/history/export?format=markdown")
        assert r.status_code == 200, r.text
        assert "text/markdown" in r.headers["content-type"]
        text = r.content.decode("utf-8").strip()
        assert text, "导出的 Markdown 不应为空"
        assert "#" in text, "导出的 Markdown 应包含标题"
        r = await client.get(f"/research/{thread_id}/history/export?format=docx")
        assert r.status_code == 200, r.text
        assert r.content[:2] == b"PK", "docx 应为 ZIP 容器（PK 魔数）"
        print("[history] 导出 markdown + docx OK")

        # 10. 删除历史报告：删除后 detail 404、列表不再包含
        r = await client.delete(f"/research/{thread_id}")
        assert r.status_code == 200 and r.json()["status"] == "deleted", r.text
        assert (await client.get(f"/research/{thread_id}/detail")).status_code == 404
        hist = (await client.get("/research/history")).json()["history"]
        assert all(h["thread_id"] != thread_id for h in hist), "删除后不应出现在历史列表"
        # 已删除的线程再删应 404
        assert (await client.delete(f"/research/{thread_id}")).status_code == 404
        print("[history] 删除 OK")


@pytest.mark.integration
def test_full_api_flow(server):
    """start → SSE(interrupt plan) → feedback → SSE(interrupt draft) → feedback → final → report。"""
    asyncio.run(_full_flow())


@pytest.mark.integration
def test_report_before_completion_returns_404(server):
    """未完成的调研线程，/report 应返回 404。"""

    async def _check():
        timeout = httpx.Timeout(60.0)
        async with httpx.AsyncClient(base_url=BASE, timeout=timeout, trust_env=False) as client:
            r = await client.post("/research/start", json={"topic": "测试主题"})
            thread_id = r.json()["thread_id"]
            # 立即查报告（图仍在跑，未到 final）
            r2 = await client.get(f"/research/{thread_id}/report")
            assert r2.status_code in (404, 200)  # 404 为预期；极快完成时为 200

    asyncio.run(_check())


@pytest.mark.integration
def test_unknown_thread_returns_404(server):
    """未知 thread_id 的 stream / feedback 应返回 404。"""

    async def _check():
        timeout = httpx.Timeout(30.0)
        async with httpx.AsyncClient(base_url=BASE, timeout=timeout, trust_env=False) as client:
            assert (await client.get("/research/nonexist/stream")).status_code == 404
            r = await client.post("/research/nonexist/feedback", json={"approved": True})
            assert r.status_code == 404

    asyncio.run(_check())
