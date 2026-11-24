"""连通性自检与合并逻辑自测。

用法（在项目目录下）:
    .venv/Scripts/python check_providers.py                # 测全部已配置搜索商
    .venv/Scripts/python check_providers.py "关键词"        # 自定义关键词
    .venv/Scripts/python check_providers.py --fetch URL [tavily|firecrawl|...]
    .venv/Scripts/python check_providers.py --demo         # 不用 key，测合并去重 + key 轮询
    .venv/Scripts/python check_providers.py --status       # 只看各家 key 配置与冷却状态
"""
from __future__ import annotations

import asyncio
import sys

from config import DEFAULT_FETCH_PROVIDER
from keypool import classify
from providers import (
    KEYLESS_ENABLED,
    POOLS,
    REGISTRY,
    enabled_search_providers,
    pool_status,
    provider_enabled,
    run_fetch,
    run_search,
)
from providers.base import SearchResult, merge_results, normalize_url


def demo_merge() -> None:
    """无 key 也能跑：用 mock 数据验证 URL 归一化 + 合并去重排序。"""
    groups = {
        "tavily": [
            SearchResult("tavily", "https://www.example.com/a?utm_source=x", "Example A", "短摘要", 0),
            SearchResult("tavily", "https://example.org/b/", "Example B（tavily）", "B 的摘要", 1),
        ],
        "querit": [
            SearchResult("querit", "https://example.com/a/", "Example A（querit 更长标题）", "querit 提供的更长摘要", 2),
            SearchResult("querit", "https://example.com/c", "Example C", "C 的摘要", 0),
        ],
        "brave": [
            # 同一页面的另一种写法：带默认端口 + 参数顺序不同
            SearchResult("brave", "https://www.example.com:443/c?b=2&a=1", "Example C", "", 0),
            SearchResult("brave", "https://example.com/c?a=1&b=2&fbclid=xyz", "Example C", "", 1),
        ],
    }
    merged = merge_results(groups)
    assert merged[0]["sources"] == ["tavily", "querit"], merged[0]
    assert merged[0]["url"] == "https://example.com/a", merged[0]
    assert merged[0]["title"] == "Example A（querit 更长标题）", merged[0]
    assert merged[0]["snippet"] == "querit 提供的更长摘要", merged[0]

    # 带查询参数的 /c 与裸 /c 是两个不同页面，不能合并
    plain_c = next(m for m in merged if m["url"] == "https://example.com/c")
    assert plain_c["sources"] == ["querit"], plain_c

    # brave 的两种写法（默认端口 / 参数顺序不同 / 带 fbclid）应归一成同一条
    param_c = next(m for m in merged if m["url"] == "https://example.com/c?a=1&b=2")
    assert param_c["sources"] == ["brave"], param_c

    assert [m["url"] for m in merged[1:]] == [
        "https://example.com/c",
        "https://example.com/c?a=1&b=2",
        "https://example.org/b",
    ], merged
    print(
        "[demo] 合并去重 OK：7 条 mock -> 4 条；跨家聚合、默认端口/参数顺序/跟踪参数归一，"
        "带参 URL 与裸 URL 正确区分"
    )


def demo_keypool() -> None:
    """无网络验证 key 轮询与错误分档。

    用内存状态跑，绝不碰真实的 .key-state.json——否则在有真实 key 的机器上
    跑一次自检就会把生产冷却状态清掉。
    """
    import hashlib
    import tempfile
    from pathlib import Path

    import keypool as kp

    # 错误分档
    assert classify(401, "") == "invalid"
    assert classify(402, "") == "quota"
    assert classify(429, "rate limit exceeded") == "rate"
    assert classify(429, "monthly quota exceeded") == "quota"
    assert classify(500, "") is None
    # 403 只有明确指向 key 时才判 invalid，否则按限流短冷却
    assert classify(403, "invalid api key") == "invalid"
    assert classify(403, "cloudflare blocked") == "rate"

    saved = kp.KEY_STATE_FILE
    with tempfile.TemporaryDirectory() as tmp:
        kp.KEY_STATE_FILE = Path(tmp) / "state.json"
        kp._CACHE = None
        kp._CACHE_MTIME = -1.0
        kp._IN_FLIGHT.clear()
        try:
            pool = kp.KeyPool("demo", ["key-one-xxxx", "key-two-yyyy", "key-three-zzzz"])
            first = pool.next_key(set())
            assert first == "key-one-xxxx", first

            # 额度耗尽 → 打冷却，释放后下一次轮询应跳过它
            pool.record_failure(first, 402, "quota exhausted")
            pool.release(first)
            second = pool.next_key(set())
            assert second == "key-two-yyyy", second
            assert pool.available() == 2, pool.available()

            # 并发中的 key 不会被另一个请求重复拿到
            assert pool.next_key(set()) == "key-three-zzzz"
            pool.release(second)
            pool.release("key-three-zzzz")

            # 本次已试过的 key 不再返回，另一个 key 已被释放，可继续重试
            fp1 = hashlib.sha256(first.encode()).hexdigest()[:16]
            retried = pool.next_key({fp1})
            assert retried == "key-two-yyyy", retried
            pool.release(retried)

            # 成功清除标记
            pool.record_failure("key-two-yyyy", 402, "quota")
            assert pool.available() == 1, pool.available()
            pool.record_success("key-two-yyyy")
            assert pool.available() == 2, pool.available()

            # Retry-After: 0 表示立即可重试，不该被当成缺省而冷却一小时
            pool.record_failure("key-two-yyyy", 429, "rate limit", retry_after=0)
            assert pool.available() == 2, pool.available()

            # 全部冷却时 next_key 返回 None，而不是死循环
            pool.record_failure("key-two-yyyy", 402, "quota")
            pool.record_failure("key-three-zzzz", 402, "quota")
            pool.release("key-three-zzzz")
            assert pool.next_key(set()) is None
        finally:
            kp.KEY_STATE_FILE = saved
            kp._CACHE = None
            kp._CACHE_MTIME = -1.0
            kp._IN_FLIGHT.clear()
    print("[demo] key 轮询 OK：额度耗尽自动跳过、已试过的 key 不重试、全部冷却时不死循环")


async def check_search(query: str) -> None:
    names = enabled_search_providers()
    print(f"已启用搜索提供商: {', '.join(names) or '无'}")
    if not names:
        print(
            "提示：先在环境变量或项目目录 .env 里配置 "
            "TAVILY_API_KEY / BRAVE_API_KEY / EXA_API_KEY / FIRECRAWL_API_KEY 等"
        )
        return
    print(f"搜索: {query}\n")
    results, errors = await run_search(names, query, 3)
    for name in names:
        if name in results:
            pool = pool_status().get(name, "")
            suffix = f"（key {pool}）" if pool else ""
            print(f"[OK]   {name}: {len(results[name])} 条{suffix}")
            for r in results[name][:2]:
                print(f"       - {r.title or '(无标题)'}  {r.url}")
        else:
            print(f"[失败] {name}: {errors.get(name, '未知错误')}")
    total = sum(len(v) for v in results.values())
    merged = merge_results(results)
    print(f"\n合并：{total} 条 -> {len(merged)} 条唯一 URL\n")


def show_status() -> None:
    print("## 各家状态\n")
    for name in REGISTRY:
        if provider_enabled(name):
            detail = pool_status().get(name) or "免 key"
            print(f"[启用] {name}: {detail}")
        else:
            print(f"[未配] {name}")
    keyless = [n for n in REGISTRY if n in KEYLESS_ENABLED]
    print(f"\n免 key 可直接用: {', '.join(keyless) or '无'}")


async def check_fetch(url: str, provider: str) -> None:
    if not url.strip().lower().startswith(("http://", "https://")):
        print(f"[跳过] 只支持 http/https 链接，收到: {url}")
        return
    print(f"抓取 {url}（后端 {provider}）…")
    try:
        result = await run_fetch(provider, url)
    except Exception as exc:
        print(f"[失败] {provider}: {type(exc).__name__}: {exc}")
        return
    head = result.content[:150].replace("\n", " ")
    print(f"[OK]   {provider}: 标题={result.title or '（无）'} 正文 {len(result.content)} 字符")
    print(f"       开头: {head}…")


async def main() -> None:
    # Windows 控制台/管道默认 GBK，统一为 UTF-8 避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    if argv and argv[0] == "--demo":
        demo_merge()
        demo_keypool()
        return
    if argv and argv[0] == "--status":
        show_status()
        return
    if argv and argv[0] == "--fetch":
        url = argv[1] if len(argv) > 1 else "https://example.com"
        provider = argv[2] if len(argv) > 2 else DEFAULT_FETCH_PROVIDER
        await check_fetch(url, provider)
        return
    await check_search(" ".join(argv) or "Python 3.13 新特性")


if __name__ == "__main__":
    asyncio.run(main())
