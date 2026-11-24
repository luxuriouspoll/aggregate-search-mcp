"""SerpApi Google 搜索（https://serpapi.com）

免费计划：250 次/月（以官网当前政策为准）。
GET https://serpapi.com/search.json?engine=google&q=...&api_key=...
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx

from providers.base import ProviderError, SearchResult, as_items, expect_dict, get_text

SEARCH_URL = "https://serpapi.com/search.json"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    params = urlencode({
        "engine": "google",
        "q": query,
        "num": min(max_results, 20),
        "api_key": key,
    })
    text = await get_text(client, f"{SEARCH_URL}?{params}", headers={"Accept": "application/json"})
    try:
        data = expect_dict(json.loads(text), "serpapi")
    except ValueError as exc:
        raise ProviderError(f"响应不是 JSON: {text[:200]}") from exc
    if data.get("error"):
        message = str(data["error"])
        raise ProviderError(message[:300], retryable=any(
            h in message.lower() for h in ("key", "quota", "limit", "credit")
        ))
    items = as_items(data, "organic_results")
    return [
        SearchResult(
            source="serpapi",
            url=str(item.get("link") or ""),
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("snippet") or "").strip(),
            rank=i,
            published=str(item.get("date") or ""),
        )
        for i, item in enumerate(items)
        if str(item.get("link") or "").startswith(("http://", "https://"))
    ][:max_results]
