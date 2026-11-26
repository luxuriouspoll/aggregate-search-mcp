"""provider 注册表与并发调度（多 key 轮询）。

同一家的多个 key 跨请求轮转；一个 key 报鉴权、额度或限流错误就换下一个。
错误与限额状态由 keypool 记录并落盘，重启后仍生效。

每次调用都产出一条 CallRecord，如实记下这次有没有真的发出请求、是匿名还是用了账号 key。
成本说明和本地使用记录都以它为准，而不是按搜索模式去推断。
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import threading
import time
from dataclasses import dataclass, field

import httpx

from .. import usage
from ..config import (
    API_KEYS,
    ENABLE_KEYLESS,
    PROXY_PROVIDERS,
    RATE_COOLDOWN,
    SEARCH_PROXY,
    SEARCH_PROXY_ALL,
    SEARXNG_BASE_URL,
    SEARCH_PROVIDERS_LIMIT,
    USE_SYSTEM_PROXY,
)
from ..keypool import KeyPool, _fingerprint
from . import (
    anysearch,
    baidu,
    bocha,
    brave,
    duckduckgo,
    exa,
    firecrawl,
    jina,
    keenable,
    ollama,
    parallel,
    perplexity,
    querit,
    searxng,
    tavily,
    volcengine,
    youcom,
    yuanbao,
    zhipu,
)
from .base import (
    NETWORK_ERROR,
    FetchResult,
    ProviderError,
    SearchResult,
    redact,
    scrub_urls,
    text_of,
    truncate,
)

# httpx 默认把每个请求的 URL 记进 INFO 日志；部分厂商的搜索词就在 URL 里，
# 没必要落进 MCP 客户端的日志，只保留警告和错误。
logging.getLogger("httpx").setLevel(logging.WARNING)

# name -> (search_fn | None, fetch_fn | None)
REGISTRY: dict[str, tuple] = {
    "tavily": (tavily.search, tavily.fetch),
    "baidu": (baidu.search, None),
    "bocha": (bocha.search, None),
    "yuanbao": (yuanbao.search, None),
    "zhipu": (zhipu.search, None),
    "anysearch": (anysearch.search, None),
    "keenable": (keenable.search, keenable.fetch),
    "youcom": (youcom.search, None),
    "querit": (querit.search, None),
    "firecrawl": (firecrawl.search, firecrawl.fetch),
    "ollama": (ollama.search, ollama.fetch),
    "brave": (brave.search, None),
    "exa": (exa.search, exa.fetch),
    "jina": (jina.search, jina.fetch),
    "parallel": (parallel.search, parallel.fetch),
    "perplexity": (perplexity.search, None),
    "volcengine": (volcengine.search, None),
    "searxng": (searxng.search, None),
    "duckduckgo": (duckduckgo.search, None),
}

# 不带 key 也能调用的源（KEYLESS_KNOWN），以及其中默认就参与搜索的（KEYLESS_ENABLED）：
# - parallel / keenable / anysearch：匿名免费，国内直连实测可用，默认启用
# - duckduckgo / firecrawl 匿名 / youcom：受 ENABLE_KEYLESS 开关控制，避免超时或重复计费
# - searxng：填实例地址即启用
KEYLESS_KNOWN: set[str] = {"parallel", "anysearch", "keenable", "youcom", "duckduckgo", "firecrawl"} | (
    {"searxng"} if SEARXNG_BASE_URL else set()
)
KEYLESS_ENABLED: set[str] = {"parallel", "anysearch", "keenable"} | (
    set(KEYLESS_KNOWN) if ENABLE_KEYLESS else set()
)
if SEARXNG_BASE_URL:
    KEYLESS_ENABLED.add("searxng")

FETCH_PROVIDERS: list[str] = [name for name, (_, fetch_fn) in REGISTRY.items() if fetch_fn]
POOLS: dict[str, KeyPool] = {
    name: KeyPool(name, list(keys)) for name, keys in API_KEYS.items() if name in REGISTRY
}


def has_keys(name: str) -> bool:
    pool = POOLS.get(name)
    return bool(pool and pool.keys)


def can_anonymous(name: str) -> bool:
    """这家不带 key 能不能调用。"""
    return name in KEYLESS_KNOWN


def auth_for(name: str, anonymous: bool = False) -> str:
    """这次调用走哪个通道：'key'（带账号 key）或 'anon'（匿名）。"""
    return "key" if has_keys(name) and not anonymous else "anon"


def provider_enabled(name: str, explicit: bool = False) -> bool:
    if has_keys(name):
        return True
    if name == "searxng" and SEARXNG_BASE_URL:
        return True
    if explicit and name in KEYLESS_KNOWN:
        return True
    return name in KEYLESS_ENABLED


_SSL_LOCK = threading.Lock()
_SSL_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """CA 证书只加载一次，全进程复用。

    httpx 每新建一个客户端都会重新加载一遍证书，实测一次约 0.8 秒且阻塞事件循环：
    并发查 3 家就白等 2 秒多，all 模式查 10 家要白等 8 秒。
    用 httpx 自己的函数创建，信任设置（含 SSL_CERT_FILE）与默认行为完全一致。
    """
    global _SSL_CONTEXT
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = httpx.create_ssl_context()
        return _SSL_CONTEXT


def _same_site(origin: str, target: str) -> bool:
    """两个主机名是否属于同一个站点：完全相同，或一个是另一个的子域名。

    放行 ollama.com → www.ollama.com 这类常见的正常跳转；
    较短的那个必须本身带点，免得把 "ai"、"com" 这种后缀当成父域名。
    """
    origin, target = origin.lower().rstrip("."), target.lower().rstrip(".")
    if not origin or not target:
        return False
    if origin == target:
        return True
    short, long = sorted((origin, target), key=len)
    return "." in short and long.endswith("." + short)


async def _keep_keys_on_host(response: httpx.Response) -> None:
    """带着 key 的请求只允许在同一个站点内跳转。

    httpx 跨主机跳转时只会去掉 Authorization，X-API-Key 这类自定义请求头会原样带到新主机。
    搜索接口没有正当理由把带 key 的请求引到别的站点，遇到就中止。不带 key 的请求不受影响。
    """
    if not response.has_redirect_location:
        return
    request = response.request
    carried = " ".join(request.headers.values())
    if redact(carried) == carried:
        return  # 请求头里没有任何已配置的 key
    try:
        destination = request.url.join(response.headers.get("location", ""))
        target, scheme = destination.host, destination.scheme
    except Exception:
        target, scheme = "", ""
    if not _same_site(request.url.host, target):
        raise ProviderError(
            f"接口要求跳转到另一个站点（{request.url.host} → {target or '未知'}），为保护 key 已中止"
        )
    if request.url.scheme == "https" and scheme != "https":
        # 同一站点但从 https 跳到 http：自定义请求头里的 key 会被明文发出去
        raise ProviderError(
            f"接口要求从 https 跳转到不加密的地址（{request.url.host}），为保护 key 已中止"
        )


def _client(name: str) -> httpx.AsyncClient:
    """按 provider 决定走哪条网络：指定的代理、系统设置，或强制直连。"""
    kwargs: dict = {
        "follow_redirects": True,
        "verify": ssl_context(),
        "event_hooks": {"response": [_keep_keys_on_host]},
    }
    if SEARCH_PROXY and (SEARCH_PROXY_ALL or name in PROXY_PROVIDERS):
        kwargs["proxy"] = SEARCH_PROXY
    elif not USE_SYSTEM_PROXY:
        kwargs["trust_env"] = False  # 不读系统代理和 HTTP(S)_PROXY，强制直连
    return httpx.AsyncClient(**kwargs)


def enabled_search_providers() -> list[str]:
    names = [name for name in REGISTRY if REGISTRY[name][0] and provider_enabled(name)]
    if SEARCH_PROVIDERS_LIMIT:
        names = [name for name in names if name in SEARCH_PROVIDERS_LIMIT]
    return names


@dataclass
class CallRecord:
    """一次厂商调用的如实记录。"""

    provider: str
    auth: str = "anon"      # 走的通道。anon：匿名；key：账号 key
    key_index: int = 0      # 成功的是这家的第几个 key（1 起）；匿名或失败为 0
    keys_tried: int = 0     # 实际发出了几次带 key 的请求（每个 key 最多一次）
    anon_sent: bool = False  # 是否真的发出过匿名请求
    ok: bool = False
    count: int = 0
    ms: int = 0
    error: str = ""
    fallback: bool = False  # 本想匿名调用，匿名通道被限流后才改用 key
    results: list[SearchResult] = field(default_factory=list, repr=False)

    @property
    def used_key(self) -> bool:
        """这次是否真的把账号 key 发了出去。成本说明和使用记录只认这个。"""
        return self.keys_tried > 0

    @property
    def sent(self) -> bool:
        """有没有发出过任何请求。False = key 冷却 / 到量 / 匿名通道冷却中，根本没发。"""
        return self.anon_sent or self.keys_tried > 0

    @property
    def network_failed(self) -> bool:
        return not self.ok and self.error.startswith(NETWORK_ERROR)

    @property
    def label(self) -> str:
        if self.auth != "key":
            return f"{self.provider}（匿名）"
        return f"{self.provider}（key#{self.key_index}）" if self.key_index else f"{self.provider}（key）"

    def as_log(self) -> dict:
        entry = {"p": self.provider, "auth": self.auth, "ok": self.ok, "n": self.count, "ms": self.ms}
        if self.auth == "key":
            entry["keys"] = self.keys_tried  # 实际发出了几次带 key 的请求；0 = key 没用上
        if self.key_index:
            entry["k"] = self.key_index
        if not self.sent:
            entry["sent"] = False
        if self.fallback:
            entry["fallback"] = True
        if self.error:
            entry["err"] = scrub_urls(self.error)[:160]
        return entry


# 匿名通道被限流后的冷却：厂商 -> 恢复时间。冷却期内不再白发匿名请求
# （实测一次被拒要白等 2.5 秒）。只放内存，进程重启即清空。
_ANON_COOLDOWN: dict[str, float] = {}


def anon_cooling(name: str) -> int:
    """匿名通道还要冷却多少秒；0 表示可用。"""
    left = _ANON_COOLDOWN.get(name, 0.0) - time.time()
    if left <= 0:
        _ANON_COOLDOWN.pop(name, None)
        return 0
    return int(left) + 1


def _note_anon_failure(name: str, exc: ProviderError) -> None:
    """匿名通道被拒（限流 / 额度 / 要求登录）就歇一会儿，别每次搜索都白发一次。"""
    if exc.status in (401, 402, 403, 429):
        wait = exc.retry_after if exc.retry_after is not None else RATE_COOLDOWN
        if wait > 0:
            _ANON_COOLDOWN[name] = time.time() + wait


async def _with_keys(name: str, call, record: CallRecord | None = None):
    """通用 key 轮询执行器，返回 (结果, 成功的那个 key)。

    保证异常时也会释放 in-flight 占用。每取到一个 key 就把 record.keys_tried 加一，
    所以哪怕中途出错或被总时限取消，「到底发出了几次带 key 的请求」也不会记错。
    一个 key 都没拿到（全在冷却 / 到量）时抛出的错误带 sent=False。
    """
    pool = POOLS[name]
    attempted: set[str] = set()
    last_error: ProviderError | None = None
    while True:
        key = pool.next_key(attempted)
        if key is None:
            break
        attempted.add(_fingerprint(key))
        if record is not None:
            record.keys_tried = len(attempted)
        try:
            value = await call(key)
        except ProviderError as exc:
            last_error = exc
            pool.record_failure(key, exc.status, redact(str(exc)), exc.retry_after, exc.soft)
            if exc.retryable_with_other_key:
                continue
            raise
        else:
            pool.record_success(key)
            return value, key
        finally:
            pool.release(key)

    if last_error is not None:
        raise ProviderError(
            f"已试 {len(attempted)} 个 key 均失败（最后错误: {redact(str(last_error))}）",
            status=last_error.status,
            retry_after=last_error.retry_after,
        )
    # 走到这里说明一个 key 都没拿到，请求根本没发出去
    reason = pool.capped_reason()
    if reason and not pool.usable():
        raise ProviderError(
            f"该家的 key 已到本地用量上限（{reason}），到期自动恢复；"
            "可在 .env 里用 DAILY_LIMITS / MONTHLY_LIMITS 调整",
            status=429,
            sent=False,
        )
    reset = pool.earliest_reset()
    if reset:
        left = max(0, int(reset - time.time()))
        raise ProviderError(
            f"该家所有 key 都在冷却中，最早 {left} 秒后恢复", status=429, sent=False
        )
    raise ProviderError("该家没有可用的 key", sent=False)


def _tidy(results) -> list[SearchResult]:
    """适配器交上来的结果统一过一遍：字段一律变成文本，丢掉没有合法网址的条目。

    合并去重在所有厂商的结果上一起做，没有按厂商隔离——任何一家给出类型不对的字段，
    都会让整次搜索报错。所以在这里兜底，不指望每个适配器都写得滴水不漏。
    """
    tidy: list[SearchResult] = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, SearchResult):
            continue
        url = text_of(item.url)
        if not url.lower().startswith(("http://", "https://")):
            continue
        item.url = url
        item.title = text_of(item.title)
        item.snippet = text_of(item.snippet)
        item.published = text_of(item.published)
        if not isinstance(item.rank, int) or isinstance(item.rank, bool):
            item.rank = len(tidy)
        tidy.append(item)
    return tidy


def _describe(exc: Exception) -> str:
    text = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
    return redact(text)


async def search_one(
    name: str,
    query: str,
    max_results: int,
    anonymous: bool = False,
    key_fallback: bool = False,
    record: CallRecord | None = None,
) -> CallRecord:
    """调用一家搜索。永不抛业务异常：成败、耗时、用没用 key 都写在返回的记录里。

    anonymous=True     强制不带 key，即使这家配了 key。
    key_fallback=True  匿名通道被限流 / 拒绝、而这家又配了可用的 key 时，改用 key 再试一次。
    record             调用方预先建好的记录：即使本次调用被总时限取消，已经发生的事也留在里面。
    """
    if record is None:
        record = CallRecord(provider=name, auth=auth_for(name, anonymous))
    started = time.monotonic()
    try:
        search_fn = REGISTRY[name][0] if name in REGISTRY else None
        if search_fn is None:
            raise ProviderError(f"{name} 不支持搜索", sent=False)
        if record.auth == "anon" and not can_anonymous(name):
            raise ProviderError(
                f"{name} 不支持匿名调用" if anonymous else f"{name} 未配置 API key",
                sent=False,
            )
        can_fall_back = key_fallback and has_keys(name)
        async with _client(name) as client:
            if record.auth == "anon":
                cooling = anon_cooling(name)
                if cooling and not (can_fall_back and POOLS[name].usable()):
                    raise ProviderError(
                        f"匿名通道刚被限流，约 {cooling} 秒后再试", status=429, sent=False
                    )
                if cooling:
                    record.auth, record.fallback = "key", True  # 冷却中，直接用 key，不白发匿名请求
                else:
                    usage.count_call(name, usage.ANON)
                    record.anon_sent = True
                    try:
                        record.results = await search_fn(client, "", query, max_results)
                    except ProviderError as exc:
                        _note_anon_failure(name, exc)
                        if not (can_fall_back and exc.retryable_with_other_key and POOLS[name].usable()):
                            raise
                        record.auth, record.fallback = "key", True
            if record.auth == "key":
                record.results, used_key = await _with_keys(
                    name, lambda key: search_fn(client, key, query, max_results), record
                )
                record.key_index = POOLS[name].index_of(used_key)
        record.results = _tidy(record.results)
        record.ok = True
        record.count = len(record.results)
    except Exception as exc:  # CancelledError 是 BaseException，不会被这里吞掉
        record.error = _describe(exc)
    finally:
        record.ms = int((time.monotonic() - started) * 1000)
    return record


async def run_search(
    names: list[str],
    query: str,
    max_results: int,
    anonymous=(),
    key_fallback=(),
    deadline: float | None = None,
):
    """并发查询多家，返回 (结果, 错误, 调用记录)。

    anonymous     要强制匿名调用的厂商名集合；传 True 表示全部强制匿名。
    key_fallback  其中哪些厂商在匿名通道被限流时允许改用 key。
    deadline      time.monotonic() 时刻。到点还没返回的厂商直接放弃，带着已经拿到的结果
                  往下走——不让一两家慢的把整次搜索拖到客户端超时。
    """
    results: dict[str, list[SearchResult]] = {}
    errors: dict[str, str] = {}
    if not names:
        return results, errors, []
    force_all = anonymous is True
    started = time.monotonic()
    records = [
        CallRecord(provider=name, auth=auth_for(name, force_all or name in anonymous))
        for name in names
    ]
    tasks = [
        asyncio.ensure_future(search_one(
            name, query, max_results,
            anonymous=force_all or name in anonymous,
            key_fallback=name in key_fallback,
            record=record,
        ))
        for name, record in zip(names, records)
    ]
    timeout = None if deadline is None else max(0.1, deadline - time.monotonic())
    try:
        _, pending = await asyncio.wait(tasks, timeout=timeout)
    except BaseException:
        for task in tasks:  # 整次搜索被取消时，别把子任务留在后台继续花额度
            task.cancel()
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for record, task in zip(records, tasks):
        if task in pending or task.cancelled():
            record.ok = False
            record.error = f"{NETWORK_ERROR}: 等待超过总时限，已放弃"
            record.ms = int((time.monotonic() - started) * 1000)
        elif task.exception() is not None:  # search_one 自己会兜住异常，这里只是保险
            record.ok = False
            record.error = redact(f"{type(task.exception()).__name__}: {task.exception()}")
    for record in records:
        if record.ok:
            results[record.provider] = record.results
        else:
            errors[record.provider] = record.error
    return results, errors, records


async def run_fetch(name: str, url: str, anonymous: bool = False) -> FetchResult:
    """用一个后端抓取。失败抛 ProviderError，其 sent=False 表示请求根本没发出去。"""
    fetch_fn = REGISTRY.get(name, (None, None))[1]
    if fetch_fn is None:
        raise ProviderError(
            f"{name} 不是可用的抓取后端，可选: {', '.join(FETCH_PROVIDERS)}", sent=False
        )
    auth = auth_for(name, anonymous)
    if auth == "anon" and not can_anonymous(name):
        raise ProviderError(
            f"{name} 不支持匿名抓取" if anonymous else f"{name} 未配置 API key 或不可用",
            sent=False,
        )
    async with _client(name) as client:
        if auth == "anon":
            cooling = anon_cooling(name)
            if cooling:
                raise ProviderError(
                    f"匿名通道刚被限流，约 {cooling} 秒后再试", status=429, sent=False
                )
            usage.count_call(name, usage.ANON)
            try:
                result = await fetch_fn(client, "", url)
            except ProviderError as exc:
                # 抓取工具自己报的错不带状态码（见 base.fetch_error），不会触发冷却；
                # 只有 HTTP 层明确的 401 / 402 / 403 / 429 才会让匿名通道歇一会儿
                _note_anon_failure(name, exc)
                raise
        else:
            result, _ = await _with_keys(name, lambda key: fetch_fn(client, key, url))
    if not isinstance(result, FetchResult):
        raise ProviderError(f"{name} 返回的不是抓取结果")
    result.title = text_of(result.title)
    result.content = text_of(result.content)
    if not result.content:
        # 「成功但什么都没抓到」对调用方没有用，按失败处理，好让上层换下一个后端
        raise ProviderError(f"{name} 抓取成功但正文为空")
    result.auth = auth
    result.content = truncate(result.content)
    return result


def pool_status() -> dict[str, str]:
    return {
        name: POOLS[name].status_line()
        for name in REGISTRY
        if name in POOLS and POOLS[name].keys
    }
