"""腾讯元宝搜索（需 TENCENTCLOUD_WSA_APIKEY）。

官方 API KEY 方式：POST https://api.wsa.cloud.tencent.com/SearchPro
新接入用户的免费额度以腾讯云活动/控制台为准，使用账号配额。
"""
from __future__ import annotations

import json

import httpx

from providers.base import ProviderError, SearchResult, expect_dict, post_json

SEARCH_URL = "https://api.wsa.cloud.tencent.com/SearchPro"


def _business_status(text: str) -> int:
    lowered = text.lower()
    if any(h in lowered for h in ("requestlimitexceeded", "rate", "频繁")):
        return 429
    if any(h in lowered for h in ("resourceunavailable", "欠费", "quota")):
        return 402
    if any(h in lowered for h in ("unauthorized", "api key", "apikey", "permission")):
        return 401
    return 0


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    count = max(10, min(50, ((max_results + 9) // 10) * 10))
    data = expect_dict(await post_json(
        client, SEARCH_URL, key=key,
        payload={"Query": query, "Mode": 2, "Cnt": count},
    ), "yuanbao")
    response = data.get("Response")
    if not isinstance(response, dict):
        raise ProviderError(f"yuanbao 响应缺少 Response: {str(data)[:200]}")
    if response.get("Error"):
        message = str(response["Error"])
        status = _business_status(message)
        raise ProviderError(message[:300], status=status, retryable=bool(status))

    pages = response.get("Pages") or []
    if not isinstance(pages, list):
        raise ProviderError("yuanbao Pages 不是列表")
    results: list[SearchResult] = []
    for raw in pages:
        item = raw
        if isinstance(raw, str):
            try:
                item = json.loads(raw)
            except ValueError:
                continue
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="yuanbao",
            url=url,
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("passage") or item.get("content") or "").strip(),
            rank=len(results),
            published=str(item.get("date") or ""),
        ))
        if len(results) >= max_results:
            break
    return results
