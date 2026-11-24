"""Firecrawl（https://www.firecrawl.dev）

搜索: POST https://api.firecrawl.dev/v2/search {query, limit, sources: ["web"]}
      -> data.web[]: {url, title, description}
抓取: POST https://api.firecrawl.dev/v2/scrape  {url, formats: ["markdown"]}
      -> data.markdown, data.metadata.title
"""
from __future__ import annotations

import httpx

from providers.base import FetchResult, ProviderError, SearchResult, as_items, expect_dict, post_json

SEARCH_URL = "https://api.firecrawl.dev/v2/search"
SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"


def _check(data: dict) -> dict:
    if data.get("success") is False:
        msg = str(data.get("error") or data)
        # Firecrawl 用 200 + success:false 表示额度/鉴权问题，换 key 可能有用
        raise ProviderError(
            msg[:300],
            retryable=any(
                hint in msg.lower()
                for hint in ("credit", "quota", "limit", "api key", "unauthorized", "payment", "额度")
            ),
        )
    return data


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = _check(await post_json(client, SEARCH_URL, key=key, payload={
        "query": query,
        "limit": max_results,
        "sources": ["web"],
    }))
    items = as_items(expect_dict(data, "firecrawl"), "data", "web")
    return [
        SearchResult(
            source="firecrawl",
            url=item.get("url", ""),
            title=(item.get("title") or "").strip(),
            snippet=(item.get("description") or "").strip(),
            rank=i,
        )
        for i, item in enumerate(items)
    ]


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    data = _check(await post_json(client, SCRAPE_URL, key=key, payload={
        "url": url,
        "formats": ["markdown"],
    }))
    payload = data.get("data") or {}
    metadata = payload.get("metadata") or {}
    return FetchResult(
        provider="firecrawl",
        url=url,
        title=(metadata.get("title") or "").strip(),
        content=(payload.get("markdown") or "").strip(),
    )
