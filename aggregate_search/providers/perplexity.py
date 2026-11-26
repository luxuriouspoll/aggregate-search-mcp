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
    brief,
    date_metadata,
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
    if domains:
        extra["search_domain_filter"] = list(domains[:10])
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
                {"role": "user", "content": query},
            ],
            "return_citations": True,
            "return_related_questions": False,
            **_perplexity_filters(),
        },
    )
    choices = as_items(data, "choices")
    if not choices:
        raise ProviderError(f"无 choices: {brief(data)}")
    answer = text_of(as_dict(choices[0].get("message")).get("content"))
    citations = data.get("citations")
    citations = [c for c in citations if isinstance(c, str)] if isinstance(citations, list) else []
    if not answer and not citations:
        raise ProviderError("Perplexity 未返回答案或引用")

    results: list[SearchResult] = []
    if answer:
        results.append(
            SearchResult(
                source="perplexity",
                url=f"https://www.perplexity.ai/search?q={quote(query, safe='')}",
                title=f"[Perplexity 摘要] {query}",
                snippet=answer[:1200],
                rank=0,
            )
        )
    # 引用条数不要顶破 max_results，也别和答案条重复
    remaining = max(0, max_results - len(results))
    for i, url in enumerate(citations[:remaining]):
        results.append(
            SearchResult(
                source="perplexity",
                url=url,
                title="",
                snippet="",
                rank=len(results),
            )
        )
    return results
