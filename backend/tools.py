"""工具封装模块：供各节点调用的搜索与分析工具。

- search_web:    聚合国内外多源联网搜索（Tavily 国外源 + 博查国内源），
  按 URL 去重后返回统一格式；两者均未配置 key 时返回模拟结果。
- tavily_search: 仅 Tavily 源（保留，供单源/测试使用）。
- python_repl:   基于标准库 code.InteractiveInterpreter 的 Python 执行器，
  供 analyst 做简单数据分析/计算（替代已停维护的 langchain-experimental）。

工具均用 LangChain 的 @tool 装饰器定义，保留绑定到 Agent 的能力。
"""
import ast
import builtins
import io
import sys
import traceback
from datetime import datetime
from typing import List

from langchain_core.tools import tool

from config import get_settings
from models import SearchResult

# 允许 REPL 使用的受限内置命名空间（拦截明显危险的内置对象）
_SAFE_BUILTINS = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "chr", "complex", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "hex", "int", "isinstance", "issubclass",
    "iter", "len", "list", "map", "max", "min", "next", "oct", "ord",
    "pow", "print", "range", "repr", "reversed", "round", "set", "slice",
    "sorted", "str", "sum", "tuple", "zip",
}


def _validate_sandbox_code(code: str) -> None:
    """静态校验 REPL 输入，拦截 import 语句与 dunder 属性访问。

    背景：exec 环境的受限 builtins 能拦截 import，但无法阻止对对象
    属性链的反射访问（经典逃逸：`().__class__.__bases__[0].__subclasses__()`，
    进而通过 `catch_warnings.__init__.__globals__["sys"]` 拿回完整模块）。
    因此在执行前用 AST 检查做第二道防线：
    - 禁止任何 import 语句；
    - 禁止访问任何 dunder 属性（`__class__` / `__subclasses__` / `__globals__` / `__init__` 等）。

    注意：这是安全加固而非完美隔离，仅应在受信任的分析场景使用。
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        raise ValueError(f"代码语法错误：{e}") from e

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("REPL 禁止 import 语句")
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if len(attr) >= 4 and attr.startswith("__") and attr.endswith("__"):
                raise ValueError(f"REPL 禁止访问 dunder 属性：{attr}")


def _now_str() -> str:
    """当前日期时间的中文可读字符串，如 2026年8月10日（周一）14:30。"""
    weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    now = datetime.now()
    return now.strftime(f"%Y年%m月%d日（{weekdays[now.weekday()]}）%H:%M")


@tool
def get_current_datetime() -> str:
    """获取当前日期与时间（本地时区）。

    用于让 LLM 明确知道「现在」是哪一天，从而在检索与撰写时
    优先采用最新信息、避免引用过时数据。

    Returns:
        当前时间的可读字符串，如 2026年8月10日（周一）14:30。
    """
    return _now_str()


def _mock_search(query: str, max_results: int) -> List[dict]:
    """所有真实搜索源均未配置/全部失败时返回模拟结果，保证图流程可本地跑通。

    结果标题带 [MOCK] 前缀，便于识别并非真实搜索数据。
    """
    return [
        SearchResult(
            title=f"[MOCK] 关于「{query}」的模拟结果 {i}",
            url=f"https://mock.example/{i}",
            content=f"（模拟数据）第 {i} 条摘要：这是关于「{query}」的占位内容，"
                    f"用于未配置搜索 Key 时的本地联调。",
            score=round(0.9 - i * 0.1, 2),
            query=query,
            source="mock",
        ).model_dump()
        for i in range(1, max_results + 1)
    ]


def _tavily_search(query: str, max_results: int) -> List[dict]:
    """Tavily（国外源）真实搜索，返回统一格式结果列表。"""
    settings = get_settings()
    # 官方包按需延迟导入，避免无密钥环境安装失败
    from tavily import TavilyClient

    client = TavilyClient(api_key=settings.tavily_api_key)
    # language="zh-CN"：让英文索引也能尽量返回中文相关结果，配合博查覆盖中文
    resp = client.search(
        query=query,
        max_results=max_results,
        search_depth="basic",
        language="zh-CN",
    )
    results = []
    for item in resp.get("results", []):
        results.append(
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                content=item.get("content", ""),
                score=item.get("score"),
                query=query,
                source="tavily",
            ).model_dump()
        )
    return results


def _bocha_search(query: str, max_results: int) -> List[dict]:
    """博查（国内源）真实搜索，返回统一格式结果列表。

    接口：POST {base_url}，Header Authorization: Bearer {key}，
    响应网页结果位于 data.webPages.value（兼容 Bing Search API 结构）。
    """
    import httpx

    settings = get_settings()
    resp = httpx.post(
        settings.bocha_base_url,
        headers={
            "Authorization": f"Bearer {settings.bocha_api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query, "count": max_results, "summary": True},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    pages = data.get("data", {}).get("webPages", {}).get("value", []) or []
    results = []
    for item in pages:
        results.append(
            SearchResult(
                title=item.get("name", ""),
                url=item.get("url", ""),
                content=item.get("summary") or item.get("snippet") or "",
                score=None,
                query=query,
                source="bocha",
            ).model_dump()
        )
    return results


@tool
def search_web(query: str, max_results: int = 5) -> List[dict]:
    """聚合国内外多源联网搜索（Tavily 国外源 + 博查国内源）。

    配置了哪些 key 就启用哪些源，按 URL 去重后返回统一格式
    [{title, url, content, score, query, source}]。某源失败只跳过该源；
    所有源均未配置或全部失败时返回 [MOCK] 模拟结果，保证流程不中断。

    Args:
        query: 搜索关键词（中文亦可）。
        max_results: 返回的最大结果数（每源），默认 5。

    Returns:
        搜索结果列表，每个元素为可 JSON 序列化的 dict。
    """
    settings = get_settings()
    results: List[dict] = []
    errors: List[str] = []

    if settings.tavily_api_key:
        try:
            results += _tavily_search(query, max_results)
        except Exception as e:  # noqa: BLE001 - 单源失败不阻断整体
            errors.append(f"tavily: {e}")
    if settings.bocha_api_key:
        try:
            results += _bocha_search(query, max_results)
        except Exception as e:  # noqa: BLE001
            errors.append(f"bocha: {e}")

    # 按 URL 去重（Tavily 与博查可能命中同一页面）
    seen = set()
    deduped: List[dict] = []
    for r in results:
        url = r.get("url") or ""
        if url in seen:
            continue
        seen.add(url)
        deduped.append(r)

    if not deduped:
        if errors:
            print(f"[search_web] 多源搜索全部失败，回退 mock: {'; '.join(errors)}")
        return _mock_search(query, max_results)
    return deduped


@tool
def tavily_search(query: str, max_results: int = 5) -> List[dict]:
    """仅使用 Tavily 搜索引擎检索互联网（单一国外源，保留用于测试）。

    配置了 TAVILY_API_KEY 时真实搜索；未配置时返回 [MOCK] 模拟结果。
    """
    settings = get_settings()
    if not settings.tavily_api_key:
        return _mock_search(query, max_results)
    return _tavily_search(query, max_results)


@tool
def python_repl(code: str) -> str:
    """在隔离的 Python 解释器中执行分析代码，返回 stdout/stderr 文本。

    Args:
        code: 一段合法的 Python 语句/表达式，例如数据统计、对比计算。

    Returns:
        执行产生的标准输出；若抛出异常则返回错误堆栈。

    注意：此工具会执行任意 Python 代码，仅应在受信任的分析场景使用。
    """
    output = io.StringIO()
    # 重定向 stdout/stderr 以捕获运行输出
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = output, output

    # 构建受限解释器环境：仅暴露安全内置，禁止 import 等危险操作
    restricted_globals = {
        "__builtins__": {name: getattr(builtins, name) for name in _SAFE_BUILTINS},
        "__name__": "__main__",
    }

    try:
        # 先做 AST 静态校验（拦截 import / dunder 属性访问，阻断沙箱逃逸）
        _validate_sandbox_code(code)
        # exec 以 "exec" 模式编译，支持多行/多语句分析代码
        exec(compile(code, "<repl>", "exec"), restricted_globals)
    except Exception:  # noqa: BLE001 - 需捕获所有异常以给出友好提示
        traceback.print_exc(file=output)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    return output.getvalue().strip() or "(无输出)"
