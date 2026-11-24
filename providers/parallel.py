"""Parallel AI 搜索与正文提取（https://parallel.ai）

通过官方 Streamable HTTP MCP 接入：
    https://search.parallel.ai/mcp
免注册、免 key 可用（Fast 模式，有限流）；注册 PARALLEL_API_KEY 后额度更高。
官方说明：匿名 MCP 免费；账号每月有免费额度，Turbo/Fast 最多约 5,000 次/月。

这里用普通 HTTP 客户端实现最小 MCP 会话，不引入额外 SDK。
"""
from __future__ import annotations

import json
import uuid

import httpx

from config import SEARCH_TIMEOUT
from providers.base import (
    FetchResult,
    ProviderError,
    SearchResult,
    as_items,
    decode_mcp_response,
    expect_dict,
)

MCP_URL = "https://search.parallel.ai/mcp"
_SESSION_ID = uuid.uuid4().hex
_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


async def _initialize(client: httpx.AsyncClient) -> dict:
    headers = dict(_HEADERS)
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
                "clientInfo": {"name": "aggregate-search", "version": "0.3"},
            },
        },
        timeout=SEARCH_TIMEOUT,
    )
    result = decode_mcp_response(response)
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


async def _call_tool(client: httpx.AsyncClient, name: str, arguments: dict, key: str = "") -> dict:
    try:
        headers = await _initialize(client)
        if key:
            headers["Authorization"] = f"Bearer {key}"
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
    except httpx.HTTPError as exc:
        raise ProviderError(f"网络错误: {type(exc).__name__}: {exc}") from exc
    result = decode_mcp_response(response)
    if result.get("isError"):
        raise ProviderError(str(result.get("content") or result)[:300], retryable=False)
    return result


def _tool_json(result: dict) -> dict:
    content = result.get("content") or []
    if not isinstance(content, list):
        raise ProviderError("Parallel 工具响应格式异常")
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                return expect_dict(json.loads(item.get("text") or "{}"), "parallel")
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
        raise ProviderError(f"Parallel 未返回正文: {str(data)[:200]}")
    item = items[0]
    content = item.get("content") or item.get("markdown") or item.get("full_content") or ""
    if isinstance(content, list):
        content = "\n".join(str(part) for part in content)
    return FetchResult(
        provider="parallel",
        url=url,
        title=str(item.get("title") or "").strip(),
        content=str(content).strip(),
    )
