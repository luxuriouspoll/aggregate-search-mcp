"""Perplexity（https://docs.perplexity.ai）

搜索: POST https://api.perplexity.ai/chat/completions
      body: {model, messages, return_citations, ...}
      -> choices[0].message.content（答案）+ search_results[] / citations[]（独立网页）

答案作为独立摘要，不给它冒用引用网页的 URL 或日期；网页优先使用有元数据的
search_results，旧版 citations 仅补没有重复的引用 URL。
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from .base import (
    ProviderError,
    SearchResult,
    as_dict,
    as_items,
    date_metadata,
    normalized_urls,
    post_json,
    query_with_sites,
    text_of,
    wants_domains,
    wants_time,
)

CHAT_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "sonar"


def _perplexity_filters() -> dict:
    extra: dict = {}
    recency = wants_time("perplexity")
    if recency:
        extra["search_recency_filter"] = recency
    domains = wants_domains()
    if domains and len(domains) <= 10:
        extra["search_domain_filter"] = list(domains)
    return extra


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        CHAT_URL,
        key=key,
        payload={
            "model": DEFAULT_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "Be precise and concise. Answer in the user's language.",
                },
                # 超过原生名单的预算时整组改用 site:；不静默丢弃第 11–20 个站点。
                {"role": "user", "content": query_with_sites(query) if len(wants_domains()) > 10 else query},
            ],
            "return_citations": True,
            "return_related_questions": False,
            **_perplexity_filters(),
        },
    )
    return _evidence(data, query, max_results)


def _evidence(data: dict, query: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()

    def add(url, title="", snippet="", published="", dates=()) -> None:
        url = text_of(url)
        key, _ = normalized_urls(url)
        if not key or key in seen or len(results) >= limit:
            return
        seen.add(key)
        results.append(SearchResult(
            source="perplexity", url=url, title=text_of(title), snippet=text_of(snippet)[:1200],
            rank=len(results), published=text_of(published), dates=dates,
        ))

    for page in as_items(data, "search_results"):
        # Sonar 的 ApiPublicSearchResult.date 明指发布日期；last_updated 仅作元数据。
        add(page.get("url"), page.get("title"), page.get("snippet"), page.get("date"),
            date_metadata(last_updated=page.get("last_updated")))
        if len(results) >= limit:
            return results

    choices = as_items(data, "choices")
    answer = text_of(as_dict(choices[0].get("message")).get("content")) if choices else ""
    summary_url = f"https://www.perplexity.ai/search?q={quote(query, safe='')}"
    summary_title = f"[Perplexity 摘要] {query}"
    if answer and not results and not (wants_domains() or wants_time("perplexity")):
        add(summary_url, summary_title, answer)  # 无筛选的旧版响应仍把答案放在首条
    citations = data.get("citations")
    for url in citations if isinstance(citations, list) else ():
        add(url)
        if len(results) >= limit:
            break
    if answer and len(results) < limit:
        add(summary_url, summary_title, answer)
    if not results:
        raise ProviderError("Perplexity 未返回可用网页、引用或答案")
    return results
