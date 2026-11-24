"""Exa（https://exa.ai）

搜索: POST https://api.exa.ai/search  {query, numResults, type}
      头: x-api-key
      -> results[]: {url, title, publishedDate, author, text?, highlights[]}
抓取: POST https://api.exa.ai/contents  {urls: [...], text: true}
      -> results[]: {url, title, text}
"""
from __future__ import annotations

import httpx

from providers.base import FetchResult, ProviderError, SearchResult, as_items, expect_dict, post_json

SEARCH_URL = "https://api.exa.ai/search"
CONTENTS_URL = "https://api.exa.ai/contents"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        headers={"x-api-key": key},
        auth="none",  # Exa 用自己的 header，不再叠 Bearer
        payload={
            "query": query,
            "numResults": min(max_results, 25),
            "type": "auto",
            "contents": {"highlights": True},
        },
    )
    results = []
    for i, item in enumerate(as_items(expect_dict(data, "exa"), "results")):
        highlights = item.get("highlights") or []
        snippet = " … ".join(h.strip() for h in highlights[:2] if h) or (
            item.get("text") or ""
        ).strip()
        results.append(
            SearchResult(
                source="exa",
                url=item.get("url", ""),
                title=(item.get("title") or "").strip(),
                snippet=snippet[:600],
                rank=i,
                published=str(item.get("publishedDate") or ""),
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
    items = as_items(expect_dict(data, "exa"), "results")
    if not items:
        raise ProviderError(f"contents 无结果: {str(data)[:200]}")
    item = items[0]
    return FetchResult(
        provider="exa",
        url=url,
        title=(item.get("title") or "").strip(),
        content=(item.get("text") or "").strip(),
    )
