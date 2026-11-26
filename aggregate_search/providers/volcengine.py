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

from .base import (
    ProviderError,
    SearchResult,
    as_items,
    brief,
    business_status,
    post_json,
    text_of,
)

SEARCH_URL = "https://open.feedcoopapi.com/search_api/web_search"
TRAFFIC_TAG = "X-Traffic-Tag"
TRAFFIC_VALUE = "skill_web_search_common"

# 明确的业务码 → 等价的状态码。有了状态码，key 池才知道该换 key、该冷却多久。
# 不在表里的业务码只认明确的文案（见 business_status）。
_CODE_STATUS = {
    "10408": 429, "RateLimitExceeded": 429,          # 限流
    "10406": 402, "QuotaExceeded": 402,              # 额度用尽（实测免费额度耗尽返回 10406）
    "10403": 403, "AccessDenied": 403,               # 账号或权限异常
    "invalid_api_key": 401, "InvalidApiKey": 401,    # key 无效
}


async def search(client: httpx.AsyncClient, key: str, query: str, max_results: int) -> list[SearchResult]:
    data = await post_json(
        client,
        SEARCH_URL,
        key=key,
        headers={TRAFFIC_TAG: TRAFFIC_VALUE},
        payload={"Query": query, "SearchType": "web", "Count": max(1, min(max_results, 20))},
    )
    # 业务错误码在 ResponseMetadata.Error 里
    meta = data.get("ResponseMetadata")
    err = meta.get("Error") if isinstance(meta, dict) else None
    if err:
        if not isinstance(err, dict):
            err = {"Message": str(err)}
        code = str(err.get("Code") or "")
        msg = str(err.get("Message") or "")
        # 以前用 "rate" 做子串匹配，"generate" / "moderate" 这类词也会被当成限流
        status = _CODE_STATUS.get(code) or business_status(f"{code} {msg}")
        raise ProviderError(
            brief(f"业务错误 {code}: {msg}", 300),
            status=status,
            retryable=bool(status),
            soft=code not in _CODE_STATUS,
        )

    items = as_items(data, "Result", "WebResults")
    out: list[SearchResult] = []
    for i, item in enumerate(items):
        snippet = text_of(item.get("Summary")) or text_of(item.get("Snippet"))
        source_site = text_of(item.get("SiteName"))
        if source_site:
            snippet = f"[{source_site}] {snippet}"
        out.append(
            SearchResult(
                source="volcengine",
                url=text_of(item.get("Url")),
                title=text_of(item.get("Title")),
                snippet=snippet,
                rank=i,
                published=text_of(item.get("PublishTime")),
            )
        )
    return out
