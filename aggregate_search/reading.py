"""Bounded document presentation and GitHub repository README retrieval."""
import math
import re
from urllib.parse import urlsplit

import httpx

from . import config
from .providers.base import FetchResult, redact
from .quality import _query_terms


def document_page(content: str, start: int, focus: str, limit: int) -> str:
    """Offsets are Python character indices in the cached, redacted document."""
    total = len(content)
    if start >= total:
        return f"原文共 {total} 字符，start_index={start} 已到末尾。"
    if not focus.strip():
        end = min(total, start + limit)
        hint = f"\n继续阅读请传 start_index={end}" if end < total else "\n已到缓存正文末尾。"
        return f"原文区间 [{start}, {end}) / 已获取正文 {total} 字符\n\n{content[start:end]}{hint}"
    # Include headings with their following paragraphs. Bound huge paragraphs.
    boundaries = [0] + [m.end() for m in re.finditer(r"\n\s*\n", content)] + [total]
    blocks = []
    for left, right in zip(boundaries, boundaries[1:]):
        for offset in range(max(left, start), right, 2000):
            blocks.append((offset, min(offset + 2000, right)))
    terms = _query_terms(focus.casefold())[:64]
    texts = []
    heading = ""
    for left, right in blocks:
        text = content[left:right]
        if text.lstrip().startswith("#"):
            heading = text.splitlines()[0]
        texts.append((heading + "\n" + text).casefold())
    weights = {term: math.log(1 + len(texts)/(1 + sum(term in text for text in texts))) for term in terms}
    scored = []
    for (left, right), text in zip(blocks, texts):
        score = sum(weights[t] * text.count(t)/(1 + text.count(t) + len(text)/1000) for t in terms)
        if score:
            scored.append((score, left, right))
    if not scored:
        return "未匹配 focus，按原文顺序展示。\n" + document_page(content, start, "", limit)
    selected = []
    remaining = limit
    for _, left, right in sorted(scored, key=lambda row: (-row[0], row[1])):
        if remaining <= 0:
            break
        end = min(right, left + remaining)
        selected.append(f"原文区间 [{left}, {end})\n{content[left:end]}")
        remaining -= end - left
    return f"聚焦摘录 / 已获取正文 {total} 字符；可用 start_index 查看原文上下文。\n\n" + "\n\n".join(selected)


def github_repository(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname != 'github.com' or parts.port not in (None, 443)
                or parts.username or parts.password or parts.query or parts.fragment):
            return None
        match = re.fullmatch(r'/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)/?', parts.path)
        if match and match[2] not in ('.', '..'):
            return match[1], match[2]
    except ValueError:
        pass
    return None


async def github_readme(url: str) -> FetchResult | None:
    repo = github_repository(url)
    if repo is None:
        return None
    from .providers import client_for
    settings = config.get_settings()
    # Fixed host, no credentials, no redirects. Never calls the GitHub REST API.
    async with client_for('github') as client:
        for filename in ('README.md', 'readme.md', 'README.rst', 'README', 'README.MD'):
            target = f'https://raw.githubusercontent.com/{repo[0]}/{repo[1]}/HEAD/{filename}'
            try:
                async with client.stream('GET', target, follow_redirects=False,
                                         timeout=settings.SEARCH_TIMEOUT) as response:
                    if response.status_code != 200:
                        continue
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 2_000_000:
                            return None
                        data.extend(chunk)
                content = redact(data.decode('utf-8', errors='replace'))
                if content.strip():
                    return FetchResult('github', url, f'{repo[0]}/{repo[1]} README',
                                       content[:settings.FETCH_CACHE_MAX_CHARS], 'anon',
                                       len(content) > settings.FETCH_CACHE_MAX_CHARS)
            except httpx.HTTPError:
                return None
    return None
