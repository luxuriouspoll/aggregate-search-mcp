"""Bounded document presentation and GitHub repository README retrieval."""
from bisect import bisect_right
import math
import re
from urllib.parse import urlsplit

import httpx

from . import config
from .providers.base import FetchResult, _DIGIT_PUNCTUATION, _NUMERIC_TOKEN, numeric_terms, redact
from .quality import _query_terms


def _uncovered(left: int, right: int, used: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Subtract already displayed intervals without duplicating overlap characters."""
    pieces = []
    cursor = left
    for used_left, used_right in sorted(used):
        if used_right <= cursor:
            continue
        if used_left >= right:
            break
        if used_left > cursor:
            pieces.append((cursor, used_left))
        cursor = max(cursor, used_right)
    if cursor < right:
        pieces.append((cursor, right))
    return pieces


def document_page(content: str, start: int, focus: str, limit: int) -> str:
    """Offsets are Python character indices in the cached, redacted document."""
    total = len(content)
    if start >= total:
        return f"原文共 {total} 字符，start_index={start} 已到末尾。"
    if not focus.strip():
        end = min(total, start + limit)
        hint = f"\n继续阅读请传 start_index={end}" if end < total else "\n已到缓存正文末尾。"
        return f"原文区间 [{start}, {end}) / 已获取正文 {total} 字符\n\n{content[start:end]}{hint}"
    # Scan headings across the entire document, including text before start.
    headings = list(re.finditer(r"(?m)^ {0,3}#{1,6}[ \t]+[^\r\n]*", content))
    heading_offsets = [match.start() for match in headings]
    boundaries = sorted({0, total, *heading_offsets,
                         *(m.end() for m in re.finditer(r"\n\s*\n", content))})
    numbers = numeric_terms(focus)
    terms = list(dict.fromkeys([*sorted(numbers), *_query_terms(focus.casefold())]))[:64]
    # Match numeric tokens against the whole document so window boundaries cannot
    # turn 1429 into a false match for 429. Translation preserves character offsets.
    hits = {term: [] for term in terms}
    for match in _NUMERIC_TOKEN.finditer(content.translate(_DIGIT_PUNCTUATION)):
        token = match.group().replace(',', '').casefold()
        if token in hits:
            hits[token].append(match.span())
    for term in terms:
        if term not in numbers:
            hits[term] = [match.span() for match in re.finditer(re.escape(term), content, re.I)]
    blocks = []
    frequencies = []
    for left, right in zip(boundaries, boundaries[1:]):
        for offset in range(max(left, start), right, 1920):
            end = min(offset + 2000, right)
            heading_index = bisect_right(heading_offsets, offset) - 1
            heading = headings[heading_index].group() if heading_index >= 0 else ""
            text = (heading + "\n" + content[offset:end]).casefold()
            heading_numbers = numeric_terms(heading)
            counts = {}
            for term in terms:
                body_count = sum(offset <= a and b <= end for a, b in hits[term])
                heading_count = int(term in heading_numbers) if term in numbers else heading.casefold().count(term)
                counts[term] = body_count + heading_count
            frequencies.append(counts)
            blocks.append((offset, end, len(text)))
            if end == right:
                break
    weights = {term: math.log(1 + len(blocks)/(1 + sum(row[term] > 0 for row in frequencies)))
               for term in terms}
    scored = []
    for (left, right, length), counts in zip(blocks, frequencies):
        score = sum(weights[term] * count/(1 + count + length/1000) for term, count in counts.items())
        if score:
            scored.append((score, left, right))
    if not scored:
        return "未匹配 focus，按原文顺序展示。\n" + document_page(content, start, "", limit)
    selected = []
    used = []
    remaining = limit
    for _, left, right in sorted(scored, key=lambda row: (-row[0], row[1])):
        if remaining <= 0:
            break
        for left, right in _uncovered(left, right, used):
            if right - left > remaining:
                anchors = sorted((a, b) for spans in hits.values() for a, b in spans
                                 if left <= a and b <= right and b - a <= remaining)
                if anchors:
                    anchor_start, anchor_end = anchors[0]
                    context = min(80, max(0, (remaining - (anchor_end - anchor_start)) // 2))
                    left = max(left, anchor_start - context)
            end = min(right, left + remaining)
            if end <= left:
                break
            selected.append(f"原文区间 [{left}, {end})\n{content[left:end]}")
            used.append((left, end))
            remaining -= end - left
    return (f"聚焦摘录 / 已获取正文 {total} 字符；命中 {len(scored)} 个候选片段，"
            f"展示 {len(selected)} 个去重区间；可用 start_index 查看原文上下文。\n\n"
            + "\n\n".join(selected))


def github_repository(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname != 'github.com' or parts.port not in (None, 443)
                or parts.username or parts.password or parts.query or parts.fragment):
            return None
        match = re.fullmatch(r'/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)/?', parts.path)
        if match:
            repository = match[2].removesuffix('.git')
            if repository and repository not in ('.', '..'):
                return match[1], repository
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
