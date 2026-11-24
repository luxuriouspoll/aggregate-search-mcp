"""DuckDuckGo HTML 端点（免 key）

搜索: POST https://html.duckduckgo.com/html/  form: q=<query>
      -> HTML，结果在 .result__a / .result__snippet 里

免 key、免注册，但属于非官方端点：可能被限流或改版。
解析用正则而非 bs4，避免多一个依赖；改版失效时这里会返回空结果并报错。
"""
from __future__ import annotations

import html
import re
from urllib.parse import unquote

import httpx

from config import SEARCH_TIMEOUT
from providers.base import ProviderError, SearchResult

SEARCH_URL = "https://html.duckduckgo.com/html/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=<编码后的真实URL>&...">
# 按结果块整体切分后再在块内取链接与摘要，避免两个独立 findall 按下标对齐时，
# 某条结果缺摘要导致后面所有摘要整体错位。
_BLOCK_RE = re.compile(r'<div[^>]+class="[^"]*result__body[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S)
_LINK_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</(?:a|div|span)>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _real_url(href: str) -> str:
    """DDG 把真实地址包在 /l/?uddg=<urlencode> 里，解出来。"""
    href = html.unescape(href)
    match = re.search(r"[?&]uddg=([^&]+)", href)
    if match:
        return unquote(match.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    try:
        resp = await client.post(
            SEARCH_URL,
            data={"q": query, "kl": "wt-wt"},
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=SEARCH_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"网络错误: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(
            f"HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code,
            retry_after=None,
        )

    # 优先按结果块解析（链接与摘要在同一块内配对，不会错位）；
    # 块结构不匹配时回退到全文匹配，保证改版后仍能出结果。
    blocks = _BLOCK_RE.findall(resp.text) or [resp.text]
    results: list[SearchResult] = []
    for block in blocks:
        link_match = _LINK_RE.search(block)
        if not link_match:
            continue
        url = _real_url(link_match.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        snippet_match = _SNIPPET_RE.search(block)
        results.append(
            SearchResult(
                source="duckduckgo",
                url=url,
                title=_clean(link_match.group(2)),
                snippet=_clean(snippet_match.group(1)) if snippet_match else "",
                rank=len(results),
            )
        )
        if len(results) >= max_results:
            break
    # 空结果可能是真的没有匹配，不当作错误
    return results
