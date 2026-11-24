"""provider 注册表与并发调度（多 key 轮询）。

同一家的多个 key 跨请求轮转；一个 key 报鉴权、额度或限流错误就换下一个。
错误与限额状态由 keypool 记录并落盘，重启后仍生效。
"""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from config import (
    API_KEYS,
    ENABLE_KEYLESS,
    PROXY_PROVIDERS,
    SEARCH_PROXY,
    SEARCH_PROXY_ALL,
    SEARXNG_BASE_URL,
    SEARCH_PROVIDERS_LIMIT,
)
from keypool import KeyPool, _fingerprint
from providers import (
    brave,
    duckduckgo,
    exa,
    firecrawl,
    jina,
    linkup,
    ollama,
    parallel,
    perplexity,
    querit,
    searxng,
    serpapi,
    serper,
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
    "parallel": (parallel.search, parallel.fetch),
    "perplexity": (perplexity.search, None),
    "volcengine": (volcengine.search, None),
    "serper": (serper.search, None),
    "serpapi": (serpapi.search, None),
    "linkup": (linkup.search, None),
    "searxng": (searxng.search, None),
    "duckduckgo": (duckduckgo.search, None),
}

# 免 key 源：
# - parallel：官方匿名 MCP，免费且国内直连实测可用，默认启用
# - duckduckgo / firecrawl 匿名：受 ENABLE_KEYLESS 开关控制，避免超时或重复计费
# - searxng：填实例地址即启用
KEYLESS_KNOWN: set[str] = {"parallel", "duckduckgo", "firecrawl"} | (
    {"searxng"} if SEARXNG_BASE_URL else set()
)
KEYLESS_ENABLED: set[str] = {"parallel"} | (
    set(KEYLESS_KNOWN) if ENABLE_KEYLESS else set()
)
if SEARXNG_BASE_URL:
    KEYLESS_ENABLED.add("searxng")

FETCH_PROVIDERS: list[str] = [name for name, (_, fetch_fn) in REGISTRY.items() if fetch_fn]
POOLS: dict[str, KeyPool] = {
    name: KeyPool(name, list(keys)) for name, keys in API_KEYS.items() if name in REGISTRY
}


def redact(text: str) -> str:
    """把已配置的 key 和代理凭据从错误文本里抹掉。"""
    if not text:
        return text
    out = text
    for keys in API_KEYS.values():
        for key in keys:
            if key and len(key) >= 8:
                out = out.replace(key, "***")
    if SEARCH_PROXY and "@" in SEARCH_PROXY:
        out = re.sub(r"(//)[^/@\s]+@", r"\1***@", out)
    return out


def provider_enabled(name: str, explicit: bool = False) -> bool:
    pool = POOLS.get(name)
    if pool and pool.keys:
        return True
    if name == "searxng" and SEARXNG_BASE_URL:
        return True
    if explicit and name in KEYLESS_KNOWN:
        return True
    return name in KEYLESS_ENABLED


def _client(name: str) -> httpx.AsyncClient:
    """按 provider 决定是否走代理，避免把国内源请求和密钥绕到代理。"""
    kwargs: dict = {"follow_redirects": True}
    if SEARCH_PROXY and (SEARCH_PROXY_ALL or name in PROXY_PROVIDERS):
        kwargs["proxy"] = SEARCH_PROXY
    return httpx.AsyncClient(**kwargs)


def enabled_search_providers() -> list[str]:
    names = [name for name in REGISTRY if provider_enabled(name)]
    if SEARCH_PROVIDERS_LIMIT:
        names = [name for name in names if name in SEARCH_PROVIDERS_LIMIT]
    return names


async def _with_keys(name: str, keys: list[str], call):
    """通用 key 轮询执行器，保证异常时也会释放 in-flight 标记。"""
    attempted: set[str] = set()
    last_error: ProviderError | None = None
    while True:
        key = POOLS[name].next_key(attempted)
        if key is None:
            break
        attempted.add(_fingerprint(key))
        try:
            value = await call(key)
        except ProviderError as exc:
            last_error = exc
            POOLS[name].record_failure(key, exc.status, redact(str(exc)), exc.retry_after)
            if exc.retryable_with_other_key:
                continue
            raise
        else:
            POOLS[name].record_success(key)
            return value
        finally:
            POOLS[name].release(key)

    if last_error is not None:
        raise ProviderError(
            f"全部 {len(keys)} 个 key 均失败（最后错误: {redact(str(last_error))}）",
            status=last_error.status,
            retry_after=last_error.retry_after,
        )
    reset = POOLS[name].earliest_reset()
    if reset:
        left = max(0, int(reset - time.time()))
        raise ProviderError(f"该家所有 key 都在冷却中，最早 {left} 秒后恢复", status=429)
    raise ProviderError("该家没有可用的 key")


async def search_with_pool(name: str, query: str, max_results: int) -> list[SearchResult]:
    search_fn = REGISTRY[name][0]
    if search_fn is None:
        raise ProviderError(f"{name} 不支持搜索")
    pool = POOLS.get(name)
    keys = list(pool.keys) if pool else []
    async with _client(name) as client:
        if not keys:
            return await search_fn(client, "", query, max_results)
        return await _with_keys(name, keys, lambda key: search_fn(client, key, query, max_results))


async def run_search(names: list[str], query: str, max_results: int):
    results: dict[str, list[SearchResult]] = {}
    errors: dict[str, str] = {}
    if not names:
        return results, errors
    outcomes = await asyncio.gather(
        *(search_with_pool(name, query, max_results) for name in names),
        return_exceptions=True,
    )
    for name, outcome in zip(names, outcomes):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, Exception):
            errors[name] = redact(f"{type(outcome).__name__}: {outcome}")
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            results[name] = outcome
    return results, errors


async def run_fetch(name: str, url: str) -> FetchResult:
    fetch_fn = REGISTRY.get(name, (None, None))[1]
    if fetch_fn is None:
        raise ProviderError(f"{name} 不是可用的抓取后端，可选: {', '.join(FETCH_PROVIDERS)}")
    if not provider_enabled(name, explicit=True):
        raise ProviderError(f"{name} 未配置 API key 或不可用")
    pool = POOLS.get(name)
    keys = list(pool.keys) if pool else []
    async with _client(name) as client:
        if not keys:
            result = await fetch_fn(client, "", url)
        else:
            result = await _with_keys(name, keys, lambda key: fetch_fn(client, key, url))
    result.content = truncate(result.content)
    return result


def pool_status() -> dict[str, str]:
    return {name: POOLS[name].status_line() for name in REGISTRY if name in POOLS}
