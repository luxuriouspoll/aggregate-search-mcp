"""SearXNG（自托管元搜索，https://docs.searxng.org）

搜索: GET {SEARXNG_BASE_URL}/search?q=...&format=json
      -> results[]: {url, title, content, publishedDate}

不需要 key，靠实例地址启用（SEARXNG_BASE_URL）。
注意：实例必须允许 JSON 输出（settings.yml 里 search.formats 含 json），
否则只返回 403 Forbidden。部分公共实例会禁用 JSON 格式。
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx

from ..config import SEARXNG_BASE_URL
from .base import (
    ProviderError,
    SearchResult,
    as_items,
    brief,
    get_text,
    text_of,
    wants_time,
)


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    if not SEARXNG_BASE_URL:
        raise ProviderError("未配置 SEARXNG_BASE_URL（SearXNG 靠实例地址启用，不需要 key）")
    query_params = {"q": query, "format": "json", "safesearch": "0"}
    time_range = wants_time("searxng")
    if time_range:
        query_params["time_range"] = time_range
    params = urlencode(query_params)
    text = await get_text(
        client,
        f"{SEARXNG_BASE_URL}/search?{params}",
        headers={"Accept": "application/json"},
    )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProviderError(
            f"响应不是 JSON（实例可能未开启 json 格式）: {brief(text)}"
        ) from exc
    return [
        SearchResult(
            source="searxng",
            url=text_of(item.get("url")),
            title=text_of(item.get("title")),
            snippet=text_of(item.get("content")),
            rank=i,
            published=text_of(item.get("publishedDate")),
        )
        for i, item in enumerate(as_items(data, "results"))
        if text_of(item.get("url"))
    ][:max_results]
