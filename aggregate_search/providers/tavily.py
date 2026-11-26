"""Tavily（https://www.tavily.com）

搜索: POST https://api.tavily.com/search   {query, max_results, search_depth}
      -> results[]: {url, title, content, score}
抓取: POST https://api.tavily.com/extract  {urls: [...]}
      -> results[]: {url, raw_content}
"""
from __future__ import annotations

import httpx

from .base import (
    FetchResult,
    ProviderError,
    SearchResult,
    as_items,
    brief,
    post_json,
    text_of,
)

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
            url=text_of(item.get("url")),
            title=text_of(item.get("title")),
            snippet=text_of(item.get("content")),
            rank=i,
        )
        for i, item in enumerate(as_items(data, "results"))
    ]


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    data = await post_json(client, EXTRACT_URL, key=key, payload={"urls": [url]})
    results = as_items(data, "results")
    if not results:
        failed = data.get("failed_results")
        extra = f"，失败信息: {brief(failed[0], 300)}" if isinstance(failed, list) and failed else ""
        raise ProviderError(f"extract 无结果{extra}")
    item = results[0]
    return FetchResult(
        provider="tavily",
        url=url,
        title="",
        content=text_of(item.get("raw_content")),
    )
