"""provider 注册表与并发调度（多 key 轮询）。

同一家的多个 key 按顺序轮询：一个 key 报 401/402/403/429 就换下一个，
直到这家所有 key 都试过或都在冷却期，才判定这家失败。
错误与限额状态由 keypool 记录并落盘，重启后仍生效。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

import httpx

from config import (
    API_KEYS,
    ENABLE_KEYLESS,
    SEARCH_PROXY,
    SEARXNG_BASE_URL,
    SEARCH_PROVIDERS_LIMIT,
)
from keypool import KeyPool
from providers import (
    brave,
    duckduckgo,
    exa,
    firecrawl,
    jina,
    ollama,
    perplexity,
    querit,
    searxng,
    tavily,
    volcengine,
)
from providers.base import FetchResult, ProviderError, SearchResult, truncate

# name -> (search_fn | None, fetch_fn | None)
REGISTRY: dict[str, tuple] = {
    "tavily": (tavily.search, tavily.fetch),
    "querit": (querit.search, None),
    "firecrawl": (firecrawl.search, firecrawl.fetch),
    "ollama": (ollama.search, ollama.fetch),
    "brave": (brave.search, None),
    "exa": (exa.search, exa.fetch),
    "jina": (jina.search, jina.fetch),
    "perplexity": (perplexity.search, None),
    "volcengine": (volcengine.search, None),
    "searxng": (searxng.search, None),
    "duckduckgo": (duckduckgo.search, None),
}

# 免 key 的家：
# - duckduckgo：天生免费（国内直连不稳，建议配 SEARCH_PROXY）
# - firecrawl：官方支持无 key 调用（每日按 IP 限额），配了 key 就自动改用 key
# - searxng：靠实例地址启用
# 这类源默认不自动参与聚合，需指定 providers 或设 ENABLE_KEYLESS=true。
KEYLESS_KNOWN: set[str] = {"duckduckgo", "firecrawl"} | (
    {"searxng"} if SEARXNG_BASE_URL else set()
)
KEYLESS_ENABLED: set[str] = set(KEYLESS_KNOWN) if ENABLE_KEYLESS else set()

# 支持抓取的提供商
FETCH_PROVIDERS: list[str] = [name for name, (_, fetch_fn) in REGISTRY.items() if fetch_fn]


def redact(text: str) -> str:
    """脱敏：把已配置的 key 和代理凭据从文本里抹掉。

    上游错误体有时会回显提交的 token，这些文本会进状态文件、也会回给客户端，
    所以统一过一遍。
    """
    if not text:
        return text
    out = text
    for keys in API_KEYS.values():
        for key in keys:
            if key and len(key) >= 8:
                out = out.replace(key, "***")
    if SEARCH_PROXY and "@" in SEARCH_PROXY:
        # 代理形如 http://user:pass@host:port，抹掉 user:pass
        out = re.sub(r"(//)[^/@\s]+@", r"\1***@", out)
    return out

# 每家的 key 池（进程内单例，冷却状态从 .key-state.json 读）
POOLS: dict[str, KeyPool] = {
    name: KeyPool(name, list(keys)) for name, keys in API_KEYS.items() if name in REGISTRY
}


def _fp(key: str) -> str:
    """与 keypool 用同一套指纹，否则 attempted 过滤会失效、退化成重复试同一 key。"""
    from keypool import _fingerprint

    return _fingerprint(key)


def provider_enabled(name: str, explicit: bool = False) -> bool:
    """这家是否可用。

    - 有 key：可用
    - 免 key 家：explicit=True（调用方在 providers 里点名）时一律可用；
      否则只认 KEYLESS_ENABLED / 自托管实例已配置的
    - searxng：配了实例地址就算配置好了
    """
    pool = POOLS.get(name)
    if pool and pool.keys:
        return True
    if name == "searxng" and SEARXNG_BASE_URL:
        return True
    if explicit and name in KEYLESS_KNOWN:
        return True
    return name in KEYLESS_ENABLED


def _client() -> httpx.AsyncClient:
    """统一构造 httpx 客户端；配了 SEARCH_PROXY 就走代理。"""
    kwargs: dict = {"follow_redirects": True}
    if SEARCH_PROXY:
        kwargs["proxy"] = SEARCH_PROXY
    return httpx.AsyncClient(**kwargs)


def enabled_search_providers() -> list[str]:
    """默认启用的搜索商：可用的全部，再叠加 SEARCH_PROVIDERS 白名单过滤。"""
    names = [name for name in REGISTRY if provider_enabled(name)]
    if SEARCH_PROVIDERS_LIMIT:
        names = [name for name in names if name in SEARCH_PROVIDERS_LIMIT]
    return names


async def _with_keys(
    name: str,
    keys: list[str],
    call,
):
    """通用 key 轮询执行器：call(key) 抛 ProviderError 时按状态换下一个 key。"""
    attempted: set[str] = set()
    last_error: ProviderError | None = None
    while True:
        key = POOLS[name].next_key(attempted)
        if key is None:
            break
        attempted.add(_fp(key))
        try:
            value = await call(key)
        except ProviderError as exc:
            last_error = exc
            POOLS[name].record_failure(key, exc.status, redact(str(exc)), exc.retry_after)
            if exc.retryable_with_other_key:
                continue  # 换下一个 key 再试
            raise
        else:
            POOLS[name].record_success(key)
            return value

    # 所有 key 都不行
    if last_error is not None:
        raise ProviderError(
            f"全部 {len(keys)} 个 key 均失败（最后错误: {last_error}）",
            status=last_error.status,
        )
    reset = POOLS[name].earliest_reset()
    if reset:
        left = max(0, int(reset - time.time()))
        raise ProviderError(f"该家所有 key 都在冷却中，最早 {left} 秒后恢复", status=429)
    raise ProviderError("该家没有可用的 key")


async def search_with_pool(
    name: str, client: httpx.AsyncClient, query: str, max_results: int
) -> list[SearchResult]:
    """单家搜索：按 key 池轮询，遇到可换 key 的错误就试下一个。"""
    search_fn = REGISTRY[name][0]
    if search_fn is None:
        raise ProviderError(f"{name} 不支持搜索")

    pool = POOLS.get(name)
    keys = list(pool.keys) if pool else []
    if not keys:
        # 免 key 家：直接调，key 传空串
        return await search_fn(client, "", query, max_results)

    return await _with_keys(
        name, keys, lambda key: search_fn(client, key, query, max_results)
    )


async def run_search(
    names: list[str], query: str, max_results: int
) -> tuple[dict[str, list[SearchResult]], dict[str, str]]:
    """并发调用各家搜索。返回 (成功结果 {name: [...]}, 失败原因 {name: msg})。

    单家失败（所有 key 都不行、网络、解析）只记入 errors，不影响其他家。
    """
    results: dict[str, list[SearchResult]] = {}
    errors: dict[str, str] = {}
    if not names:
        return results, errors

    async def call(name: str, client: httpx.AsyncClient):
        return await search_with_pool(name, client, query, max_results)

    async with _client() as client:
        outcomes = await asyncio.gather(
            *(call(name, client) for name in names), return_exceptions=True
        )

    for name, outcome in zip(names, outcomes):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome  # 客户端取消要向上传递，不能被当成"这家失败"
        if isinstance(outcome, Exception):
            errors[name] = redact(f"{type(outcome).__name__}: {outcome}")
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            results[name] = outcome
    return results, errors


async def run_fetch(name: str, url: str) -> FetchResult:
    """用指定提供商抓取网页正文（统一截断到 FETCH_MAX_CHARS），同样走 key 轮询。"""
    fetch_fn = REGISTRY.get(name, (None, None))[1]
    if fetch_fn is None:
        raise ProviderError(f"{name} 不是可用的抓取后端，可选: {', '.join(FETCH_PROVIDERS)}")

    pool = POOLS.get(name)
    keys = list(pool.keys) if pool else []
    if not keys and name not in KEYLESS_ENABLED:
        raise ProviderError(f"{name} 未配置 API key")

    async with _client() as client:
        if not keys:
            result = await fetch_fn(client, "", url)
        else:
            result = await _with_keys(name, keys, lambda key: fetch_fn(client, key, url))
    result.content = truncate(result.content)
    return result


def pool_status() -> dict[str, str]:
    """各家 key 池状态摘要，供 server 输出与自检使用。"""
    return {name: POOLS[name].status_line() for name in REGISTRY if name in POOLS}
