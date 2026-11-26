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
    date_metadata,
    post_json,
    strict_items,
    text_of,
)

SEARCH_URL = "https://api.anysearch.com/v1/search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        payload={"query": query, "max_results": max_results},
    )
    code = data.get("code", 0)
    if code not in (0, "0", None):
        raise business_error(text_of(data.get("message") or code))
    if not isinstance(data.get("data"), dict):
        raise ProviderError("anysearch 响应缺少 data 对象")
    items = strict_items(data["data"], "anysearch", "results")
    results: list[SearchResult] = []
    for item in items:
        url = text_of(item.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="anysearch",
            url=url,
            title=text_of(item.get("title")),
            snippet=text_of(item.get("snippet") or item.get("content")),
            rank=len(results),
            published=text_of(item.get("published_date")),
            dates=date_metadata(source_date=item.get("date")),
        ))
        if len(results) >= max_results:
            break
    return results
