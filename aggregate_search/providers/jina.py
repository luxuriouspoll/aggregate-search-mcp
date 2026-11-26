"""Jina（https://jina.ai）

搜索: GET https://s.jina.ai/<query>  Accept: application/json  X-Respond-With: no-content
      -> data[]: {title, url, description}
抓取: GET https://r.jina.ai/<url>    Accept: text/plain
      -> Markdown 正文

搜索默认会把每个结果页的全文都抓回来，又慢又费额度。这里加 no-content 只要
标题、链接和摘要：实测同一查询 20 秒 → 2 秒，计费 8.5 万 token → 固定 1 万 token。
需要全文时再用 web_fetch 指定 jina。
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

import httpx

from ..config import get_settings
from .base import (
    FetchResult,
    ProviderError,
    SearchResult,
    as_items,
    brief,
    date_metadata,
    expect_dict,
    get_text,
    text_of,
)

SEARCH_BASE = "https://s.jina.ai/"
READER_BASE = "https://r.jina.ai/"
_TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.M)


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    text = await get_text(
        client,
        f"{SEARCH_BASE}{quote(query, safe='')}",
        key=key,
        headers={"Accept": "application/json", "X-Respond-With": "no-content"},
        # 走代理时首包偏慢，给得比普通源宽一点
        timeout=max(get_settings().SEARCH_TIMEOUT, 25),
    )
    try:
        data = expect_dict(json.loads(text), "jina")
    except ValueError as exc:
        raise ProviderError(f"响应不是 JSON: {brief(text)}") from exc

    results: list[SearchResult] = []
    for item in as_items(data, "data"):
        url = str(item.get("url") or item.get("finalUrl") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        description = str(item.get("description") or "").strip()
        content = str(item.get("content") or "").strip()
        snippet = description or re.sub(r"\s+", " ", content)[:600]
        results.append(
            SearchResult(
                source="jina",
                url=url,
                title=str(item.get("title") or "").strip(),
                snippet=snippet[:600],
                rank=len(results),
                published=text_of(item.get("publishedTime")),
                dates=date_metadata(source_date=item.get("date")),
            )
        )
        if len(results) >= max_results:
            break
    return results


async def fetch(client: httpx.AsyncClient, key: str, url: str) -> FetchResult:
    text = await get_text(
        client,
        f"{READER_BASE}{url}",
        key=key,
        headers={"Accept": "text/plain", "X-Return-Format": "markdown"},
    )
    title = ""
    match = _TITLE_RE.search(text)
    if match:
        title = match.group(1).strip()
        text = _TITLE_RE.sub("", text, count=1)
    return FetchResult(provider="jina", url=url, title=title, content=text.strip())
