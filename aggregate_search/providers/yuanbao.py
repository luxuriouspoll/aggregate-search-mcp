"""腾讯元宝搜索（需 TENCENTCLOUD_WSA_APIKEY）。

官方 API KEY 方式：POST https://api.wsa.cloud.tencent.com/SearchPro
新接入用户的免费额度以腾讯云活动/控制台为准，使用账号配额。

Pages 里每一项可能是对象，也可能是对象的 JSON 字符串，两种都认。
"""
from __future__ import annotations

import json

import httpx

from .base import (
    ProviderError,
    SearchResult,
    brief,
    business_status,
    error_text,
    post_json,
    text_of,
)

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


def _as_page(raw) -> dict | None:
    """Pages 里的一项：对象直接用，JSON 字符串解析，其它类型丢掉。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    count = max(10, min(50, ((max_results + 9) // 10) * 10))
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        payload={"Query": query, "Mode": 2, "Cnt": count},
    )
    response = data.get("Response")
    if not isinstance(response, dict):
        raise ProviderError(f"yuanbao 响应缺少 Response: {brief(data)}")
    if response.get("Error"):
        message = error_text(response["Error"], "Code", "code", "Message", "message")
        status = _business_status(message)
        raise ProviderError(brief(message, 300), status=status, retryable=bool(status), soft=True)

    pages = response.get("Pages") or []
    if not isinstance(pages, list):
        raise ProviderError("yuanbao Pages 不是列表")
    results: list[SearchResult] = []
    for raw in pages:
        item = _as_page(raw)
        if item is None:
            continue
        url = text_of(item.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="yuanbao",
            url=url,
            title=text_of(item.get("title")),
            snippet=text_of(item.get("passage") or item.get("content")),
            rank=len(results),
            published=text_of(item.get("date")),
        ))
        if len(results) >= max_results:
            break
    return results
