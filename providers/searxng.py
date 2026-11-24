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

from config import SEARXNG_BASE_URL
from providers.base import ProviderError, SearchResult, as_items, expect_dict, get_text


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    if not SEARXNG_BASE_URL:
        raise ProviderError("未配置 SEARXNG_BASE_URL（SearXNG 靠实例地址启用，不需要 key）")
    params = urlencode({"q": query, "format": "json", "safesearch": "0"})
    text = await get_text(
        client,
        f"{SEARXNG_BASE_URL}/search?{params}",
        headers={"Accept": "application/json"},
    )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProviderError(
            f"响应不是 JSON（实例可能未开启 json 格式）: {text[:200]}"
        ) from exc
    return [
        SearchResult(
            source="searxng",
            url=item.get("url", ""),
            title=(item.get("title") or "").strip(),
            snippet=(item.get("content") or "").strip(),
            rank=i,
            published=str(item.get("publishedDate") or ""),
        )
        for i, item in enumerate(as_items(expect_dict(data, "searxng"), "results"))
        if item.get("url")
    ][:max_results]
