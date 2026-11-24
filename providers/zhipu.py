"""智谱联网搜索（需 ZHIPU_API_KEY）。

官方按次计费，账号赠送 token 不等于搜索免费；不列入默认匿名池。
文档：https://docs.bigmodel.cn/cn/guide/tools/web-search
"""
from __future__ import annotations

import httpx

from providers.base import ProviderError, SearchResult, expect_dict, post_json, strict_items

SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = expect_dict(await post_json(
        client, SEARCH_URL, key=key,
        payload={"search_engine": "search_std", "search_query": query},
    ), "zhipu")
    if data.get("error"):
        message = str(data["error"])
        lowered = message.lower()
        status = 429 if "rate" in lowered or "limit" in lowered else 0
        if not status and any(h in lowered for h in ("balance", "quota", "欠费")):
            status = 402
        if not status and any(h in lowered for h in ("key", "auth", "token")):
            status = 401
        raise ProviderError(message[:300], status=status, retryable=bool(status))
    items = strict_items(data, "zhipu", "search_result")
    return [
        SearchResult(
            source="zhipu",
            url=str(item.get("link") or ""),
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("content") or "").strip(),
            rank=i,
            published=str(item.get("publish_date") or item.get("date") or ""),
        )
        for i, item in enumerate(items)
        if str(item.get("link") or "").startswith(("http://", "https://"))
    ][:max_results]
