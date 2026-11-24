"""Tavily（https://www.tavily.com）

搜索: POST https://api.tavily.com/search   {query, max_results, search_depth}
      -> results[]: {url, title, content, score}
抓取: POST https://api.tavily.com/extract  {urls: [...]}
      -> results[]: {url, raw_content}
"""
from __future__ import annotations

import httpx

from providers.base import FetchResult, ProviderError, SearchResult, as_items, expect_dict, post_json

SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(client, SEARCH_URL, key=key, payload={
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    })
    return [
        SearchResult(
            source="tavily",
            url=item.get("url", ""),
            title=(item.get("title") or "").strip(),
            snippet=(item.get("content") or "").strip(),
            rank=i,
        )
        for i, item in enumerate(as_items(expect_dict(data, "tavily"), "results"))
    ]


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    data = await post_json(client, EXTRACT_URL, key=key, payload={"urls": [url]})
    results = as_items(expect_dict(data, "tavily"), "results")
    if not results:
        failed = data.get("failed_results") or []
        extra = f"，失败信息: {failed[0]}" if failed else ""
        raise ProviderError(f"extract 无结果{extra}")
    item = results[0]
    return FetchResult(
        provider="tavily",
        url=url,
        title="",
        content=(item.get("raw_content") or "").strip(),
    )
