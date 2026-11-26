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

from .base import (
    ProviderError,
    SearchResult,
    as_items,
    brief,
    get_text,
    text_of,
    wants_time,
)

SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    query_params = {"q": query, "count": min(max_results, 20)}
    freshness = wants_time("brave")
    if freshness:
        query_params["freshness"] = freshness
    params = urlencode(query_params)
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
        raise ProviderError(f"响应不是 JSON: {brief(text)}") from exc
    items = as_items(data, "web", "results")
    return [
        SearchResult(
            source="brave",
            url=text_of(item.get("url")),
            title=text_of(item.get("title")),
            snippet=text_of(item.get("description")),
            rank=i,
            published=text_of(item.get("age")) or text_of(item.get("page_age")),
        )
        for i, item in enumerate(items)
    ]
