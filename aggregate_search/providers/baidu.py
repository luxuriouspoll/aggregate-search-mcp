"""百度智能云千帆·百度搜索（需 BAIDU_API_KEY）。

官方文档：https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy
免费额度以官方当前页面为准，使用账号配额，不属于匿名免费。
"""
from __future__ import annotations

import httpx

from .base import SearchResult, business_error, date_metadata, post_json, strict_items, text_of

SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
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
    )
    if data.get("code"):
        raise business_error(text_of(data.get("message") or data.get("code")))
    items = strict_items(data, "baidu", "references")
    results: list[SearchResult] = []
    for item in items:
        url = text_of(item.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="baidu",
            url=url,
            title=text_of(item.get("title")),
            snippet=text_of(item.get("content")),
            rank=len(results),
            dates=date_metadata(source_date=item.get("date")),
        ))
        if len(results) >= max_results:
            break
    return results
