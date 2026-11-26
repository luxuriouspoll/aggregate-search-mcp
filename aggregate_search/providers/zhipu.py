"""智谱联网搜索（需 ZHIPU_API_KEY）。

官方按次计费，账号赠送 token 不等于搜索免费；不列入默认匿名池。
文档：https://docs.bigmodel.cn/cn/guide/tools/web-search
"""
from __future__ import annotations

import httpx

from .base import SearchResult, business_error, error_text, post_json, strict_items, text_of

SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        payload={"search_engine": "search_std", "search_query": query},
    )
    if data.get("error"):
        raise business_error(error_text(data["error"], "message", "msg", "code"))
    items = strict_items(data, "zhipu", "search_result")
    results: list[SearchResult] = []
    for item in items:
        url = text_of(item.get("link"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="zhipu",
            url=url,
            title=text_of(item.get("title")),
            snippet=text_of(item.get("content")),
            rank=len(results),
            published=text_of(item.get("publish_date") or item.get("date")),
        ))
        if len(results) >= max_results:
            break
    return results
