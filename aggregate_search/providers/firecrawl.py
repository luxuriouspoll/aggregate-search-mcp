"""Firecrawl（https://www.firecrawl.dev）

搜索: POST https://api.firecrawl.dev/v2/search {query, limit, sources: ["web"]}
      -> data.web[]: {url, title, description}
抓取: POST https://api.firecrawl.dev/v2/scrape  {url, formats: ["markdown"]}
      -> data.markdown, data.metadata.title
"""
from __future__ import annotations

import httpx

from ..config import FIRECRAWL_BASE_URL
from .base import (
    FetchResult,
    SearchResult,
    as_dict,
    as_items,
    business_error,
    fetch_error,
    post_json,
    text_of,
)

SEARCH_URL = f"{FIRECRAWL_BASE_URL}/v2/search"
SCRAPE_URL = f"{FIRECRAWL_BASE_URL}/v2/scrape"


def _check(data: dict, fetching: bool = False) -> dict:
    if data.get("success") is False:
        msg = str(data.get("error") or data)
        # 搜索：Firecrawl 会用 200 + success:false 表示额度 / 鉴权问题，换 key 可能有用。
        # 抓取：文案说的多半是目标网页，不拿它判断账号（额度不足时 Firecrawl 会直接回 HTTP 402）。
        raise fetch_error(msg) if fetching else business_error(msg)
    return data


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = _check(await post_json(client, SEARCH_URL, key=key, payload={
        "query": query,
        "limit": max_results,
        "sources": ["web"],
    }))
    items = as_items(data, "data", "web")
    return [
        SearchResult(
            source="firecrawl",
            url=text_of(item.get("url")),
            title=text_of(item.get("title")),
            snippet=text_of(item.get("description")),
            rank=i,
        )
        for i, item in enumerate(items)
    ]


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    data = _check(await post_json(client, SCRAPE_URL, key=key, payload={
        "url": url,
        "formats": ["markdown"],
    }), fetching=True)
    payload = as_dict(data.get("data"))
    metadata = as_dict(payload.get("metadata"))
    return FetchResult(
        provider="firecrawl",
        url=url,
        title=text_of(metadata.get("title")),
        content=text_of(payload.get("markdown")),
    )
