"""Ollama Web Search（https://ollama.com，key 在 ollama.com 账户的 keys 设置页创建）

搜索: POST https://ollama.com/api/web_search {query, max_results}
      -> results[]: {title, url, content}
抓取: POST https://ollama.com/api/web_fetch  {url}
      -> {title, content, links}
"""
from __future__ import annotations

import httpx

from .base import FetchResult, SearchResult, as_items, post_json, text_of

SEARCH_URL = "https://ollama.com/api/web_search"
FETCH_URL = "https://ollama.com/api/web_fetch"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(client, SEARCH_URL, key=key, payload={
        "query": query,
        "max_results": max_results,
    })
    return [
        SearchResult(
            source="ollama",
            url=text_of(item.get("url")),
            title=text_of(item.get("title")),
            snippet=text_of(item.get("content")),
            rank=i,
        )
        for i, item in enumerate(as_items(data, "results"))
    ]


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    data = await post_json(client, FETCH_URL, key=key, payload={"url": url})
    return FetchResult(
        provider="ollama",
        url=url,
        title=text_of(data.get("title")),
        content=text_of(data.get("content")),
    )
