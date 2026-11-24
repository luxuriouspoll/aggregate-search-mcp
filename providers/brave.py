"""Brave Search（https://brave.com/search/api）

搜索: GET https://api.search.brave.com/res/v1/web/search?q=&count=
      头: X-Subscription-Token
      -> web.results[]: {url, title, description, age}
抓取: 官方无独立抓取接口，用通用的「摘要拼装」不合适，故不做 web_fetch。
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx

from providers.base import ProviderError, SearchResult, as_items, expect_dict, get_text

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    params = urlencode({"q": query, "count": min(max_results, 20)})
    text = await get_text(
        client,
        f"{SEARCH_URL}?{params}",
        key=key,
        headers={"Accept": "application/json"},
        auth="header:X-Subscription-Token",
    )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProviderError(f"响应不是 JSON: {text[:200]}") from exc
    items = as_items(expect_dict(data, "brave"), "web", "results")
    return [
        SearchResult(
            source="brave",
            url=item.get("url", ""),
            title=(item.get("title") or "").strip(),
            snippet=(item.get("description") or "").strip(),
            rank=i,
            published=str(item.get("age") or item.get("page_age") or ""),
        )
        for i, item in enumerate(items)
    ]
