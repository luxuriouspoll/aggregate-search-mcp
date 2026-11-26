"""博查 AI 搜索（需 BOCHA_API_KEY）。

账户额度/计费以官方控制台为准，不列入默认匿名池。
POST https://api.bochaai.com/v1/web-search

注意 code 字段：博查正常返回是**整数** 200，旧版判断里漏了整数 200，
导致正常结果被当成业务错误。这里同时接受整数和字符串两种写法。
"""
from __future__ import annotations

import httpx

from .base import (
    ProviderError,
    SearchResult,
    as_dict,
    business_error,
    post_json,
    text_of,
    wants_time,
)

SEARCH_URL = "https://api.bochaai.com/v1/web-search"

# 正常返回的 code：整数 200 / 字符串 "200"，以及历史版本用过的 0
_OK_CODES = (None, 0, "0", 200, "200")


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        payload={
            "query": query,
            "freshness": wants_time("bocha") or "noLimit",
            "summary": True,
            "count": max_results,
        },
    )
    if data.get("code") not in _OK_CODES:
        raise business_error(text_of(data.get("msg") or data.get("message") or data.get("code")))
    # webPages 有两种位置：data.webPages（新版）和顶层 webPages（旧版）
    web_pages = as_dict(data.get("data")).get("webPages") if isinstance(data.get("data"), dict) else None
    if not isinstance(web_pages, dict):
        web_pages = data.get("webPages")
    values = as_dict(web_pages).get("value")
    if not isinstance(values, list):
        raise ProviderError("bocha 响应缺少 webPages.value 列表")
    results: list[SearchResult] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        url = text_of(item.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(SearchResult(
            source="bocha",
            url=url,
            title=text_of(item.get("name") or item.get("title")),
            snippet=text_of(item.get("summary") or item.get("snippet")),
            rank=len(results),
            published=text_of(item.get("datePublished")),
        ))
        if len(results) >= max_results:
            break
    return results
