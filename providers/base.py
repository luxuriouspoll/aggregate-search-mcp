"""provider 公共基础设施：统一结果模型、HTTP 封装、URL 归一化与合并去重。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from config import FETCH_MAX_CHARS, SEARCH_TIMEOUT


class ProviderError(RuntimeError):
    """单个提供商调用失败（网络 / HTTP / 响应格式），由聚合层捕获隔离。

    status / retry_after 用于多 key 轮询判断：401/403 → key 无效，
    402/429 → 额度或限流，可据此换下一个 key 重试。
    retryable 用于「HTTP 200 但业务码表示额度问题」这类厂商（火山 / Querit / Firecrawl）。
    """

    def __init__(
        self,
        message: str,
        status: int = 0,
        retry_after: float | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self._retryable = retryable

    @property
    def retryable_with_other_key(self) -> bool:
        """换一个 key 有没有意义。"""
        if self._retryable is not None:
            return self._retryable
        return self.status in (401, 402, 403, 429)


@dataclass
class SearchResult:
    source: str        # 提供商名：tavily / querit / firecrawl / ...
    url: str
    title: str
    snippet: str = ""
    rank: int = 99     # 在该提供商结果中的原始名次（0 起），用于合并排序
    published: str = ""


@dataclass
class FetchResult:
    provider: str
    url: str
    title: str = ""
    content: str = ""


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After：秒数或 HTTP 日期都支持。"""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        import datetime as _dt
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        now = _dt.datetime.now(target.tzinfo)
        return max(0.0, (target - now).total_seconds())
    except (TypeError, ValueError):
        return None


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    key: str = "",
    payload: dict,
    headers: dict | None = None,
    auth: str = "bearer",
    timeout: float | None = None,
) -> dict:
    """统一的 POST 请求，非 200 一律抛 ProviderError（带 status / retry_after）。

    auth: bearer（Authorization: Bearer）| header:<名字> | query:<参数名> | none
    """
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    request_url = url
    if key:
        if auth == "bearer":
            merged["Authorization"] = f"Bearer {key}"
        elif auth.startswith("header:"):
            merged[auth.split(":", 1)[1]] = key
        elif auth.startswith("query:"):
            param = auth.split(":", 1)[1]
            sep = "&" if "?" in url else "?"
            request_url = f"{url}{sep}{param}={key}"

    try:
        resp = await client.post(
            request_url, json=payload, headers=merged, timeout=timeout or SEARCH_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"网络错误: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(
            f"HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code,
            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(f"响应不是 JSON: {resp.text[:200]}") from exc


async def get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    key: str = "",
    headers: dict | None = None,
    auth: str = "bearer",
    timeout: float | None = None,
) -> str:
    """GET 文本（Jina reader / SearXNG 之类返回非标准格式的端点）。"""
    merged = dict(headers or {})
    request_url = url
    if key:
        if auth == "bearer":
            merged["Authorization"] = f"Bearer {key}"
        elif auth.startswith("header:"):
            merged[auth.split(":", 1)[1]] = key
        elif auth.startswith("query:"):
            param = auth.split(":", 1)[1]
            sep = "&" if "?" in url else "?"
            request_url = f"{url}{sep}{param}={key}"
    try:
        resp = await client.get(request_url, headers=merged, timeout=timeout or SEARCH_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ProviderError(f"网络错误: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(
            f"HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code,
            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
        )
    return resp.text


def truncate(text: str) -> str:
    """抓取正文统一截断，避免撑爆模型上下文。"""
    if len(text) > FETCH_MAX_CHARS:
        return text[:FETCH_MAX_CHARS] + "\n\n…（内容过长，已截断）"
    return text


# ---- 搜索结果合并 ----

# 跟踪参数一律剔除；参数排序后参与去重，保证 ?a=1&b=2 与 ?b=2&a=1 命中同一条。
# 注意：from / src / ref / fr 不在此列——很多站点用它们做功能跳转（分页、来源、
# 目标地址），剔除既会把不同页面误合并，也会让输出的链接失效。
_TRACKING_KEY = re.compile(
    r"^(utm_\w+|spm|vd_source|share_token|share_source|"
    r"fbclid|gclid|msclkid|yclid|mc_cid|mc_eid|igshid|_hsenc|_hsmi)$",
    re.I,
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """去重用的 URL 归一化。

    处理：小写 scheme/host、去 www、去默认端口、去跟踪参数、参数排序、去末尾斜杠。
    fragment 只在是纯锚点（#section）时丢弃；hash 路由（#/path）保留，
    否则会把单页应用的不同页面错误合并成一条。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        return url  # 非 http 原样返回，交给调用方过滤
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    host = parts.netloc.lower().removeprefix("www.")
    if ":" in host:
        hostname, _, port = host.rpartition(":")
        if port.isdigit() and int(port) == _DEFAULT_PORTS.get(scheme):
            host = hostname
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_KEY.match(k)
    ]
    pairs.sort()
    path = parts.path.rstrip("/") or "/"
    # hash 路由（#/x、#!/x）保留，纯锚点丢弃
    fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
    return urlunsplit((scheme, host, path, urlencode(pairs), fragment))


def merge_results(groups: dict[str, list[SearchResult]]) -> list[dict]:
    """合并各家结果并去重。

    同一条 URL 聚合出所有来源；排序：命中家数多在前，同分按最佳原始名次。
    返回项字段：url / title / snippet / published / sources / best_rank。
    """
    merged: dict[str, dict] = {}
    for results in groups.values():
        for r in results:
            key = normalize_url(r.url)
            if not key.startswith(("http://", "https://")):
                continue
            item = merged.setdefault(key, {
                "url": key,  # 输出归一化后的干净 URL（去跟踪参数等）
                "title": r.title,
                "snippet": r.snippet,
                "published": r.published,
                "sources": [],
                "best_rank": r.rank,
            })
            if r.source not in item["sources"]:
                item["sources"].append(r.source)
            if len(r.title) > len(item["title"]):
                item["title"] = r.title
            if len(r.snippet) > len(item["snippet"]):
                item["snippet"] = r.snippet
            if r.published and not item["published"]:
                item["published"] = r.published
            item["best_rank"] = min(item["best_rank"], r.rank)
    return sorted(merged.values(), key=lambda x: (-len(x["sources"]), x["best_rank"]))


def expect_dict(data, provider: str) -> dict:
    """上游返回非 dict 时抛 ProviderError，而不是让 AttributeError 逃出去。"""
    if not isinstance(data, dict):
        raise ProviderError(f"{provider} 响应格式异常（期望对象，实际 {type(data).__name__}）")
    return data


def as_items(data, *path):
    """按路径取列表并过滤掉非 dict 项；任一层不是预期类型就返回空列表。"""
    node = data
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, dict)]
