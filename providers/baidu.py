"""百度智能云千帆·百度搜索（需 BAIDU_API_KEY）。

官方文档：https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy
免费额度以官方当前页面为准，使用账号配额，不属于匿名免费。
"""
from __future__ import annotations

import httpx

from providers.base import ProviderError, SearchResult, expect_dict, post_json, strict_items

SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = expect_dict(await post_json(
        client,
        SEARCH_URL,
        key=key,
        headers={"X-Appbuilder-Authorization": f"Bearer {key}"},
        auth="none",
        payload={
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": min(max_results, 50)}],
        },
    ), "baidu")
    if data.get("code"):
        message = str(data.get("message") or data["code"])
        lowered = message.lower()
        status = 429 if any(h in lowered for h in ("rate", "limit", "qps")) else 0
        if not status and any(h in lowered for h in ("quota", "balance", "欠费")):
            status = 402
        if not status and any(h in lowered for h in ("auth", "key", "permission")):
            status = 401
        raise ProviderError(message[:300], status=status, retryable=bool(status))
    items = strict_items(data, "baidu", "references")
    return [
        SearchResult(
            source="baidu",
            url=str(item.get("url") or ""),
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("content") or "").strip(),
            rank=i,
            published=str(item.get("date") or ""),
        )
        for i, item in enumerate(items)
        if str(item.get("url") or "").startswith(("http://", "https://"))
    ][:max_results]
