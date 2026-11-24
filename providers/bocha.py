"""博查 AI 搜索（需 BOCHA_API_KEY）。

账户额度/计费以官方控制台为准，不列入默认匿名池。
POST https://api.bochaai.com/v1/web-search
"""
from __future__ import annotations

import httpx

from providers.base import ProviderError, SearchResult, expect_dict, post_json

SEARCH_URL = "https://api.bochaai.com/v1/web-search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = expect_dict(await post_json(
        client, SEARCH_URL, key=key,
        payload={"query": query, "freshness": "noLimit", "summary": True, "count": max_results},
    ), "bocha")
    if data.get("code") not in (None, 0, "0", "200"):
        message = str(data.get("msg") or data.get("message") or data.get("code"))
        lowered = message.lower()
        status = 429 if "rate" in lowered or "limit" in lowered else 0
        if not status and any(h in lowered for h in ("balance", "quota", "credit")):
            status = 402
        if not status and any(h in lowered for h in ("key", "auth", "token")):
            status = 401
        raise ProviderError(message[:300], status=status, retryable=bool(status))
    web_pages = data.get("data", {}).get("webPages") if isinstance(data.get("data"), dict) else data.get("webPages")
    if not isinstance(web_pages, dict) or not isinstance(web_pages.get("value"), list):
        raise ProviderError("bocha 响应缺少 webPages.value 列表")
    results = []
    for item in web_pages["value"]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="bocha",
            url=url,
            title=str(item.get("name") or item.get("title") or "").strip(),
            snippet=str(item.get("summary") or item.get("snippet") or "").strip(),
            rank=len(results),
            published=str(item.get("datePublished") or ""),
        ))
        if len(results) >= max_results:
            break
    return results
