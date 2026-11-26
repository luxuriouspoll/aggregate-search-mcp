"""Perplexity（https://docs.perplexity.ai）

搜索: POST https://api.perplexity.ai/chat/completions
      body: {model, messages, return_citations, ...}
      -> choices[0].message.content（答案）+ citations[]（引用 URL 列表）

Perplexity 只给「答案 + 引用链接」，没有逐条标题/摘要，因此：
- 把答案正文作为第 0 条结果（source=perplexity，url 取第一条引用或搜索页）
- 每个引用 URL 各生成一条结果，snippet 留空，由其他家补全
这样在多源合并里它既贡献独家原文，也不会把别人的摘要冲掉。
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from .base import ProviderError, SearchResult, as_dict, as_items, brief, post_json, text_of

CHAT_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "sonar"


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
