"""Linkup 搜索（https://www.linkup.so）

注册有免费试用额度（具体以官网当前政策为准）。
POST https://api.linkup.so/v1/search，Bearer 认证。
"""
from __future__ import annotations

import httpx

from providers.base import ProviderError, SearchResult, as_items, expect_dict, post_json

SEARCH_URL = "https://api.linkup.so/v1/search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = expect_dict(
        await post_json(
            client,
            SEARCH_URL,
            key=key,
            payload={"q": query, "depth": "standard", "outputType": "searchResults"},
        ),
        "linkup",
    )
    items = as_items(data, "results")
    return [
        SearchResult(
            source="linkup",
            url=str(item.get("url") or ""),
            title=str(item.get("name") or item.get("title") or "").strip(),
            snippet=str(item.get("content") or item.get("snippet") or "").strip()[:900],
            rank=i,
            published=str(item.get("date") or item.get("publishedDate") or ""),
        )
        for i, item in enumerate(items)
        if str(item.get("url") or "").startswith(("http://", "https://"))
    ][:max_results]
