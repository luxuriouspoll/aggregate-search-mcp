"""provider 公共基础设施：统一结果模型、HTTP 封装、URL 归一化与合并去重。"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..config import API_KEYS, FETCH_MAX_CHARS, OFFICIAL_SOURCE_DOMAINS, SEARCH_TIMEOUT


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
    published: str = ""


@dataclass
class FetchResult:
    provider: str
    url: str
    title: str = ""
    content: str = ""
    auth: str = ""     # 由调度层填写：anon（匿名）/ key（用了账号 key）


def redact(text: str) -> str:
    """把已配置的 key 和网址里的账号口令从文本里抹掉。

    长的 key 先替换：一个 key 恰好是另一个的前缀时，不会留下后半截。
    """
    if not text:
        return text
    out = text
    keys = {key for keys in API_KEYS.values() for key in keys if key and len(key) >= 8}
    for key in sorted(keys, key=len, reverse=True):
        out = out.replace(key, "***")
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
            url, json=payload, headers=merged, timeout=timeout or SEARCH_TIMEOUT
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
        resp = await client.get(url, headers=merged, timeout=timeout or SEARCH_TIMEOUT)
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


def _is_official(url: str) -> bool:
    """这个网址是不是官方一手来源（政策站点、官方文档等）。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_SOURCE_DOMAINS)


# 官方一手来源的等效来源数。取 1.2 的效果：
#   官网单源(1+1.2=2.2) > 两家低质转载(2.0)，但 < 三家共识(3.0)。
# 也就是说「官方原文」值得优先读，但多家独立来源互相印证仍然更强。
OFFICIAL_SOURCE_WEIGHT = 1.2


def _weight(item: dict) -> float:
    """排序用的等效来源数：来源数 + 官方来源加成。"""
    return item["source_count"] + (OFFICIAL_SOURCE_WEIGHT if item.get("is_official") else 0.0)


def _sort_key(item: dict) -> tuple:
    # 第一优先是等效来源数（含官方加成），其次 RRF 分值，再看最佳名次和网址
    return (-_weight(item), -item["score"], item["best_rank"], item["url"])


def _refresh_score(item: dict) -> None:
    item["source_count"] = len(item["sources"])
    item["is_official"] = _is_official(item["url"])
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
# 搜索过滤条件（时间范围 / 限制站点）
#
# 用 contextvar 传而不是改 19 个适配器的函数签名：绝大多数适配器用不到这些条件，
# 支持的 8 家自己读，其余的在查询里拼 site: 并在汇总阶段后过滤。
# ---------------------------------------------------------------------------

import contextvars

# 时间范围别名 -> 规范值（day / week / month / year）
_TIME_ALIASES: dict[str, str] = {
    "day": "day", "d": "day", "24h": "day", "1d": "day", "today": "day", "daily": "day", "今天": "day", "一天": "day",
    "week": "week", "w": "week", "7d": "week", "weekly": "week", "本周": "week", "一周": "week", "7天": "week",
    "month": "month", "m": "month", "30d": "month", "monthly": "month", "本月": "month", "一个月": "month", "30天": "month",
    "year": "year", "y": "year", "365d": "year", "yearly": "year", "今年": "year", "一年": "year", "12个月": "year",
}

# 规范值 -> 各家的原生参数
_TAVILY_TIME = {"day": "day", "week": "week", "month": "month", "year": "year"}
_BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
_BOCHA_FRESHNESS = {"day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"}
_VOLC_TIME = {"day": 1, "week": 2, "month": 3, "year": 4}
_FIRECRAWL_TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
_PERPLEXITY_RECENCY = {"day": "day", "week": "week", "month": "month", "year": "year"}
_SEARXNG_TIME = {"day": "day", "week": "week", "month": "month", "year": "year"}

# 当前这次搜索的过滤条件：(规范时间范围或空串, 站点元组)
_FILTERS: contextvars.ContextVar[tuple[str, tuple[str, ...]]] = contextvars.ContextVar(
    "aggregate_search_filters", default=("", ())
)


def normalize_time_range(value: str | None) -> str:
    """把用户写的 time_range 归一成 day / week / month / year；认不出返回空串（不过滤）。"""
    return _TIME_ALIASES.get((value or "").strip().casefold(), "")


def normalize_domains(domains) -> tuple[str, ...]:
    """规范化站点列表：只取主机名，去掉协议、路径、通配前缀，小写去重。"""
    out: list[str] = []
    for raw in domains or []:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        text = text.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
        text = text.removeprefix("*.").removeprefix(".")
        if text and "." in text and text not in out:
            out.append(text)
    return tuple(out)


def set_filters(time_range: str | None = "", domains=None):
    """设置本次调用生效的过滤条件，返回 token（供 reset_filters 还原）。"""
    return _FILTERS.set((normalize_time_range(time_range), normalize_domains(domains)))


def reset_filters(token) -> None:
    try:
        _FILTERS.reset(token)
    except ValueError:
        # 跨事件循环重置时 token 会失效，退回默认值即可，不影响结果
        _FILTERS.set(("", ()))


def current_filters() -> tuple[str, tuple[str, ...]]:
    """当前生效的过滤条件：('week', ('example.com',))。"""
    return _FILTERS.get()


def wants_time(key: str) -> str:
    """当前时间范围换算成某家的原生参数值；不用过滤或不支持时返回空串/0。"""
    time_range = current_filters()[0]
    if not time_range:
        return ""
    table = {
        "tavily": _TAVILY_TIME, "brave": _BRAVE_FRESHNESS, "bocha": _BOCHA_FRESHNESS,
        "searxng": _SEARXNG_TIME, "firecrawl": _FIRECRAWL_TBS, "perplexity": _PERPLEXITY_RECENCY,
    }.get(key)
    if table:
        return table.get(time_range, "")
    if key == "volcengine":
        return _VOLC_TIME.get(time_range, 0)  # type: ignore[return-value]
    if key == "exa":
        # Exa 要的是绝对日期
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[time_range]
        import datetime as _dt
        return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    return ""


def wants_domains() -> tuple[str, ...]:
    return current_filters()[1]


def query_with_sites(query: str) -> str:
    """给不支持站点过滤的引擎，把站点条件拼进查询（site:a.com OR site:b.com）。"""
    domains = wants_domains()
    if not domains:
        return query
    sites = " OR ".join(f"site:{domain}" for domain in domains[:5])
    return f"{query} ({sites})"


# 支持原生时间 / 站点过滤的厂商；其余厂商把站点条件拼进查询（site:），时间条件只在汇总时说明
NATIVE_FILTER_PROVIDERS: frozenset[str] = frozenset({
    "tavily", "exa", "brave", "bocha", "volcengine", "firecrawl", "perplexity", "searxng",
})
