"""Keenable 匿名/密钥搜索与正文抓取（https://keenable.ai）。"""
from __future__ import annotations

import re

import httpx

from ..config import get_settings
from .base import (
    NETWORK_ERROR,
    FetchResult,
    ProviderError,
    SearchResult,
    brief,
    business_error,
    decode_mcp_response,
    fetch_error,
    strict_items,
    text_of,
)

MCP_URL = "https://api.keenable.ai/mcp"
_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def _call(client: httpx.AsyncClient, key: str, tool: str, arguments: dict) -> dict:
    headers = dict(_HEADERS)
    if key:
        headers["X-API-Key"] = key
    try:
        response = await client.post(
            MCP_URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            timeout=max(get_settings().SEARCH_TIMEOUT, 25),
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"{NETWORK_ERROR}: {type(exc).__name__}: {exc}") from exc
    result = decode_mcp_response(response)
    content = result.get("content") or []
    if not isinstance(content, list):
        raise ProviderError("keenable content 不是列表")
    text = "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )
    if result.get("isError"):
        raise business_error(text) if tool == "search_web_pages" else fetch_error(text)
    structured = result.get("structuredContent")
    if structured is not None and not isinstance(structured, dict):
        raise ProviderError("keenable structuredContent 不是对象")
    return {"structured": structured, "text": text}


def _parse_structured(payload: dict, max_results: int) -> list[SearchResult]:
    structured = payload.get("structured")
    if structured is None:
        return []
    items = strict_items(structured, "keenable", "results")
    results = []
    for item in items:
        snippets = item.get("snippets") or item.get("excerpts") or []
        if isinstance(snippets, str):
            snippets = [snippets]
        elif not isinstance(snippets, list):
            snippets = []
        results.append(SearchResult(
            source="keenable",
            url=text_of(item.get("url")),
            title=text_of(item.get("title")),
            snippet="\n".join(part for part in (text_of(x) for x in snippets[:3]) if part)[:1200],
            rank=len(results),
            published=text_of(item.get("published") or item.get("published_at")),
        ))
        if len(results) >= max_results:
            break
    return results


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    payload = await _call(client, key, "search_web_pages", {
        "query": query,
        "max_results": max_results,
        "snippet_max_length": 1200,
    })
    structured = _parse_structured(payload, max_results)
    if structured:
        return structured

    text = payload["text"].replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() and payload.get("structured") is not None:
        return []  # 明确的结构化空结果
    results: list[SearchResult] = []
    for block in re.split(r"\n(?=Title:\s*)", text):
        title = re.search(r"^Title:\s*(.+)$", block, re.M)
        url = re.search(r"^URL:\s*(\S+)\s*$", block, re.M)
        if not url:
            continue
        published = re.search(r"^Published:\s*(.+)$", block, re.M)
        marker = re.search(r"^Snippets:\s*$", block, re.M)
        snippet = block[marker.end():] if marker else ""
        snippet = re.sub(r"\n---\s*$", "", snippet).strip()
        results.append(SearchResult(
            source="keenable",
            url=url.group(1).strip(),
            title=title.group(1).strip() if title else "",
            snippet=snippet[:1200],
            rank=len(results),
            published=published.group(1).strip() if published else "",
        ))
        if len(results) >= max_results:
            break
    if text.strip() and not results:
        raise ProviderError(f"keenable 非空响应无法解析: {brief(text, 160)}")
    return results


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    payload = await _call(client, key, "fetch_page_content", {
        "urls": [url], "max_chars": get_settings().FETCH_CACHE_MAX_CHARS, "live": True,
    })
    text = payload["text"]
    structured = payload.get("structured") or {}
    title = text_of(structured.get("title"))
    content = text_of(structured.get("content")) or text.strip()
    match = re.search(r"^Title:\s*(.+)$", text, re.M)
    if match and not title:
        title = match.group(1).strip()
    return FetchResult(provider="keenable", url=url, title=title, content=content,
                       truncated=len(content) >= get_settings().FETCH_CACHE_MAX_CHARS)
