"""腾讯元宝搜索（需 TENCENTCLOUD_WSA_APIKEY）。

官方 API KEY 方式：POST https://api.wsa.cloud.tencent.com/SearchPro
新接入用户的免费额度以腾讯云活动/控制台为准，使用账号配额。
"""
from __future__ import annotations

import json

import httpx

from .base import ProviderError, SearchResult, brief, business_status, expect_dict, post_json

SEARCH_URL = "https://api.wsa.cloud.tencent.com/SearchPro"


def _business_status(text: str) -> int:
    """腾讯云的错误码优先；其余只认明确的文案（不再用 "rate" 这种会误伤的子串）。"""
    lowered = text.lower()
    if "requestlimitexceeded" in lowered:
        return 429
    if "resourceunavailable" in lowered or "resourceinsufficient" in lowered:
        return 402
    if "authfailure" in lowered:
        return 401
    return business_status(text)


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    count = max(10, min(50, ((max_results + 9) // 10) * 10))
    data = expect_dict(await post_json(
        client, SEARCH_URL, key=key,
        payload={"Query": query, "Mode": 2, "Cnt": count},
    ), "yuanbao")
    response = data.get("Response")
    if not isinstance(response, dict):
        raise ProviderError(f"yuanbao 响应缺少 Response: {brief(data)}")
    if response.get("Error"):
        message = str(response["Error"])
        status = _business_status(message)
        raise ProviderError(brief(message, 300), status=status, retryable=bool(status), soft=True)

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
