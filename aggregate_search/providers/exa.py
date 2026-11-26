"""Exa（https://exa.ai）

搜索: POST https://api.exa.ai/search  {query, numResults, type}
      头: x-api-key
      -> results[]: {url, title, publishedDate, author, text?, highlights[]}
抓取: POST https://api.exa.ai/contents  {urls: [...], text: true}
      -> results[]: {url, title, text}
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
    wants_domains,
    wants_time,
)

SEARCH_URL = "https://api.exa.ai/search"
CONTENTS_URL = "https://api.exa.ai/contents"


def _exa_payload(query: str, max_results: int) -> dict:
    payload = {
        "query": query,
        "numResults": min(max_results, 25),
        "type": "auto",
        "contents": {"highlights": True},
    }
    start = wants_time("exa")          # 绝对日期，形如 2026-11-19
    if start:
        payload["startPublishedDate"] = start
    domains = wants_domains()
    if domains:
        payload["includeDomains"] = list(domains[:20])
    return payload


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        headers={"x-api-key": key},
        auth="none",  # Exa 用自己的 header，不再叠 Bearer
        payload=_exa_payload(query, max_results),
    )
    results = []
    for i, item in enumerate(as_items(data, "results")):
        highlights = item.get("highlights")
        highlights = highlights if isinstance(highlights, list) else []
        snippet = " … ".join(
            part for part in (text_of(h) for h in highlights[:2]) if part
        ) or text_of(item.get("text"))
        results.append(
            SearchResult(
                source="exa",
                url=text_of(item.get("url")),
                title=text_of(item.get("title")),
                snippet=snippet[:600],
                rank=i,
                published=text_of(item.get("publishedDate")),
            )
        )
    return results


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    data = await post_json(
        client,
        CONTENTS_URL,
        key=key,
        headers={"x-api-key": key},
        auth="none",
        payload={"urls": [url], "text": True},
    )
    items = as_items(data, "results")
    if not items:
        raise ProviderError(f"contents 无结果: {brief(data)}")
    item = items[0]
    return FetchResult(
        provider="exa",
        url=url,
        title=text_of(item.get("title")),
        content=text_of(item.get("text")),
    )
