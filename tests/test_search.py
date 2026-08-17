"""双源搜索（search_web：Tavily 国外 + 博查国内）单元测试。

不依赖真实 API key 或网络：通过 monkeypatch 控制配置与搜索源实现。
"""
import httpx
import pytest

import tools
from config import get_settings


def _fake_tavily(query, max_results):
    return [
        {
            "title": "Tavily结果",
            "url": "https://example.com/a",
            "content": "tavily 摘要",
            "score": 0.9,
            "query": query,
            "source": "tavily",
        }
    ]


def _fake_bocha(query, max_results):
    return [
        {
            "title": "博查结果",
            "url": "https://example.com/a",  # 与 tavily 相同 url，用于去重
            "content": "bocha 摘要",
            "query": query,
            "source": "bocha",
        }
    ]


def _set_keys(monkeypatch, tavily=True, bocha=True):
    s = get_settings()
    monkeypatch.setattr(s, "tavily_api_key", "x" if tavily else "")
    monkeypatch.setattr(s, "bocha_api_key", "x" if bocha else "")


# ---------------------------------------------------------------------------
# search_web 聚合行为
# ---------------------------------------------------------------------------
def test_search_web_mock_when_no_keys(monkeypatch):
    """Tavily 与博查都未配置时，search_web 返回 [MOCK] 模拟结果。"""
    _set_keys(monkeypatch, tavily=False, bocha=False)
    res = tools.search_web.invoke({"query": "咖啡机市场", "max_results": 3})
    assert len(res) == 3
    assert res[0]["title"].startswith("[MOCK]")
    assert res[0]["source"] == "mock"


def test_search_web_merges_and_dedups(monkeypatch):
    """两个源结果合并，并按 URL 去重。"""
    _set_keys(monkeypatch)
    monkeypatch.setattr(tools, "_tavily_search", _fake_tavily)
    monkeypatch.setattr(tools, "_bocha_search", _fake_bocha)
    res = tools.search_web.invoke({"query": "咖啡机市场", "max_results": 5})
    # 两源命中同一 url → 去重后仅 1 条，且保留先到（tavily）的
    assert len(res) == 1
    assert res[0]["source"] == "tavily"


def test_search_web_skips_failed_source(monkeypatch):
    """某源抛异常只跳过该源，不影响另一源的结果。"""
    _set_keys(monkeypatch)

    def boom(query, max_results):
        raise RuntimeError("api down")

    monkeypatch.setattr(tools, "_tavily_search", boom)
    monkeypatch.setattr(tools, "_bocha_search", _fake_bocha)
    res = tools.search_web.invoke({"query": "咖啡机市场", "max_results": 5})
    assert len(res) == 1
    assert res[0]["source"] == "bocha"


def test_search_web_all_failed_falls_back_to_mock(monkeypatch):
    """所有源都失败时回退 mock，保证流程不中断。"""
    _set_keys(monkeypatch)

    def boom(query, max_results):
        raise RuntimeError("down")

    monkeypatch.setattr(tools, "_tavily_search", boom)
    monkeypatch.setattr(tools, "_bocha_search", boom)
    res = tools.search_web.invoke({"query": "咖啡机市场", "max_results": 3})
    assert res[0]["title"].startswith("[MOCK]")


# ---------------------------------------------------------------------------
# 博查源解析
# ---------------------------------------------------------------------------
def test_bocha_search_parses_response(monkeypatch):
    """博查响应（data.webPages.value）应被正确解析为统一格式。"""
    _set_keys(monkeypatch, tavily=False, bocha=True)

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": "标题A",
                                "url": "https://b.cn/1",
                                "snippet": "短摘要",
                                "summary": "长摘要内容",
                            },
                            {
                                "name": "标题B",
                                "url": "https://b.cn/2",
                                "snippet": "只有短摘要",
                            },
                        ]
                    }
                }
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    res = tools._bocha_search("咖啡机市场", 5)
    assert len(res) == 2
    assert res[0]["title"] == "标题A"
    assert res[0]["content"] == "长摘要内容"  # summary 优先于 snippet
    assert res[1]["content"] == "只有短摘要"  # 无 summary 时回退 snippet
    assert res[0]["source"] == "bocha"
    assert res[0]["query"] == "咖啡机市场"
