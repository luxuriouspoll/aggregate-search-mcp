"""Serper Google 搜索（https://serper.dev）

注册赠送一次性 2,500 次查询（以官网当前政策为准）。
POST https://google.serper.dev/search，使用 X-API-KEY 认证。
"""
from __future__ import annotations

import httpx

from providers.base import ProviderError, SearchResult, as_items, expect_dict, post_json

SEARCH_URL = "https://google.serper.dev/search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = expect_dict(
        await post_json(
            client,
            SEARCH_URL,
            key=key,
            auth="header:X-API-KEY",
            payload={"q": query, "num": min(max_results, 20)},
        ),
        "serper",
    )
    items = as_items(data, "organic")
    return [
        SearchResult(
            source="serper",
            url=str(item.get("link") or ""),
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("snippet") or "").strip(),
            rank=i,
            published=str(item.get("date") or ""),
        )
        for i, item in enumerate(items)
        if str(item.get("link") or "").startswith(("http://", "https://"))
    ][:max_results]
