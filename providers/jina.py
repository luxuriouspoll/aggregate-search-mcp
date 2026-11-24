"""Jina（https://jina.ai）

搜索: GET https://s.jina.ai/<query>      头: Authorization: Bearer
      -> 纯文本搜索结果（不是 JSON），按空行分段
抓取: GET https://r.jina.ai/<url>        头: Authorization: Bearer
      -> Markdown 正文

Jina 的两条端点都返回文本，格式不稳定，故按行做宽松解析；
无 key 时匿名访问有时可用，但 IP 信誉差会被 401 拦。
"""
from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from providers.base import FetchResult, ProviderError, SearchResult, get_text

SEARCH_BASE = "https://s.jina.ai/"
READER_BASE = "https://r.jina.ai/"

# 形如 "Title: xxx" / "URL Source: https://..." 的头部块
_TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.M)
_URL_RE = re.compile(r"^(?:URL Source|URL):\s*(\S+)$", re.M)


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    text = await get_text(
        client,
        f"{SEARCH_BASE}{quote(query, safe="")}",
        key=key,
        headers={"Accept": "text/plain", "X-Return-Format": "markdown"},
    )
    if not text.strip():
        raise ProviderError("返回内容为空")

    # Jina 搜索结果用 "N. Title" 或 "## Title" 分块，取每块的标题与 URL
    blocks = [b for b in re.split(r"\n(?=\d+\.\s|##\s)", text) if b.strip()]
    results: list[SearchResult] = []
    for block in blocks:
        url_match = _URL_RE.search(block)
        title_match = _TITLE_RE.search(block) or re.match(r"^\s*(?:\d+\.\s+|##\s*)(.+)$", block)
        if not url_match:
            continue
        url = url_match.group(1).strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = (title_match.group(1).strip() if title_match else url)[:200]
        # 去掉头部元信息，剩下的当摘要
        body = _TITLE_RE.sub("", _URL_RE.sub("", block)).strip()
        results.append(
            SearchResult(
                source="jina",
                url=url,
                title=title,
                snippet=body.replace("\n", " ").strip()[:600],
                rank=len(results),
            )
        )
        if len(results) >= max_results:
            break
    # 解析出 0 条可能是真的没结果，不当作错误（只记空列表）
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
