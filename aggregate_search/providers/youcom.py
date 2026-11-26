"""You.com 免费搜索档（https://you.com）

匿名 agents/search 端点有按日限额；可选 YDC_API_KEY 使用正式 API。
国内直连可能超时，默认列入按源代理名单。
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx

from .base import (
    ProviderError,
    SearchResult,
    brief,
    get_text,
    post_json,
    strict_items,
    text_of,
)

FREE_URL = "https://api.you.com/v1/agents/search"
API_URL = "https://ydc-index.io/v1/search"


def _snippets(item: dict) -> str:
    """snippets 可能是列表、字符串或别的类型，统一成多行文本。"""
    raw = item.get("snippets") or item.get("snippet")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts = [part for part in (text_of(entry) for entry in raw[:2]) if part]
        return "\n".join(parts)
    return ""


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    if key:
        params = urlencode({"query": query, "count": max_results})
        text = await get_text(
            client,
            f"{API_URL}?{params}",
            key=key,
            auth="header:X-API-Key",
            headers={"Accept": "application/json"},
        )
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProviderError(f"youcom 响应不是 JSON: {brief(text)}") from exc
    else:
        data = await post_json(client, FREE_URL, payload={"query": query, "count": max_results})
    if not isinstance(data, dict):
        raise ProviderError("youcom 响应格式异常")
    # 结果可能在 results.web（对象）里，也可能直接在 results（列表）里
    results_node = data.get("results")
    if isinstance(results_node, dict) and "web" in results_node:
        items = strict_items(results_node, "youcom", "web")
    else:
        items = strict_items(data, "youcom", "results")
    results: list[SearchResult] = []
    for item in items:
        url = text_of(item.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="youcom",
            url=url,
            title=text_of(item.get("title")),
            snippet=_snippets(item)[:900],
            rank=len(results),
            published=text_of(item.get("page_age") or item.get("date")),
        ))
        if len(results) >= max_results:
            break
    return results
