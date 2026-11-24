"""火山引擎联网搜索（https://www.volcengine.com/docs/85508/1650263）

搜索: POST https://open.feedcoopapi.com/search_api/web_search
      头: Authorization: Bearer <api_key>、X-Traffic-Tag
      body: {Query, SearchType: "web", Count, TimeRange?}
      -> Result.WebResults[]: {Title, Url, Summary/Snippet, SiteName, PublishTime,
                               AuthInfoDes, RankScore}

无抓取接口；返回的 Content/Summary 本身就是正文摘要，量大时可用作粗抓取。
"""
from __future__ import annotations

import httpx

from providers.base import ProviderError, SearchResult, as_items, expect_dict, post_json

SEARCH_URL = "https://open.feedcoopapi.com/search_api/web_search"
TRAFFIC_TAG = "X-Traffic-Tag"
TRAFFIC_VALUE = "skill_web_search_common"

# 这些业务码代表「换一个 key 可能就好了」（key 无效 / 额度 / 限流 / 权限）
_KEY_OR_QUOTA_CODES = {
    "10403",  # 账号或权限异常
    "10406",  # 额度用尽（实测免费额度耗尽返回此码）
    "10408",  # 限流
    "invalid_api_key",
    "InvalidApiKey",
    "QuotaExceeded",
    "RateLimitExceeded",
    "AccessDenied",
}


def _is_key_or_quota_error(code: str, message: str) -> bool:
    if code in _KEY_OR_QUOTA_CODES:
        return True
    text = f"{code} {message}".lower()
    return any(
        hint in text
        for hint in ("api key", "apikey", "quota", "额度", "limit", "permission", "forbidden")
    )


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        headers={TRAFFIC_TAG: TRAFFIC_VALUE},
        payload={"Query": query, "SearchType": "web", "Count": max(1, min(max_results, 20))},
    )
    # 业务错误码在 ResponseMetadata / Result 里
    meta = data.get("ResponseMetadata") or {}
    if meta.get("Error"):
        err = meta["Error"]
        code = str(err.get("Code", ""))
        msg = str(err.get("Message", ""))
        raise ProviderError(
            f"业务错误 {code}: {msg}",
            retryable=_is_key_or_quota_error(code, msg),
        )

    result = data.get("Result") or {}
    items = as_items(expect_dict(data, "volcengine"), "Result", "WebResults")
    out: list[SearchResult] = []
    for i, item in enumerate(items):
        snippet = (item.get("Summary") or item.get("Snippet") or "").strip()
        source_site = (item.get("SiteName") or "").strip()
        if source_site:
            snippet = f"[{source_site}] {snippet}"
        out.append(
            SearchResult(
                source="volcengine",
                url=item.get("Url", ""),
                title=(item.get("Title") or "").strip(),
                snippet=snippet,
                rank=i,
                published=str(item.get("PublishTime") or ""),
            )
        )
    return out
