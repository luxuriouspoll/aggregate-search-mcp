"""Querit（https://www.querit.ai）

搜索: POST https://api.querit.ai/v1/search  {query, count}
      -> results.result[]: {url, title, snippet, page_age, site_name}
官方目前只公开了搜索端点，未提供可调用的抓取接口，故本家不支持 web_fetch。
"""
from __future__ import annotations

import httpx

from .base import (
    ProviderError,
    SearchResult,
    as_items,
    brief,
    business_status,
    date_metadata,
    post_json,
    text_of,
)

SEARCH_URL = "https://api.querit.ai/v1/search"

# 业务码 → 等价的状态码。有了状态码，key 池才会换 key 并让坏 key 歇一会儿，
# 而不是之后每次搜索都把它再试一遍。
_CODE_STATUS = {"10403": 403, "10406": 402, "10408": 429}


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(client, SEARCH_URL, key=key, payload={"query": query, "count": max_results})
    code = str(data.get("error_code", "200"))
    if code != "200":
        msg = str(data.get("error_msg", ""))
        # 业务码是 401/402/403/429 时直接采信；其它业务码只认明确的额度 / 鉴权 / 限流文案
        known = code.isdigit() and int(code) in (401, 402, 403, 429)
        status = int(code) if known else (_CODE_STATUS.get(code) or business_status(msg))
        raise ProviderError(
            brief(f"error_code={code} {msg}".strip(), 300),
            status=status,
            retryable=bool(status),
            soft=not known,
        )
    items = as_items(data, "results", "result")
    return [
        SearchResult(
            source="querit",
            url=text_of(item.get("url")),
            title=text_of(item.get("title")),
            snippet=text_of(item.get("snippet")),
            rank=i,
            dates=date_metadata(page_age=item.get("page_age")),
        )
        for i, item in enumerate(items)
    ]
