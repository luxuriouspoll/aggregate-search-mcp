"""You.com 免费搜索档（https://you.com）

匿名 agents/search 端点有按日限额；可选 YDC_API_KEY 使用正式 API。
国内直连可能超时，默认列入按源代理名单。
"""
from __future__ import annotations

import httpx

from urllib.parse import urlencode

from providers.base import (
    ProviderError,
    SearchResult,
    expect_dict,
    get_text,
    post_json,
    strict_items,
)
import json

FREE_URL = "https://api.you.com/v1/agents/search"
API_URL = "https://ydc-index.io/v1/search"


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    if key:
        params = urlencode({"query": query, "count": max_results})
        text = await get_text(
            client, f"{API_URL}?{params}", key=key, auth="header:X-API-Key",
            headers={"Accept": "application/json"},
        )
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProviderError(f"youcom 响应不是 JSON: {text[:200]}") from exc
    else:
        data = await post_json(
            client, FREE_URL, payload={"query": query, "count": max_results},
        )
    data = expect_dict(data, "youcom")
    results_node = data.get("results")
    if isinstance(results_node, dict) and "web" in results_node:
        items = strict_items(results_node, "youcom", "web")
    else:
        items = strict_items(data, "youcom", "results")
    results: list[SearchResult] = []
    for item in items:
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        snippets = item.get("snippets") or []
        if not isinstance(snippets, list):
            snippets = [str(snippets)]
        results.append(SearchResult(
            source="youcom",
            url=url,
            title=str(item.get("title") or "").strip(),
            snippet="\n".join(str(x) for x in snippets[:2])[:900],
            rank=len(results),
            published=str(item.get("page_age") or item.get("date") or ""),
        ))
        if len(results) >= max_results:
            break
    return results
