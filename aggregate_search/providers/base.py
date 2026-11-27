"""provider 公共基础设施：统一结果模型、HTTP 封装、URL 归一化与合并去重。"""
from __future__ import annotations

import contextvars
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from ipaddress import ip_address
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .. import config


class ProviderError(RuntimeError):
    """单个提供商调用失败（网络 / HTTP / 响应格式），由聚合层捕获隔离。

    status / retry_after 用于多 key 轮询判断：401/403 → key 无效，
    402/429 → 额度或限流，可据此换下一个 key 重试。
    retryable 用于「HTTP 200 但业务码表示额度问题」这类厂商（火山 / Querit / Firecrawl）。
    soft 表示 status 是从报错文案里猜出来的，不一定准：冷却时从轻处理，
    不会因为一句含糊的文案就把 key 当成失效封上一整天。
    """

    def __init__(
        self,
        message: str,
        status: int = 0,
        retry_after: float | None = None,
        retryable: bool | None = None,
        soft: bool = False,
        sent: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self._retryable = retryable
        self.soft = soft
        # False：请求根本没发出去（key 全在冷却、到了用量上限等），不占额度，也不算一次失败的尝试
        self.sent = sent

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
    published: str = ""  # 仅字段语义明确为发布日期时填写
    dates: tuple[tuple[str, str], ...] = ()  # 上游的其他日期及其类型，不假称发布
    engines: tuple[str, ...] = ()


@dataclass
class FetchResult:
    provider: str
    url: str
    title: str = ""
    content: str = ""
    auth: str = ""     # 由调度层填写：anon（匿名）/ key（用了账号 key）
    truncated: bool = False


def redact(text: str) -> str:
    """把已配置的 key 和网址里的账号口令从文本里抹掉。

    长的 key 先替换：一个 key 恰好是另一个的前缀时，不会留下后半截。
    """
    if not text:
        return text
    out = text
    for secret in config.redaction_secrets():
        out = out.replace(secret, "***")
    # 网址里 // 和 @ 之间的账号口令部分（代理地址、自建实例地址都可能带）
    return re.sub(r"(//)[^/@\s]+@", r"\1***@", out)


_URL_QUERY = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]*")


def scrub_urls(text: str) -> str:
    """写进本地记录前，把文本里所有网址 ? 后面的参数去掉（预签名链接、token 不该落盘），并脱敏。"""
    return _URL_QUERY.sub(r"\1?…", redact(text or ""))


def brief(text, limit: int = 200) -> str:
    """把响应体之类的内容放进报错前先过一遍：先脱敏再截断。

    顺序不能反：先截断的话，key 正好跨在截断处时会留下前半截。
    """
    return redact(str(text or ""))[:limit]


# 只认完整短语。以前用 "key" / "limit" / "auth" 这类单词做子串匹配，
# 「Unrecognized key(s) in object」「limit must be <= 10」这种参数错误也会被当成 key 失效或限流。
_RATE_RE = re.compile(
    r"rate[ _-]?limit|too many requests|\bqps\b|限流|频率|过于频繁", re.I
)
_AUTH_RE = re.compile(
    r"invalid[ _-]?(?:api[ _-]?)?key|api[ _-]?key (?:is )?(?:invalid|missing|required|not found)"
    r"|missing api[ _-]?key|unauthori[sz]ed|authentication (?:failed|required)"
    r"|invalid[ _-]?token|token is invalid|鉴权失败|无效的",
    re.I,
)
_QUOTA_RE = re.compile(
    r"quota|\bcredits?\b|\bbalance\b|payment required|insufficient[ _-]?funds|余额|额度|欠费",
    re.I,
)


def business_status(text: str) -> int:
    """HTTP 200 但正文在报错时，从文案里猜出等价的状态码，供换 key 和冷却判断。

    429 限流 / 401 鉴权 / 402 额度；认不出来返回 0（只当次失败，不标记 key）。
    只用于搜索接口的报错。抓取的报错请用 fetch_error()。
    """
    text = text or ""
    if _RATE_RE.search(text):
        return 429
    if _AUTH_RE.search(text):
        return 401
    if _QUOTA_RE.search(text):
        return 402
    return 0


def business_error(message: str) -> ProviderError:
    """把「HTTP 200 + 报错文案」包装成 ProviderError。状态是猜的，所以标记为 soft。"""
    status = business_status(message)
    return ProviderError(brief(message, 300), status=status, retryable=bool(status), soft=True)


def fetch_error(message: str) -> ProviderError:
    """抓取工具自己报的错：只当这一次失败，不拿它判断账号状态。

    这类文案说的多半是目标网页（「403 Forbidden」「需要登录」「被限流」），
    不是我们的 key。据此冷却或换 key，只会白白多花几次额度。
    """
    return ProviderError(brief(message, 300), soft=True)


# 所有网络层失败都以它开头；补搜时一整轮全是它，说明网络不通，就不再递补
NETWORK_ERROR = "网络错误"


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

    auth: bearer（Authorization: Bearer）| header:<名字> | none
    key 只放请求头，不拼进 URL——URL 会出现在日志和报错里。
    """
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    if key:
        if auth == "bearer":
            merged["Authorization"] = f"Bearer {key}"
        elif auth.startswith("header:"):
            merged[auth.split(":", 1)[1]] = key

    try:
        resp = await client.post(
            url, json=payload, headers=merged, timeout=timeout or config.get_settings().SEARCH_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise ProviderError(f"{NETWORK_ERROR}: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(
            f"HTTP {resp.status_code}: {brief(resp.text)}",
            status=resp.status_code,
            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(f"响应不是 JSON: {brief(resp.text)}") from exc
    if not isinstance(data, dict):
        # 所有适配器都按「对象」来读返回值；上游哪天改成数组，这里统一报清楚，
        # 不让各个适配器里冒出 AttributeError
        raise ProviderError(f"响应格式异常（期望 JSON 对象，实际是 {type(data).__name__}）")
    return data


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
    if key:
        if auth == "bearer":
            merged["Authorization"] = f"Bearer {key}"
        elif auth.startswith("header:"):
            merged[auth.split(":", 1)[1]] = key
    try:
        resp = await client.get(url, headers=merged, timeout=timeout or config.get_settings().SEARCH_TIMEOUT)
    except httpx.HTTPError as exc:
        raise ProviderError(f"{NETWORK_ERROR}: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise ProviderError(
            f"HTTP {resp.status_code}: {brief(resp.text)}",
            status=resp.status_code,
            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
        )
    return resp.text


def truncate(text: str) -> str:
    """抓取正文统一截断，避免撑爆模型上下文。"""
    limit = config.get_settings().FETCH_CACHE_MAX_CHARS
    if len(text) > limit:
        return text[:limit] + "\n\n…（内容过长，已截断）"
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
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HOST_DOTS = str.maketrans("。．｡", "...")
_BAD_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")


def _hostname(host: str) -> str:
    host = host.translate(_HOST_DOTS).removesuffix(".").lower()
    if not host or "%" in host:
        return ""
    if ":" in host:
        try:
            return str(ip_address(host))
        except ValueError:
            return ""
    if not host.isascii():
        try:
            host = httpx.URL(host=host).raw_host.decode("ascii")
        except (httpx.InvalidURL, UnicodeError):
            return ""
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(part) for part in host.split(".")):
        return ""
    return host


def _url_parts(url: str) -> tuple[SplitResult, str] | None:
    if not url.lower().startswith(("http://", "https://")) or _BAD_URL_CHARS.search(url):
        return None
    try:
        parts = urlsplit(url)
        if "@" in parts.netloc or parts.netloc.endswith(":"):
            return None
        _ = parts.port  # 拒绝非法端口
        host = _hostname(parts.hostname or "")
    except ValueError:
        return None
    return (parts, host) if host else None


def _host_in_domains(host: str, domains: Iterable[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def normalized_urls(url: str) -> tuple[str, str]:
    """返回去重键与证据链接；证据链接不能改变原始主机（尤其是 www. 限定）。"""
    parsed = _url_parts(url)
    if parsed is None:
        return "", ""
    parts, hostname = parsed
    scheme = parts.scheme
    port = parts.port
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_KEY.match(k)
    ]
    pairs.sort()
    path = parts.path.rstrip("/") or "/"
    # hash 路由（#/x、#!/x）保留，纯锚点丢弃
    fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
    query = urlencode(pairs)
    actual = urlunsplit((scheme, host, path, query, fragment))
    key = urlunsplit((scheme, host.removeprefix("www."), path, query, fragment))
    return key, actual


def normalize_url(url: str) -> str:
    """去重键：小写 scheme/host、去 www、默认端口、跟踪参数和纯锚点，排序查询参数。"""
    url = (url or "").strip()
    return normalized_urls(url)[0] or url  # 非法或非 HTTP URL 原样交给调用方


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


def is_official_url(url: str, domains: Iterable[str] | None = None) -> bool:
    """这个网址是不是官方一手来源（政策站点、官方文档等）。"""
    parsed = _url_parts(url)
    if parsed is None:
        return False
    if domains is None:
        domains = config.get_settings().OFFICIAL_SOURCE_DOMAINS
    return _host_in_domains(parsed[1], (host for raw in domains if (host := _hostname(raw))))


# 官方一手来源的等效来源数。取 1.2 的效果：
#   官网单源(1+1.2=2.2) > 两家低质转载(2.0)，但 < 三家共识(3.0)。
# 也就是说「官方原文」值得优先读，但多家独立来源互相印证仍然更强。
OFFICIAL_SOURCE_WEIGHT = 1.2


def _weight(item: dict) -> float:
    """排序用的等效来源数：同一 URL 的共识 + 官方来源加成。"""
    return item["consensus_source_count"] + (OFFICIAL_SOURCE_WEIGHT if item["is_official"] else 0.0)


def _sort_key(item: dict) -> tuple:
    # 第一优先是等效来源数（含官方加成），其次 RRF 分值，再看最佳名次和网址
    return (-_weight(item), -item["score"], item["best_rank"], item["url"])


def _refresh_score(item: dict, official_domains: Iterable[str]) -> None:
    item["source_count"] = len(item["sources"])
    item["is_official"] = is_official_url(item["url"], official_domains)
    # 近重簇的排序/共识使用同一 URL 的最佳证据，不把转载间的厂商计数相加。
    candidates: dict[str, dict[str, int]] = {}
    for evidence in item["evidence"]:
        canonical = normalize_url(evidence["url"])
        ranks = candidates.setdefault(canonical, {})
        source, rank = evidence["source"], evidence["rank"]
        ranks[source] = min(ranks.get(source, rank), rank)
    canonical, ranks = max(
        candidates.items(),
        key=lambda pair: (len(pair[1]), _reciprocal_rank_score(list(pair[1]), pair[1])),
    )
    item["consensus_url"] = next(e["url"] for e in item["evidence"] if normalize_url(e["url"]) == canonical)
    item["consensus_sources"] = list(ranks)
    item["consensus_source_count"] = len(ranks)
    item["score"] = _reciprocal_rank_score(item["consensus_sources"], ranks)
    item["best_rank"] = min(item["ranks"].values(), default=99)


# 数字允许紧邻中文，但不从英文单词/型号中截取，也不从更大的数值里截取子串。
# 逗号只在正规千位分组时作为数字的一部分，中文句逗不会吞掉随后的数字。
_NUMERIC_CORE = r"(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)*(?:[eE][+-]?\d+)?"
_NUMERIC_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?<!\d[.,])"
    r"(?:v\d+(?:\.\d+)+|\d{4}-\d{1,2}-\d{1,2}|[+-]?" + _NUMERIC_CORE + r"%?)"
    r"(?![A-Za-z0-9_]|\d|\.\d|,\d{3}(?!\d))",
    re.I | re.ASCII,
)
# 不把关键数字、金额币种和常见单位不同的文章折叠成一份证据。
_CURRENCY = r"人民币|美元|美金|港元|港币|欧元|英镑|日元|日圆|韩元|澳元|加元|元|cny|rmb|usd|hkd|eur|gbp|jpy|krw|aud|cad"
_UNIT = (
    r"%|‰|[万亿千]?(?:" + _CURRENCY + r")|[万亿千]|"
    r"千克|公斤|毫克|克|公吨|吨|千米|公里|厘米|毫米|米|小时|分钟|秒|天|年|月|日|"
    r"(?:kg|mg|g|km|cm|mm|m|mph|kwh|kw|ghz|mhz|hz|tb|gb|mb|kb|ms|s)(?![a-z])"
)
_FACT_NUMBER = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|(?:\b(?:" + _CURRENCY + r")\s*|[$€£¥₩]\s*)?[+-]?"
    + _NUMERIC_CORE + r"(?:\s*(?:" + _UNIT + r"))?", re.I
)
_DIGIT_PUNCTUATION = str.maketrans("０１２３４５６７８９，．＋－％−", "0123456789,.+-%-")


def numeric_terms(text: str) -> set[str]:
    """完整数字检索词，防止 42 命中 142、42.1、V42 或不同百分比。"""
    return {token.replace(",", "").casefold() for token in _NUMERIC_TOKEN.findall(text.translate(_DIGIT_PUNCTUATION))}


def _numeric_facts(text: str) -> tuple[str, ...]:
    # 用 NFKC 令全角数字、货币符号可比较。保留顺序，金额对调也不是同一份证据。
    return tuple(re.sub(r"[\s,]", "", token).casefold() for token in _FACT_NUMBER.findall(
        unicodedata.normalize("NFKC", text).translate(_DIGIT_PUNCTUATION)
    ))


def _fact_signatures(item: dict) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    titles = {_numeric_facts(title) for title in item.get("titles", ()) or (item.get("title", ""),)}
    snippets = {_numeric_facts(snippet) for snippet in item.get("snippets", ()) or (item.get("snippet", ""),)}
    return titles - {()}, snippets - {()}


def merge_results(groups: dict[str, list[SearchResult]]) -> list[dict]:
    """把多厂商结果汇总为证据包，保留同一 URL 的各份标题、摘要与有类型的日期。"""
    official_domains = config.get_settings().OFFICIAL_SOURCE_DOMAINS
    merged: dict[str, dict] = {}
    for results in groups.values():
        for r in results:
            key, url = normalized_urls(text_of(r.url))
            if not key:
                continue
            title, snippet = _clean_text(r.title), _clean_text(r.snippet)
            published = text_of(r.published)
            item = merged.setdefault(key, {
                "url": url,
                "title": title,
                "titles": [],
                "snippet": snippet,
                "snippets": [],
                "published": published,
                "published_dates": [],
                "sources": [],
                "evidence": [],
                "ranks": {},
            })
            if url == key:
                item["url"] = url  # 无 www. 版本确实出现过时优先选它
            if r.source not in item["sources"]:
                item["sources"].append(r.source)
            item["ranks"][r.source] = min(item["ranks"].get(r.source, r.rank), r.rank)
            item["evidence"].append({
                "source": r.source,
                "url": url,
                "title": title,
                "rank": r.rank,
                "snippet": snippet,
                "published": published,
                "dates": dict(r.dates),
                "engines": list(r.engines),
            })
            item["titles"] = _dedupe_text(item["titles"] + [title])
            item["snippets"] = _dedupe_text(item["snippets"] + [snippet])
            item["published_dates"] = _dedupe_text(item["published_dates"] + [published])
            if len(title) > len(item["title"]):
                item["title"] = title
            if len(snippet) > len(item["snippet"]):
                item["snippet"] = snippet
            if published and not item["published"]:
                item["published"] = published

    for item in merged.values():
        _refresh_score(item, official_domains)
    # 多家命中优先，其次按原始名次的倒数排名融合。
    return sorted(merged.values(), key=_sort_key)


def fold_near_duplicates(items: list[dict], threshold: float = 0.94) -> tuple[list[dict], int]:
    """折叠高度近似的转载结果，同时保留 alternate_urls 和全部厂商来源。

    采用保守阈值，且只在标题高度相似、摘要也高度相似时折叠。
    不把同一主题的不同文章合并；无法判断时保留两条，比误删证据更安全。
    """
    kept: list[dict] = []
    facts: list[tuple[set[tuple[str, ...]], set[tuple[str, ...]]]] = []
    folded = 0
    official_domains = config.get_settings().OFFICIAL_SOURCE_DOMAINS
    for item in items:
        title = _clean_text(item.get("title", "")).casefold()
        snippet = _clean_text(item.get("snippet", "")).casefold()
        signature = _fact_signatures(item)
        target = None
        if len(title) >= 10 and len(snippet) >= 30:
            for existing, existing_signature in zip(kept, facts):
                other_title = _clean_text(existing.get("title", "")).casefold()
                other_snippet = _clean_text(existing.get("snippet", "")).casefold()
                if len(other_snippet) < 30 or signature != existing_signature:
                    continue
                title_sim = SequenceMatcher(None, title, other_title).ratio()
                if title_sim < threshold:
                    continue
                snippet_sim = SequenceMatcher(None, snippet, other_snippet).ratio()
                if snippet_sim >= threshold:
                    target = existing
                    break
        if target is None:
            kept.append({**item, "alternate_urls": []})
            facts.append(signature)
            continue
        target["alternate_urls"].append(item["url"])
        target["sources"] = list(dict.fromkeys(target["sources"] + item["sources"]))
        target["evidence"] = target["evidence"] + item["evidence"]
        target["titles"] = _dedupe_text(target["titles"] + item["titles"])
        target["snippets"] = _dedupe_text(target["snippets"] + item["snippets"])
        target["published_dates"] = _dedupe_text(
            target["published_dates"] + item["published_dates"]
        )
        ranks = target["ranks"]
        target["ranks"] = {**ranks, **{
            source: min(rank, ranks.get(source, rank))
            for source, rank in item["ranks"].items()
        }}
        folded += 1
    for item in kept:
        if item["alternate_urls"]:
            _refresh_score(item, official_domains)
    kept.sort(key=_sort_key)
    return kept, folded


def decode_mcp_response(response: httpx.Response) -> dict:
    """解码 Streamable HTTP MCP 的 JSON 或 SSE 响应，并归类鉴权/限流错误。"""
    if response.status_code >= 400:
        raise ProviderError(
            f"HTTP {response.status_code}: {brief(response.text)}",
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
        raise ProviderError(f"MCP 响应不是 JSON: {brief(text)}") from exc
    if not isinstance(data, dict):
        raise ProviderError("MCP 响应不是对象")
    if data.get("error"):
        # JSON-RPC 层的 error 是对方 MCP 服务自己报的（协议问题、匿名额度用完等），
        # 和目标网页无关，所以搜索和抓取都按它判断限流 / 鉴权。
        # 目标网页的问题会出现在工具结果的 isError 里，由各适配器用 fetch_error 处理。
        raise business_error(str(data["error"]))
    result = data.get("result")
    if not isinstance(result, dict):
        raise ProviderError("MCP 响应缺少 result 对象")
    return result


def text_of(value) -> str:
    """把上游给的字段安全地变成文本。

    字符串去掉首尾空白；数字转成字符串；None、布尔、列表、对象一律当空。
    上游哪天把某个字段的类型改了，适配器不至于在 .strip() 上报错。
    """
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return ""
    return str(value)


def date_metadata(**values) -> tuple[tuple[str, str], ...]:
    """保留适配器明确映射的日期字段，不猜它们是发布日期。"""
    return tuple((kind, text) for kind, value in values.items() if (text := text_of(value)))


def as_dict(value) -> dict:
    """上游该给对象的地方给了别的东西时，当成空对象处理。"""
    return value if isinstance(value, dict) else {}


def error_text(value, *keys: str) -> str:
    """把「错误」字段变成一行可读文本，供状态码判断用。

    各家的错误可能是字符串，也可能是 {code, message} / {Code, Message} 这样的对象
    （智谱、腾讯云都是后者）。直接 text_of(dict) 会得到空串，
    于是「额度用完」被当成认不出的错误，key 就不会被冷却。
    """
    if not isinstance(value, dict):
        return text_of(value)
    parts = [part for part in (text_of(value.get(key)) for key in keys) if part]
    if parts:
        return " ".join(parts)
    # 没给候选键时，把对象里所有能变成文本的值都拼上
    return " ".join(part for part in (text_of(item) for item in value.values()) if part)


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


# ---------------------------------------------------------------------------
# 原生过滤 / site: 只用于缩小上游范围。本地按每条原始证据验收，再交给合并和门禁。
# ---------------------------------------------------------------------------

# 时间范围别名 -> 规范值（day / week / month / year）
_TIME_ALIASES: dict[str, str] = {
    "day": "day", "d": "day", "24h": "day", "1d": "day", "today": "day", "daily": "day", "今天": "day", "一天": "day",
    "week": "week", "w": "week", "7d": "week", "weekly": "week", "本周": "week", "一周": "week", "7天": "week",
    "month": "month", "m": "month", "30d": "month", "monthly": "month", "本月": "month", "一个月": "month", "30天": "month",
    "year": "year", "y": "year", "365d": "year", "yearly": "year", "今年": "year", "一年": "year", "12个月": "year",
}
_WINDOW_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

# 仅支持站点参数的厂商；是否支持原生时间是另一个独立的能力。
NATIVE_DOMAIN_PROVIDERS = frozenset({"tavily", "exa", "perplexity"})
MAX_DOMAINS = 20

_NATIVE_TIME = {
    "tavily": dict(zip(_WINDOW_DAYS, _WINDOW_DAYS)),
    "brave": {"day": "pd", "week": "pw", "month": "pm", "year": "py"},
    "bocha": {"day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"},
    "volcengine": {"day": 1, "week": 2, "month": 3, "year": 4},
    "firecrawl": {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"},
    "perplexity": dict(zip(_WINDOW_DAYS, _WINDOW_DAYS)),
    "searxng": dict(zip(_WINDOW_DAYS, _WINDOW_DAYS)),
}

_FILTERS: contextvars.ContextVar[tuple[str, tuple[str, ...], datetime | None]] = contextvars.ContextVar(
    "aggregate_search_filters", default=("", (), None)
)


def normalize_time_range(value: str | None) -> str:
    """把用户写的 time_range 归一成 day / week / month / year；认不出返回空串。"""
    return _TIME_ALIASES.get((value or "").strip().casefold(), "")


def normalize_domains(domains: Iterable[str] | None) -> tuple[str, ...]:
    """安全 DNS 主机名（允许 URL / 通配写法），去重并排序，至多 20 个。"""
    if isinstance(domains, str):
        domains = (domains,)
    out: set[str] = set()
    for raw in domains or ():
        if not isinstance(raw, str) or not (text := raw.strip()):
            continue
        text = text.translate(_HOST_DOTS)
        scheme, sep, rest = text.partition("://")
        if sep and scheme.lower() not in _DEFAULT_PORTS:
            continue
        rest = rest if sep else text.removeprefix("//")
        rest = rest.removeprefix("*.").removeprefix(".")
        parsed = _url_parts(f"https://{rest}")
        if parsed is not None:
            _, host = parsed
            # URL 的路径/查询/合法端口不属于站点过滤条件；只允许安全、带点的 DNS 名称。
            if "." in host:
                out.add(host)
    return tuple(sorted(out)[:MAX_DOMAINS])


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def set_filters(time_range: str | None = "", domains=None, as_of: datetime | None = None):
    """设置本次调用生效的过滤条件，返回 token（供 reset_filters 还原）。"""
    anchor = _utc(as_of) if as_of is not None else datetime.now(timezone.utc)
    return _FILTERS.set((normalize_time_range(time_range), normalize_domains(domains), anchor))


def reset_filters(token) -> None:
    _FILTERS.reset(token)


def current_filters() -> tuple[str, tuple[str, ...]]:
    """当前生效的过滤条件：('week', ('example.com',))。"""
    time_range, domains, _ = _FILTERS.get()
    return time_range, domains


def current_search_as_of() -> datetime:
    return _FILTERS.get()[2] or datetime.now(timezone.utc)


def wants_time(key: str) -> str | int:
    """当前时间范围换算成某家的原生参数值；不用过滤或不支持时返回空串。"""
    time_range = current_filters()[0]
    if not time_range:
        return ""
    if key == "exa":
        # Exa 只用作粗筛：纯日期在窗口边界的语义由本地过滤决定。
        return (current_search_as_of() - timedelta(days=_WINDOW_DAYS[time_range])).date().isoformat()
    return _NATIVE_TIME.get(key, {}).get(time_range, "")


def wants_domains() -> tuple[str, ...]:
    return current_filters()[1]


def query_with_sites(query: str) -> str:
    """给不支持站点过滤的引擎，把所有规范站点拼进查询。"""
    domains = wants_domains()
    if not domains:
        return query
    sites = " OR ".join(f"site:{domain}" for domain in domains)
    return f"{query} ({sites})"


_DATE_ONLY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ISO_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:[.,][0-9]+)?)?(?:[Zz]|[+-][0-9]{2}:?[0-9]{2})"
)
# email.utils 本身也接受模糊/过时格式；先限制词形、完整时区和偏移分钟。
_RFC_PUBLICATION = re.compile(
    r"(?:(?P<weekday>[a-z]{3}),[ \t]+)?[0-9]{1,2}[ \t]+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[ \t]+(?:19|[2-9][0-9])[0-9]{2}[ \t]+"
    r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?[ \t]+(?:GMT|UTC|(?!-0000)[+-](?:[01][0-9]|2[0-3])[0-5][0-9])",
    re.I | re.ASCII,
)
_ZH_DATE = re.compile(r"([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日")


def _parse_publication_date(raw: str) -> date | datetime | None:
    value = text_of(raw)
    try:
        if _DATE_ONLY.fullmatch(value):
            return date.fromisoformat(value)
        if _ISO_DATETIME.fullmatch(value):
            if value[-1] in "Zz":  # Python 3.10 的 fromisoformat 不接受 Z
                value = value[:-1] + "+00:00"
            value = re.sub(r"[.,]([0-9]+)", lambda m: "." + m[1][:6].ljust(6, "0"), value)
            value = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", value)
            return datetime.fromisoformat(value).astimezone(timezone.utc)
        if zh := _ZH_DATE.fullmatch(value):
            return date(*map(int, zh.groups()))
        if rfc := _RFC_PUBLICATION.fullmatch(value):
            from email.utils import parsedate_to_datetime

            stamp = parsedate_to_datetime(value)
            weekday = rfc["weekday"]
            if weekday and weekday.lower() != ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[stamp.weekday()]:
                return None
            return stamp.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        pass
    return None


def filter_results(
    source_results: Iterable[SearchResult],
    time_window: str | None,
    domains: Iterable[str] | None,
    as_of: datetime,
) -> list[SearchResult]:
    """逐条按站点及时间验收，保序且不改写证据；未知、未来时间不通过。

    明确发布日期之外的页龄/更新时间不用来验证。带时区时间戳按 UTC 滚动 N 天，
    纯日期按 UTC 日历日检查（包括边界日）。
    """
    has_domains = bool(domains)
    sites = normalize_domains(domains)
    days = _WINDOW_DAYS.get(normalize_time_range(time_window))
    if (has_domains and not sites) or (time_window and not days):
        return []
    end = _utc(as_of)
    start = end - timedelta(days=days or 0)
    out: list[SearchResult] = []
    for result in source_results:
        parsed_url = _url_parts(text_of(result.url))
        if parsed_url is None or (sites and not _host_in_domains(parsed_url[1], sites)):
            continue
        if days:
            timestamp = _parse_publication_date(result.published)
            if isinstance(timestamp, datetime):
                if not start <= timestamp <= end:
                    continue
            elif timestamp is None or not start.date() <= timestamp <= end.date():
                continue
        out.append(result)
    return out
