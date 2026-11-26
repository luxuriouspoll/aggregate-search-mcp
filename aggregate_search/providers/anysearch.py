"""AnySearch 匿名/密钥搜索（https://www.anysearch.com）

匿名访问有按 IP 的免费额度；可选 ANYSEARCH_API_KEY 提高额度。
POST https://api.anysearch.com/v1/search {query, max_results}
"""
from __future__ import annotations

import httpx

from .base import (
    ProviderError,
    SearchResult,
    business_error,
    expect_dict,
    post_json,
    strict_items,
)

SEARCH_URL = "https://api.anysearch.com/v1/search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = expect_dict(
        await post_json(
            client,
            SEARCH_URL,
            key=key,
            payload={"query": query, "max_results": max_results},
        ),
        "anysearch",
    )
    code = data.get("code", 0)
    if code not in (0, "0", None):
        raise business_error(str(data.get("message") or code))
    if not isinstance(data.get("data"), dict):
        raise ProviderError("anysearch 响应缺少 data 对象")
    items = strict_items(data["data"], "anysearch", "results")
    return [
        SearchResult(
            source="anysearch",
            url=str(item.get("url") or ""),
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("snippet") or item.get("content") or "").strip(),
            rank=i,
            published=str(item.get("date") or item.get("published_date") or ""),
        )
        for i, item in enumerate(items)
        if str(item.get("url") or "").startswith(("http://", "https://"))
    ][:max_results]
