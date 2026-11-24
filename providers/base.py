"""provider 公共基础设施：统一结果模型、HTTP 封装、URL 归一化与合并去重。"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
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
    r"^(utm_\w+|spm|vd_source|share_source|"
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


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _dedupe_text(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        text = _clean_text(raw)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _reciprocal_rank_score(sources: list[str], ranks: dict[str, int]) -> float:
    """RRF 风格排序：兼顾多家共识和各厂商原始名次。"""
    return sum(1.0 / (12 + ranks.get(source, 99)) for source in sources)


def _sort_key(item: dict) -> tuple:
    return (-item["source_count"], -item["score"], item["best_rank"], item["url"])


def _refresh_score(item: dict) -> None:
    item["source_count"] = len(item["sources"])
    item["score"] = _reciprocal_rank_score(item["sources"], item["ranks"])
    item["best_rank"] = min(item["ranks"].values(), default=99)


def _meaningful_tokens(text: str) -> set[str]:
    """抽取版本号、日期、金额、条款编号等数字实体，供冲突守卫使用。"""
    return set(re.findall(
        r"(?<![\w])(?:v?\d+(?:\.\d+)+|\d{4}-\d{1,2}-\d{1,2}|\d+(?:\.\d+)?%?)(?![\w])",
        text.casefold(),
    ))


def _has_conflicting_facts(a: dict, b: dict) -> bool:
    left = _meaningful_tokens(a.get("title", "") + " " + a.get("snippet", ""))
    right = _meaningful_tokens(b.get("title", "") + " " + b.get("snippet", ""))
    return left != right


def merge_results(groups: dict[str, list[SearchResult]]) -> list[dict]:
    """把多厂商结果汇总为证据包。

    同一 URL 的多份标题、摘要、发布日期均保留；不再只取最长摘要。
    输出包含 evidence[]（source/rank/snippet/published）和 sources[]，
    方便上层生成回答时追溯每条信息来自哪家厂商。
    """
    merged: dict[str, dict] = {}
    for results in groups.values():
        for r in results:
            key = normalize_url(r.url)
            if not key.startswith(("http://", "https://")):
                continue
            item = merged.setdefault(key, {
                "url": key,
                "title": _clean_text(r.title),
                "titles": [],
                "snippet": _clean_text(r.snippet),
                "snippets": [],
                "published": r.published,
                "published_dates": [],
                "sources": [],
                "evidence": [],
                "ranks": {},
                "best_rank": r.rank,
            })
            if r.source not in item["sources"]:
                item["sources"].append(r.source)
            item["ranks"][r.source] = min(item["ranks"].get(r.source, 99), r.rank)
            item["evidence"].append({
                "source": r.source,
                "url": key,
                "title": _clean_text(r.title),
                "rank": r.rank,
                "snippet": _clean_text(r.snippet),
                "published": r.published,
            })
            item["titles"] = _dedupe_text(item["titles"] + [r.title])
            item["snippets"] = _dedupe_text(item["snippets"] + [r.snippet])
            item["published_dates"] = _dedupe_text(item["published_dates"] + [r.published])
            # 标题取信息更充分的版本，而不是简单比较字符串长度。
            if len(_clean_text(r.title)) > len(item["title"]):
                item["title"] = _clean_text(r.title)
            if len(_clean_text(r.snippet)) > len(item["snippet"]):
                item["snippet"] = _clean_text(r.snippet)
            if r.published and not item["published"]:
                item["published"] = r.published
            item["best_rank"] = min(item["best_rank"], r.rank)

    for item in merged.values():
        _refresh_score(item)
    # 多家命中优先，其次按原始名次的倒数排名融合。
    return sorted(merged.values(), key=_sort_key)


def fold_near_duplicates(items: list[dict], threshold: float = 0.94) -> tuple[list[dict], int]:
    """折叠高度近似的转载结果，同时保留 alternate_urls 和全部厂商来源。

    采用保守阈值，且只在标题高度相似、摘要也高度相似时折叠。
    不把同一主题的不同文章合并；无法判断时保留两条，比误删证据更安全。
    """
    kept: list[dict] = []
    folded = 0
    for item in items:
        title = _clean_text(item.get("title", "")).casefold()
        snippet = _clean_text(item.get("snippet", "")).casefold()
        target = None
        if len(title) >= 10 and len(snippet) >= 30:
            for existing in kept:
                other_title = _clean_text(existing.get("title", "")).casefold()
                other_snippet = _clean_text(existing.get("snippet", "")).casefold()
                if len(other_snippet) < 30 or _has_conflicting_facts(item, existing):
                    continue
                title_sim = SequenceMatcher(None, title, other_title).ratio()
                if title_sim < threshold:
                    continue
                snippet_sim = SequenceMatcher(None, snippet, other_snippet).ratio()
                if snippet_sim >= threshold:
                    target = existing
                    break
        if target is None:
            item["alternate_urls"] = []
            kept.append(item)
            continue
        target.setdefault("alternate_urls", []).append(item["url"])
        target["sources"] = list(dict.fromkeys(target["sources"] + item["sources"]))
        target["source_count"] = len(target["sources"])
        target["evidence"].extend(item["evidence"])
        target["titles"] = _dedupe_text(target["titles"] + item["titles"])
        target["snippets"] = _dedupe_text(target["snippets"] + item["snippets"])
        target["published_dates"] = _dedupe_text(
            target["published_dates"] + item["published_dates"]
        )
        target["ranks"].update({
            source: min(rank, target["ranks"].get(source, 99))
            for source, rank in item["ranks"].items()
        })
        folded += 1
    for item in kept:
        _refresh_score(item)
    kept.sort(key=_sort_key)
    return kept, folded


def decode_mcp_response(response: httpx.Response) -> dict:
    """解码 Streamable HTTP MCP 的 JSON 或 SSE 响应，并归类鉴权/限流错误。"""
    if response.status_code >= 400:
        raise ProviderError(
            f"HTTP {response.status_code}: {response.text[:200]}",
            status=response.status_code,
            retry_after=_parse_retry_after(response.headers.get("Retry-After")),
        )
    text = response.text.strip()
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not payloads:
            raise ProviderError("MCP SSE 响应没有 data")
        # 通知/进度事件不一定有 id；取最后一个有效 JSON-RPC 响应。
        for payload in reversed(payloads):
            try:
                candidate = json.loads(payload)
            except ValueError:
                continue
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                text = payload
                break
        else:
            raise ProviderError("MCP SSE 没有有效响应事件")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProviderError(f"MCP 响应不是 JSON: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise ProviderError("MCP 响应不是对象")
    if data.get("error"):
        error = data["error"]
        message = str(error)[:300]
        lowered = message.lower()
        status = 429 if any(h in lowered for h in ("rate", "too many", "limit")) else 0
        if status == 0 and any(h in lowered for h in ("key", "auth", "unauthorized", "forbidden")):
            status = 401
        if status == 0 and any(h in lowered for h in ("quota", "credit", "payment")):
            status = 402
        raise ProviderError(message, status=status, retryable=bool(status))
    result = data.get("result")
    if not isinstance(result, dict):
        raise ProviderError("MCP 响应缺少 result 对象")
    return result


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


def strict_items(data, provider: str, *path) -> list[dict]:
    """读取契约要求存在的结果列表；明确空列表合法，结构变化则报错。"""
    node = data
    for depth, key in enumerate(path):
        if not isinstance(node, dict) or key not in node:
            raise ProviderError(f"{provider} 响应缺少 {'.'.join(path[:depth + 1])}")
        node = node[key]
    if not isinstance(node, list):
        raise ProviderError(f"{provider} 响应 {'.'.join(path)} 不是列表")
    return [item for item in node if isinstance(item, dict)]
