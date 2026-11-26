"""Parallel AI 搜索与正文提取（https://parallel.ai）

通过官方 Streamable HTTP MCP 接入：
    https://search.parallel.ai/mcp
免注册、免 key 可用（Fast 模式，有限流）；注册 PARALLEL_API_KEY 后额度更高。
官方说明：匿名 MCP 免费；账号每月有免费额度，Turbo/Fast 最多约 5,000 次/月。

这里用普通 HTTP 客户端直接发 tools/call，不引入额外 SDK。
"""
from __future__ import annotations

import json
import uuid

import httpx

from ..config import SEARCH_TIMEOUT
from .base import (
    NETWORK_ERROR,
    FetchResult,
    ProviderError,
    SearchResult,
    as_items,
    brief,
    business_error,
    decode_mcp_response,
    expect_dict,
    fetch_error,
    text_of,
)

MCP_URL = "https://search.parallel.ai/mcp"
_SESSION_ID = uuid.uuid4().hex
_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

# 实测（2026-11）服务端是无状态的：不握手直接发 tools/call 就能拿到结果，一次往返即可。
# 以前每次搜索都先 initialize 再 notifications/initialized，白白多两次往返。
# 万一以后服务端改成必须先握手，这里会自动退回完整握手并记住，不用改代码。
_handshake_required = False


def _headers(key: str) -> dict:
    headers = dict(_HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def _handshake(client: httpx.AsyncClient, headers: dict) -> dict:
    """完整握手，返回带会话 ID 的请求头。只在服务端拒绝直接调用时才走这里。"""
    headers = dict(headers)
    response = await client.post(
        MCP_URL,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "aggregate-search", "version": "0.4"},
            },
        },
        timeout=SEARCH_TIMEOUT,
    )
    decode_mcp_response(response)
    session = response.headers.get("mcp-session-id")
    if session:
        headers["Mcp-Session-Id"] = session
    notify = await client.post(
        MCP_URL,
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=SEARCH_TIMEOUT,
    )
    if notify.status_code not in (200, 202, 204):
        raise ProviderError(f"MCP initialized 通知失败: HTTP {notify.status_code}")
    return headers


def _wants_handshake(exc: ProviderError) -> bool:
    """这个错误是不是在说「你还没握手 / 会话无效」。鉴权和限流错误不算。"""
    if exc.status in (401, 402, 403, 429):
        return False
    text = str(exc).lower()
    return exc.status in (400, 404, 405) or any(
        hint in text for hint in ("session", "initializ")
    )


async def _post_call(client: httpx.AsyncClient, headers: dict, name: str, arguments: dict) -> dict:
    response = await client.post(
        MCP_URL,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        timeout=SEARCH_TIMEOUT,
    )
    return decode_mcp_response(response)


async def _call_tool(client: httpx.AsyncClient, name: str, arguments: dict, key: str = "") -> dict:
    global _handshake_required
    headers = _headers(key)
    try:
        result = None
        if not _handshake_required:
            try:
                result = await _post_call(client, headers, name, arguments)
            except ProviderError as exc:
                if not _wants_handshake(exc):
                    raise
        if result is None:
            result = await _post_call(client, await _handshake(client, headers), name, arguments)
            # 握手之后真的成功了才记住。否则一次普通的 400（比如参数错误）
            # 就会让这个进程此后每次调用都多跑两次往返。
            _handshake_required = True
    except httpx.HTTPError as exc:
        raise ProviderError(f"{NETWORK_ERROR}: {type(exc).__name__}: {exc}") from exc
    if result.get("isError"):
        message = str(result.get("content") or result)
        raise business_error(message) if name == "web_search" else fetch_error(message)
    return result


def _tool_json(result: dict) -> dict:
    content = result.get("content") or []
    if not isinstance(content, list):
        raise ProviderError("Parallel 工具响应格式异常")
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            raw = item.get("text")
            if raw is not None and not isinstance(raw, str):
                raise ProviderError("Parallel 工具内容不是文本")
            try:
                return expect_dict(json.loads(raw or "{}"), "parallel")
            except ValueError as exc:
                raise ProviderError("Parallel 工具内容不是 JSON") from exc
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    raise ProviderError("Parallel 工具没有返回文本内容")


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    # MCP 版没有 max_results 参数，返回后本地截断。
    raw = await _call_tool(
        client,
        "web_search",
        {"objective": query, "search_queries": [query], "session_id": _SESSION_ID},
        key,
    )
    data = _tool_json(raw)
    results: list[SearchResult] = []
    for item in as_items(data, "results"):
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        excerpts = item.get("excerpts") or []
        if not isinstance(excerpts, list):
            excerpts = []
        snippet = "\n".join(str(part) for part in excerpts[:2])[:900]
        results.append(
            SearchResult(
                source="parallel",
                url=url,
                title=str(item.get("title") or "").strip(),
                snippet=snippet,
                rank=len(results),
                published=str(item.get("publish_date") or item.get("published_date") or ""),
            )
        )
        if len(results) >= max_results:
            break
    return results


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    raw = await _call_tool(
        client,
        "web_fetch",
        {"urls": [url], "full_content": True, "session_id": _SESSION_ID},
        key,
    )
    data = _tool_json(raw)
    items = as_items(data, "results") or as_items(data, "pages")
    if not items:
        raise ProviderError(f"Parallel 未返回正文: {brief(data)}")
    item = items[0]
    content = item.get("content") or item.get("markdown") or item.get("full_content") or ""
    if isinstance(content, list):
        content = "\n".join(part for part in (text_of(part) for part in content) if part)
    return FetchResult(
        provider="parallel",
        url=url,
        title=text_of(item.get("title")),
        content=text_of(content),
    )
