"""Querit（https://www.querit.ai）

搜索: POST https://api.querit.ai/v1/search  {query, count}
      -> results.result[]: {url, title, snippet, page_age, site_name}
官方目前只公开了搜索端点，未提供可调用的抓取接口，故本家不支持 web_fetch。
"""
from __future__ import annotations

import httpx

from providers.base import ProviderError, SearchResult, as_items, expect_dict, post_json

SEARCH_URL = "https://api.querit.ai/v1/search"

# 业务错误码里代表「换 key 可解」的情况
_RETRYABLE_CODES = {"401", "402", "403", "429", "10403", "10406", "10408"}


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(client, SEARCH_URL, key=key, payload={"query": query, "count": max_results})
    code = str(data.get("error_code", "200"))
    if code != "200":
        msg = str(data.get("error_msg", ""))
        # 401/402/403/429 以及额度类文案 → 换 key 可能有用
        raise ProviderError(
            f"error_code={code} {msg}".strip(),
            status=int(code) if code.isdigit() and int(code) in (401, 402, 403, 429) else 0,
            retryable=code in _RETRYABLE_CODES
            or any(h in msg.lower() for h in ("api key", "quota", "额度", "limit", "permission")),
        )
    items = as_items(expect_dict(data, "querit"), "results", "result")
    return [
        SearchResult(
            source="querit",
            url=item.get("url", ""),
            title=(item.get("title") or "").strip(),
            snippet=(item.get("snippet") or "").strip(),
            rank=i,
            published=str(item.get("page_age") or ""),
        )
        for i, item in enumerate(items)
    ]
