"""离线自测：不联网，也不依赖、不改动本机的任何配置。

由 `aggregate-search selftest` 在一个临时沙盒里拉起：
- 配置目录指向临时目录（AGGREGATE_SEARCH_HOME），冷却状态、用量计数、使用记录都写在那里
- key 用下面这组固定的假值，所以无论你机器上配了哪些厂商，结果都一样
- 所有厂商的网络调用换成假函数，只记录「谁被调用了、带没带 key」
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

# 沙盒里的假 key。特意给 parallel 也配一个，用来证明匿名阶段不会带上它；
# 特意不配 baidu，用来证明「排在前面的厂商没配 key 时仍能凑满 3 家」；
# 特意配上 perplexity，用来证明被排除的厂商不会被自动补搜。
def _fake_key(name: str, number: int = 1) -> str:
    """生成一眼就能看出是假的 key，源码里不出现任何长得像真密钥的字符串。"""
    return f"selftest-fake-{name}-{number}"


SANDBOX_ENV: dict[str, str] = {
    "TAVILY_API_KEYS": ",".join(_fake_key("tavily", number) for number in (1, 2)),
    "VOLCENGINE_API_KEY": _fake_key("volcengine"),
    "QUERIT_API_KEY": _fake_key("querit"),
    "EXA_API_KEY": _fake_key("exa"),
    "FIRECRAWL_API_KEY": _fake_key("firecrawl"),
    "JINA_API_KEY": _fake_key("jina"),
    "PARALLEL_API_KEY": _fake_key("parallel"),
    "PERPLEXITY_API_KEY": _fake_key("perplexity"),
    "DAILY_LIMITS": "jina:2",
}

ANON_THREE = {"parallel", "keenable", "anysearch"}
CASES: list[tuple[str, object]] = []
# 真实的适配器函数：{厂商: (search, fetch)}。在换成假后端之前存下来，
# 专门用来测「适配器自己的解析和报错逻辑」——假后端测的只是调度。
REAL: dict[str, tuple] = {}


def case(name: str):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


class Fakes:
    """假的厂商后端：记录每次调用收到的 key，并按需返回空结果或报错。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: list[tuple[str, str]] = []        # [(厂商, 收到的 key)]，匿名调用 key 为空串
        self.fetch_calls: list[tuple[str, str]] = []
        self.errors: dict = {}                        # 厂商 -> 要抛的错误
        self.anon_errors: dict = {}                   # 厂商 -> 只在匿名调用时抛的错误
        self.key_errors: dict = {}                    # 具体某个 key -> 要抛的错误
        self.empty: set[str] = set()                  # 这些厂商返回空结果
        self.delay = 0.0
        self.fetch_errors: dict = {}
        self.fetch_text: dict[str, str] = {}


FAKES = Fakes()


def _fake_search(name: str):
    from .providers.base import SearchResult

    async def search(client, key, query, max_results):
        FAKES.calls.append((name, key))
        if FAKES.delay:
            await asyncio.sleep(FAKES.delay)
        error = FAKES.key_errors.get(key) or FAKES.errors.get(name)
        if not key:
            error = error or FAKES.anon_errors.get(name)
        if error:
            raise error
        if name in FAKES.empty:
            return []
        return [
            SearchResult(
                name,
                f"https://docs.example.com/page-{i}",
                f"{query} 详解 {i}",
                f"{query}：第 {i} 篇说明文档，由 {name} 返回，介绍背景、用法和注意事项。",
                i,
            )
            for i in range(min(3, max_results))
        ]

    return search


def _fake_fetch(name: str):
    from .providers.base import FetchResult

    async def fetch(client, key, url):
        FAKES.fetch_calls.append((name, key))
        error = FAKES.fetch_errors.get(name)
        if error:
            raise error
        return FetchResult(
            provider=name,
            url=url,
            title=f"{name} 抓到的标题",
            content=FAKES.fetch_text.get(name, "这是一段足够长的正文。" * 40),
        )

    return fetch


def _install_fakes() -> None:
    from . import providers

    REAL.update(providers.REGISTRY)
    for name, (search_fn, fetch_fn) in list(providers.REGISTRY.items()):
        providers.REGISTRY[name] = (
            _fake_search(name) if search_fn else None,
            _fake_fetch(name) if fetch_fn else None,
        )


def _reset() -> None:
    """每个用例开始前清场：假后端、搜索缓存、冷却状态、用量计数、使用记录。"""
    from . import config, keypool, providers, server, usage

    FAKES.reset()
    server._RECENT.clear()
    server._SERIAL.clear()
    for path in (config.KEY_STATE_FILE, config.USAGE_COUNTER_FILE, config.USAGE_LOG_FILE):
        path.unlink(missing_ok=True)
    keypool._CACHE = None
    keypool._CACHE_MTIME = -1.0
    keypool._IN_FLIGHT.clear()
    providers._ANON_COOLDOWN.clear()
    usage._CACHE = None
    usage._CACHE_MTIME = -1.0
    for pool in providers.POOLS.values():
        pool._cursor = 0


def _keyed() -> list[str]:
    return [name for name, key in FAKES.calls if key]


def config_log_text() -> str:
    """使用记录文件的原始内容。"""
    from . import config

    return config.USAGE_LOG_FILE.read_text(encoding="utf-8")


# ---------------- 全局化 ----------------

@case("配置与状态文件都在全局配置目录，不依赖代码所在目录")
def t_config_home():
    from . import config

    home = Path(os.environ["AGGREGATE_SEARCH_HOME"]).resolve()
    assert config.CONFIG_DIR == home, config.CONFIG_DIR
    for path in (
        config.ENV_FILE, config.KEYS_FILE, config.KEY_STATE_FILE,
        config.USAGE_LOG_FILE, config.USAGE_COUNTER_FILE,
    ):
        assert home in path.parents, path
    # 指定了配置目录，就不再去读源码目录里的旧 .env
    assert config.legacy_file(".env") is None
    package_dir = Path(config.__file__).resolve().parent
    assert home not in package_dir.parents and package_dir not in home.parents


# ---------------- 匿名阶段不带 key ----------------

@case("匿名阶段不带账号 key（即使配了 PARALLEL_API_KEY）")
async def t_auto_anonymous():
    from . import providers, server

    text = await server.web_search("MCP 协议是什么")
    assert {name for name, _ in FAKES.calls} == ANON_THREE, FAKES.calls
    assert all(key == "" for _, key in FAKES.calls), FAKES.calls
    assert providers.has_keys("parallel"), "沙盒里应当配了 parallel 的 key"
    assert "没有发送任何账号 key" in text, text
    assert "本地门禁通过" in text, text


@case("free 模式：显式指定配了 key 的 parallel 也能用，且不带 key")
async def t_free_explicit():
    from . import server

    text = await server.web_search(
        "MCP 协议是什么", providers=["parallel", "tavily"], search_mode="free"
    )
    assert FAKES.calls == [("parallel", "")], FAKES.calls
    assert "tavily: 未调用（free 模式只做匿名调用" in text, text
    assert "没有发送任何账号 key" in text, text


@case("匿名通道被限流：auto 改用该家的 key 并如实说明，冷却期内不再白发匿名请求")
async def t_anon_rate_limited():
    from . import providers, server
    from .providers.base import ProviderError

    limited = ProviderError("You've hit the free-tier rate limit", status=429, retryable=True)
    FAKES.anon_errors["parallel"] = limited      # 配了 key
    FAKES.anon_errors["keenable"] = limited      # 没配 key
    text = await server.web_search("MCP 协议是什么")
    parallel_calls = [key for name, key in FAKES.calls if name == "parallel"]
    assert len(parallel_calls) == 2 and parallel_calls[0] == "" and parallel_calls[1], FAKES.calls
    assert [key for name, key in FAKES.calls if name == "keenable"] == [""], FAKES.calls
    assert "parallel（key#1）: OK" in text and "匿名通道被限流，改用 key" in text, text
    assert "其中 parallel 是匿名通道被限流后才改用 key" in text, text
    assert "keenable（匿名）: 失败" in text, text

    # 冷却期内：parallel 直接用 key，keenable 不再发请求
    FAKES.calls.clear()
    await server.web_search("另一个问题 MCP 怎么用")
    assert [key for name, key in FAKES.calls if name == "parallel"] == [parallel_calls[1]], FAKES.calls
    assert "keenable" not in dict(FAKES.calls), FAKES.calls
    assert providers.anon_cooling("parallel") > 0


@case("匿名通道被限流：free 模式仍然绝不带 key")
async def t_anon_rate_limited_free():
    from . import server
    from .providers.base import ProviderError

    FAKES.anon_errors["parallel"] = ProviderError("rate limit", status=429, retryable=True)
    text = await server.web_search("MCP 协议是什么", search_mode="free")
    assert not _keyed(), FAKES.calls
    assert "parallel（匿名）: 失败" in text and "没有发送任何账号 key" in text, text


@case("free 模式：覆盖不足也绝不补搜")
async def t_free_never_escalates():
    from . import server

    FAKES.empty = set(ANON_THREE)
    text = await server.web_search("MCP 协议是什么", search_mode="free")
    assert len(FAKES.calls) == 3 and not _keyed(), FAKES.calls
    assert "escalate" not in text, text


@case("all 模式：全部已启用厂商，配了 key 的才带 key")
async def t_all_mode():
    from . import providers, server

    await server.web_search("MCP 协议是什么", search_mode="all")
    called = dict(FAKES.calls)
    assert set(called) == set(providers.enabled_search_providers()), called
    assert called["parallel"] and called["tavily"], called
    assert called["keenable"] == "" and called["anysearch"] == "", called


# ---------------- 并发与多 key ----------------

@case("并发：只配了一个 key 的厂商也能同时服务多个请求")
async def t_concurrency():
    from . import keypool, providers

    FAKES.delay = 0.05
    first, second = await asyncio.gather(
        providers.run_search(["volcengine"], "问题一", 3),
        providers.run_search(["volcengine"], "问题二", 3),
    )
    assert first[2][0].ok and second[2][0].ok, (first[1], second[1])
    # 配了两个 key 的厂商：并发请求分到不同的 key，而不是挤在同一个上
    FAKES.calls.clear()
    await asyncio.gather(
        providers.run_search(["tavily"], "问题一", 3),
        providers.run_search(["tavily"], "问题二", 3),
    )
    assert len({key for _, key in FAKES.calls}) == 2, FAKES.calls
    assert not keypool._IN_FLIGHT, keypool._IN_FLIGHT


@case("key 池：轮转、冷却、已试过的不重试、单 key 可共用")
def t_keypool():
    from . import keypool
    from .keypool import classify

    assert classify(401, "") == "invalid"
    assert classify(402, "") == "quota"
    assert classify(429, "rate limit exceeded") == "rate"
    assert classify(429, "monthly quota exceeded") == "quota"
    assert classify(500, "") is None
    # 403 只有明确指向 key 时才判 invalid，否则按限流短冷却
    assert classify(403, "invalid api key") == "invalid"
    assert classify(403, "cloudflare blocked") == "rate"

    pool = keypool.KeyPool("demo", ["key-one-xxxx", "key-two-yyyy", "key-three-zzzz"])
    first = pool.next_key(set())
    assert first == "key-one-xxxx", first
    # 额度耗尽 → 打冷却，释放后下一次轮询应跳过它
    pool.record_failure(first, 402, "quota exhausted")
    pool.release(first)
    second = pool.next_key(set())
    assert second == "key-two-yyyy", second
    assert pool.available() == 2, pool.available()
    # 另一个并发请求优先拿到空闲的 key
    assert pool.next_key(set()) == "key-three-zzzz"
    pool.release(second)
    pool.release("key-three-zzzz")
    # 本次已试过的 key 不再返回
    retried = pool.next_key({keypool._fingerprint(first)})
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
    # 全部冷却时返回 None，而不是死循环
    pool.record_failure("key-two-yyyy", 402, "quota")
    pool.record_failure("key-three-zzzz", 402, "quota")
    assert pool.next_key(set()) is None

    # 只有一个 key：并发请求共用它，而不是让后来的请求拿不到
    solo = keypool.KeyPool("demo-solo", ["only-key-aaaa"])
    assert solo.next_key(set()) == "only-key-aaaa"
    assert solo.next_key(set()) == "only-key-aaaa"
    solo.release("only-key-aaaa")
    solo.release("only-key-aaaa")
    assert not keypool._IN_FLIGHT, keypool._IN_FLIGHT


@case("同一家多 key：额度耗尽自动换下一个 key 并进入冷却")
async def t_key_rotation():
    from . import config, providers
    from .providers.base import ProviderError

    first, second = config.API_KEYS["tavily"]
    FAKES.key_errors[first] = ProviderError("HTTP 402: quota exhausted", status=402)
    _, _, records = await providers.run_search(["tavily"], "问题", 3)
    record = records[0]
    assert record.ok and record.auth == "key", record
    assert record.key_index == 2 and record.keys_tried == 2, record
    assert providers.POOLS["tavily"].usable() == 1
    # 下一次直接用第二个 key，不再碰冷却中的那个
    FAKES.calls.clear()
    await providers.run_search(["tavily"], "问题", 3)
    assert FAKES.calls == [("tavily", second)], FAKES.calls


# ---------------- 补搜：凑满 3 家并递补 ----------------

@case("auto 覆盖不足：追加 3 个不同且可用的厂商")
async def t_auto_escalate():
    from . import server

    FAKES.empty = set(ANON_THREE)
    text = await server.web_search("MCP 协议是什么")
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    assert "动用了 **3 个厂商的账号 key**" in text, text


@case("国内问题：排在前面的百度没配 key，跳过后仍凑满 3 家")
async def t_domestic_fills():
    from . import providers, server

    assert not providers.has_keys("baidu")
    FAKES.empty = set(ANON_THREE)
    text = await server.web_search("中国最新的人工智能政策")
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    assert "baidu" not in dict(FAKES.calls)
    assert "识别到国内实体" in text, text


@case("补搜有厂商失败时自动递补")
async def t_backfill():
    from . import server
    from .providers.base import ProviderError

    FAKES.empty = set(ANON_THREE)
    FAKES.errors["volcengine"] = ProviderError("HTTP 500: upstream boom", status=500)
    text = await server.web_search("MCP 协议是什么")
    assert _keyed() == ["volcengine", "querit", "tavily", "exa"], FAKES.calls
    assert "volcengine（key）: 失败（HTTP 500" in text, text
    assert "有效返回 **3 家**" in text and "失败 **1 家**" in text, text


@case("补搜候选：匿名失败的可带 key 重试，不重复已失败的调用")
def t_ready_rules():
    from . import server

    ready = server._escalation_ready
    assert ready("volcengine", {}, {})
    assert ready("parallel", {}, {"parallel": {"anon"}})              # 匿名失败 → 带 key 再试
    assert not ready("parallel", {}, {"parallel": {"anon", "key"}})   # 带 key 也试过了
    assert not ready("parallel", {"parallel": []}, {})                # 已经有它的结果
    assert not ready("keenable", {}, {"keenable": {"anon"}})          # 没有 key，匿名失败就不重复
    assert not ready("baidu", {}, {})                                  # 没配 key


# ---------------- escalate 模式与 region 参数 ----------------

@case("escalate：只补 3 家新厂商，并与此前的匿名结果合并")
async def t_escalate_mode():
    from . import server

    query = "MCP 协议是什么"
    first = await server.web_search(query)
    assert 'search_mode="escalate"' in first, first
    assert "预计 volcengine, querit, tavily" in first, first

    FAKES.calls.clear()
    second = await server.web_search(query, search_mode="escalate")
    assert FAKES.calls and all(key for _, key in FAKES.calls), FAKES.calls
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    assert "并入" in second and "parallel: 沿用此前结果" in second, second
    assert "（6 家：" in second, second          # 匿名 3 家 + 补搜 3 家的证据合并到同一页面

    FAKES.calls.clear()
    await server.web_search(query, search_mode="escalate")
    assert _keyed() == ["exa", "firecrawl", "jina"], FAKES.calls   # 再补就换下一批

    FAKES.calls.clear()
    fourth = await server.web_search(query, search_mode="escalate")
    assert FAKES.calls == [], FAKES.calls
    assert "已没有尚未查询且可用的厂商" in fourth, fourth


@case("escalate：没有此前结果时直接补 3 家，不跑匿名阶段")
async def t_escalate_fresh():
    from . import server

    text = await server.web_search("Kubernetes 滚动升级怎么回滚", search_mode="escalate")
    assert _keyed() == ["tavily", "exa", "firecrawl"], FAKES.calls   # 识别到国际实体
    assert len(FAKES.calls) == 3
    assert "没有找到同一问题的近期结果" in text, text


@case("region 参数：Agent 指定后覆盖关键词判断")
async def t_region_param():
    from . import server

    FAKES.empty = set(ANON_THREE)
    await server.web_search("中国最新的人工智能政策", region="global")
    assert _keyed() == ["tavily", "exa", "firecrawl"], FAKES.calls

    _reset()
    FAKES.empty = set(ANON_THREE)
    text = await server.web_search("OpenAI 最新 API 价格", region="国内")
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    assert "调用方指定 region=domestic" in text, text

    assert "region 仅支持" in await server.web_search("随便问问", region="mars")


@case("地域识别：只认明确的地域实体，中文提问不等于国内")
def t_region_detect():
    from . import routing

    assert routing.detect_region("中国最新的人工智能政策").region == "domestic"
    assert routing.detect_region("OpenAI 最新 API 价格").region == "global"
    assert routing.detect_region("中美云计算政策对比").region == "mixed"
    assert routing.detect_region("MCP 协议是什么").region == "auto"
    assert routing.detect_region("MCP 协议是什么", "domestic").region == "domestic"
    assert routing.normalize_region("国外") == "global"
    assert routing.normalize_region("") == "auto"
    assert routing.normalize_region("mars") is None
    ordered, _ = routing.order_escalation("中美云计算政策对比")
    assert ordered[:4] == ["baidu", "tavily", "volcengine", "exa"], ordered
    assert len(ordered) == len(set(ordered)), ordered


# ---------------- 使用记录与用量上限 ----------------

@case("本地用量上限：到量的 key 不再发请求，也不参与补搜")
async def t_limits():
    from . import config, providers, server

    assert config.DAILY_LIMITS.get("jina") == 2
    assert config.DAILY_LIMITS.get("baidu") == 90      # 内置默认：防止百度超出免费额度后计费
    for _ in range(2):
        _, _, records = await providers.run_search(["jina"], "问题", 3)
        assert records[0].ok, records[0]
    _, errors, records = await providers.run_search(["jina"], "问题", 3)
    assert not records[0].ok and "本地用量上限" in errors["jina"], errors
    assert len([c for c in FAKES.calls if c[0] == "jina"]) == 2, FAKES.calls   # 第三次没发出去
    assert not server._escalation_ready("jina", {}, {})
    line = providers.POOLS["jina"].status_line()
    assert "已到本地用量上限" in line and "今日已用 2/2" in line, line


@case("使用记录：只写在配置目录，且不含任何 key")
async def t_usage_log():
    from . import config, server, usage

    await server.web_search("MCP 协议是什么")
    FAKES.empty = set(ANON_THREE)
    await server.web_search("MCP 协议 对比 评测")       # 触发自动补搜
    await server.web_fetch("https://example.com/a")

    log = config.USAGE_LOG_FILE
    assert log.is_file() and config.CONFIG_DIR in log.parents, log
    raw = log.read_text(encoding="utf-8")
    for keys in config.API_KEYS.values():
        for key in keys:
            assert key not in raw, "使用记录里不应出现 key"
    events = usage.read_events(1)
    assert [e["tool"] for e in events] == ["web_search", "web_search", "web_fetch"], events
    assert events[0]["gate"] == "pass" and not events[0]["escalated"], events[0]
    assert all(call["auth"] == "anon" for call in events[0]["calls"]), events[0]
    assert len(events[1]["escalated"]) == 3, events[1]
    assert events[2]["backend"] == "parallel" and events[2]["auth"] == "anon", events[2]
    summary = usage.summarize(1)
    assert "| parallel |" in summary and "自动补搜 1 次" in summary, summary


# ---------------- 抓取：免费优先 ----------------

@case("抓取：免费后端优先，匿名调用")
async def t_fetch_free_first():
    from . import server

    chain = server.fetch_chain()
    assert chain[:2] == [("parallel", True), ("keenable", True)], chain
    assert [name for name, _ in chain[2:]] == ["tavily", "jina", "firecrawl", "exa", "parallel"], chain
    text = await server.web_fetch("https://example.com/a")
    assert FAKES.fetch_calls == [("parallel", "")], FAKES.fetch_calls
    assert text.startswith("抓取后端: parallel（匿名）"), text[:80]


@case("抓取：匿名后端失败或正文过短时自动换下一家")
async def t_fetch_fallback():
    from . import server
    from .providers.base import ProviderError

    FAKES.fetch_errors["parallel"] = ProviderError("HTTP 429: too many requests", status=429)
    FAKES.fetch_text["keenable"] = "太短"
    text = await server.web_fetch("https://example.com/a")
    assert [name for name, _ in FAKES.fetch_calls] == ["parallel", "keenable", "tavily"], FAKES.fetch_calls
    assert FAKES.fetch_calls[2][1] != "", "轮到 tavily 时应带 key"
    assert text.startswith("抓取后端: tavily（账号 key）"), text[:80]
    assert "已跳过失败后端: parallel（匿名）" in text and "正文过短" in text, text[:300]

    # 地址本身打不开：免费后端 + 2 个带 key 的都失败后就停，不把每家的额度都试一遍
    _reset()
    for name in ("parallel", "keenable", "tavily", "jina", "firecrawl", "exa"):
        FAKES.fetch_errors[name] = ProviderError("HTTP 422: could not be resolved", status=422)
    text = await server.web_fetch("https://example.invalid/a")
    assert [name for name, _ in FAKES.fetch_calls] == ["parallel", "keenable", "tavily", "jina"], FAKES.fetch_calls
    assert "抓取失败" in text, text
    assert "为节省额度，没有继续尝试 firecrawl（key）, exa（key）" in text, text

    # 抓到的是空正文：算失败，换下一个后端
    _reset()
    FAKES.fetch_text["parallel"] = "   "
    text = await server.web_fetch("https://example.com/a")
    assert [name for name, _ in FAKES.fetch_calls] == ["parallel", "keenable"], FAKES.fetch_calls
    assert text.startswith("抓取后端: keenable（匿名）") and "parallel 抓取成功但正文为空" in text, text[:200]

    # 指定后端时只试这一家
    _reset()
    await server.web_fetch("https://example.com/a", "exa")
    assert [name for name, _ in FAKES.fetch_calls] == ["exa"], FAKES.fetch_calls
    assert "只支持 http/https" in await server.web_fetch("ftp://example.com/a")


# ---------------- 工具说明与状态 ----------------

@case("工具说明与实际配置一致（抓取后端、模式、补搜数量）")
async def t_descriptions():
    from . import config, providers, server

    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(tools) == {"web_search", "web_fetch", "web_status"}, set(tools)
    for name in providers.FETCH_PROVIDERS:
        assert name in tools["web_fetch"].description, name
    search = tools["web_search"].description
    for mode in config.SEARCH_MODES:
        assert mode in search, mode
    assert f"追加 {config.AUTO_ESCALATE_MAX} 个不同厂商" in search, search
    assert "region" in tools["web_search"].inputSchema["properties"]
    assert "usage_days" in tools["web_status"].inputSchema["properties"]


@case("web_status：显示配置位置、抓取顺序、可补搜厂商和用量上限")
async def t_status():
    from . import config, server

    text = await server.web_status()
    assert str(config.CONFIG_DIR) in text, text
    assert "parallel（匿名） → keenable（匿名）" in text, text
    assert "第一阶段匿名调用，匿名被限流、补搜或点名指定时才用 key" in text, text
    assert "baidu 每日 90 次/key" in text, text
    assert "近 7 天使用记录" in await server.web_status(usage_days=7)


# ---------------- 其它回归 ----------------

@case("错误信息里的 key 会被抹掉")
async def t_redact():
    from . import config, server
    from .providers.base import ProviderError

    key = config.API_KEYS["volcengine"][0]
    FAKES.errors["volcengine"] = ProviderError(f"HTTP 400: bad token {key}", status=400)
    text = await server.web_search("MCP 协议是什么", providers=["volcengine"])
    assert key not in text and "***" in text, text


@case("合并去重：URL 归一化、多摘要保留、近似转载折叠、版本号不误合并")
def t_merge():
    import httpx

    from .providers.base import (
        SearchResult,
        decode_mcp_response,
        fold_near_duplicates,
        merge_results,
        normalize_url,
    )

    sse = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n\n',
    )
    assert decode_mcp_response(sse) == {"content": []}
    assert normalize_url("https://example.com/a?share_token=one") != normalize_url(
        "https://example.com/a?share_token=two"
    )
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
    # 默认端口 / 参数顺序不同 / 带 fbclid 的两种写法应归一成同一条
    param_c = next(m for m in merged if m["url"] == "https://example.com/c?a=1&b=2")
    assert param_c["sources"] == ["brave"], param_c
    assert [m["url"] for m in merged[1:]] == [
        "https://example.com/c",
        "https://example.com/c?a=1&b=2",
        "https://example.org/b",
    ], merged

    # 多份摘要必须保留，不能因同 URL 合并而丢互补证据
    evidence = merge_results({
        "tavily": [SearchResult("tavily", "https://example.com/evidence", "证据标题", "第一家给出的定义和背景信息", 0)],
        "exa": [SearchResult("exa", "https://example.com/evidence", "证据标题", "第二家补充了发布日期和适用范围", 1)],
    })[0]
    assert len(evidence["evidence"]) == 2 and len(evidence["snippets"]) == 2
    assert evidence["source_count"] == 2

    long_text = "模型上下文协议为 AI 应用连接外部工具和数据提供统一标准，并支持多种传输方式。"
    copies = merge_results({
        "one": [SearchResult("one", "https://one.example/article", "模型上下文协议完整指南", long_text, 0)],
        "two": [SearchResult("two", "https://two.example/repost", "模型上下文协议完整指南", long_text, 0)],
    })
    folded, count = fold_near_duplicates(copies)
    assert count == 1 and len(folded) == 1
    assert folded[0]["alternate_urls"] == ["https://two.example/repost"]
    assert folded[0]["source_count"] == 2

    # 同一主题但内容不同的文章不能因关键词相似而被误删
    distinct = merge_results({
        "one": [SearchResult("one", "https://one.example/install", "MCP 安装教程", "这篇文章逐步介绍 Windows 下的 Python 环境安装和客户端配置方法。", 0)],
        "two": [SearchResult("two", "https://two.example/security", "MCP 安全分析", "这篇文章讨论权限边界、提示注入风险和远程服务器的安全审计问题。", 0)],
    })
    kept, count = fold_near_duplicates(distinct)
    assert count == 0 and len(kept) == 2

    # 只差版本号、年份或关键数字时，即使字符串很相似也禁止折叠
    versions = merge_results({
        "one": [SearchResult("one", "https://one.example/v13", "Python 3.13 新特性完整指南", "本文介绍 Python 3.13 的新特性、性能变化和升级注意事项，适用于准备升级的开发者。", 0)],
        "two": [SearchResult("two", "https://two.example/v14", "Python 3.14 新特性完整指南", "本文介绍 Python 3.14 的新特性、性能变化和升级注意事项，适用于准备升级的开发者。", 0)],
    })
    version_kept, version_count = fold_near_duplicates(versions)
    assert version_count == 0 and len(version_kept) == 2

    # 折叠新增来源后必须重新计算 source_count、best_rank 和排序
    rerank_input = merge_results({
        "a": [SearchResult("a", "https://a.example/story", "相同文章标题用于测试排序", "这是完全相同的文章摘要内容，用于验证转载折叠后的排名和来源统计是否正确。", 8)],
        "b": [SearchResult("b", "https://b.example/repost", "相同文章标题用于测试排序", "这是完全相同的文章摘要内容，用于验证转载折叠后的排名和来源统计是否正确。", 9)],
        "c": [SearchResult("c", "https://c.example/other", "另一篇独立文章", "完全不同的内容用于产生一个独立结果，不应和前面的文章合并。", 0)],
        "d": [SearchResult("d", "https://d.example/other", "另一篇独立文章", "完全不同的内容用于产生一个独立结果，不应和前面的文章合并。", 0)],
    })
    reranked, rerank_count = fold_near_duplicates(rerank_input)
    assert rerank_count == 1
    repost = next(x for x in reranked if x["url"] == "https://a.example/story")
    assert repost["source_count"] == 2 and repost["best_rank"] == 8
    assert repost["evidence"][1]["url"] == "https://b.example/repost"
    assert reranked[0]["source_count"] == 2


# ---------------- 审查后补充的回归用例 ----------------

@case("escalate 不能和 providers 一起用（否则只会把点名的厂商重查一遍）")
async def t_escalate_with_providers():
    from . import server

    text = await server.web_search("MCP 协议是什么", providers=["tavily"], search_mode="escalate")
    assert "不能和 providers 一起用" in text, text
    assert FAKES.calls == [], FAKES.calls
    # 但如果 escalate 只是本机的默认值、调用方并没有明说，点名就按「查这几家」处理
    saved = server.SEARCH_MODE
    server.SEARCH_MODE = "escalate"
    try:
        named = await server.web_search("MCP 协议是什么", providers=["keenable"])
    finally:
        server.SEARCH_MODE = saved
    assert [name for name, _ in FAKES.calls] == ["keenable"] and "执行模式: **auto**" in named, named
    FAKES.calls.clear()
    # 点名查询的结果里，提示会提醒下一步别再带 providers
    hint = await server.web_search("MCP 协议是什么", providers=["keenable"])
    assert "这次不要带 providers" in hint, hint


@case("补搜：没搜到结果的厂商不算数，继续往后递补")
async def t_backfill_on_empty():
    from . import server

    FAKES.empty = set(ANON_THREE) | {"volcengine"}
    text = await server.web_search("MCP 协议是什么")
    assert _keyed() == ["volcengine", "querit", "tavily", "exa"], FAKES.calls
    assert "空结果 **4 家**" in text and "补搜数量" not in text, text


@case("补搜：一整轮全是网络错误就不再递补")
async def t_no_backfill_when_offline():
    from . import server
    from .providers.base import NETWORK_ERROR, ProviderError

    FAKES.empty = set(ANON_THREE)
    for name in ("volcengine", "querit", "tavily"):
        FAKES.errors[name] = ProviderError(f"{NETWORK_ERROR}: ConnectTimeout: ")
    text = await server.web_search("MCP 协议是什么")
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    assert "全部是网络错误，没有继续递补" in text, text


@case("总时限：慢的厂商到点就放弃，带着已有结果返回，不再开新一轮补搜")
async def t_total_timeout():
    import time

    from . import server

    saved = server.TOTAL_TIMEOUT
    server.TOTAL_TIMEOUT = 0.3
    FAKES.delay = 2.0
    try:
        tick = time.monotonic()
        text = await server.web_search("MCP 协议是什么")
        elapsed = time.monotonic() - tick
    finally:
        server.TOTAL_TIMEOUT = saved
    assert elapsed < 1.5, elapsed
    assert not _keyed(), FAKES.calls
    assert "等待超过总时限" in text and "已到总时限 0.3 秒，没有继续递补" in text, text


@case("同一问题再跑一次 auto，escalate 也不会忘记已经补过哪些厂商")
async def t_cache_merges():
    from . import server

    query = "MCP 协议是什么"
    await server.web_search(query)
    await server.web_search(query, search_mode="escalate")
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    again = await server.web_search(query)                       # 以前这一步会把缓存整个覆盖掉
    assert "预计 exa, firecrawl, jina" in again, again
    FAKES.calls.clear()
    await server.web_search(query, search_mode="escalate")
    assert _keyed() == ["exa", "firecrawl", "jina"], FAKES.calls


@case("同一问题的两个并发 escalate 不会补同一批厂商")
async def t_concurrent_escalate():
    from . import server

    FAKES.delay = 0.05
    query = "MCP 协议是什么"
    await asyncio.gather(
        server.web_search(query, search_mode="escalate"),
        server.web_search(query, search_mode="escalate"),
    )
    keyed = _keyed()
    assert sorted(keyed) == sorted(
        ["volcengine", "querit", "tavily", "exa", "firecrawl", "jina"]
    ), FAKES.calls
    assert len(keyed) == len(set(keyed)), keyed
    assert not server._SERIAL, server._SERIAL


@case("成本如实：key 在冷却或到量、请求没发出去时，不算动用了 key")
async def t_cost_only_counts_sent():
    from . import providers, server, usage

    for _ in range(2):                                           # 把 jina 用到每日上限
        await providers.run_search(["jina"], "问题", 3)
    FAKES.calls.clear()
    query = "MCP 协议是什么"
    text = await server.web_search(query, providers=["jina", "keenable"])
    assert [name for name, _ in FAKES.calls] == ["keenable"], FAKES.calls
    assert "jina（key）: 没有发出请求" in text, text
    assert "没有发送任何账号 key" in text and "本次查询 **1 家**" in text, text
    call = next(c for c in usage.read_events(1)[-1]["calls"] if c["p"] == "jina")
    assert call["keys"] == 0 and call["sent"] is False, call
    assert "动用过账号 key 的搜索: 0 次" in usage.summarize(1)
    _, tried, _ = server._recall(query)
    assert "jina" not in tried, tried                            # 没发出请求不算试过


@case("多个 key 都失败时，如实记下试了几个")
async def t_keys_tried():
    from . import config, providers
    from .providers.base import ProviderError

    for key in config.API_KEYS["tavily"]:
        FAKES.key_errors[key] = ProviderError("HTTP 402: quota exhausted", status=402)
    _, errors, records = await providers.run_search(["tavily"], "问题", 3)
    record = records[0]
    assert not record.ok and record.keys_tried == 2 and record.used_key and record.sent, record
    assert "已试 2 个 key 均失败" in errors["tavily"], errors
    assert record.as_log()["keys"] == 2, record.as_log()


@case("抓取：正文过短只复核一次，不会把所有带 key 的后端都试一遍")
async def t_fetch_recheck_once():
    from . import server
    from .providers.base import ProviderError

    FAKES.fetch_text["parallel"] = "太短"
    FAKES.fetch_errors["keenable"] = ProviderError("upstream error")
    FAKES.fetch_errors["tavily"] = ProviderError("HTTP 500: boom", status=500)
    text = await server.web_fetch("https://example.com/a")
    # 以前：parallel 过短 → 之后每个带 key 的后端都会被真实调用一遍
    assert [name for name, _ in FAKES.fetch_calls] == ["parallel", "keenable", "tavily"], FAKES.fetch_calls
    assert text.startswith("抓取后端: parallel（匿名）"), text[:80]
    assert "没能用别的后端复核成功" in text, text[:400]


@case("抓取：WEB_FETCH_PROVIDER 点名的首选后端不受「带 key 后端上限」约束")
async def t_fetch_preferred_exempt():
    from . import server
    from .providers.base import ProviderError

    saved = server.FETCH_PREFERRED, server.FETCH_KEYED_FALLBACKS
    server.FETCH_PREFERRED, server.FETCH_KEYED_FALLBACKS = ["exa"], 0
    for name in ("exa", "parallel", "keenable", "tavily", "jina", "firecrawl"):
        FAKES.fetch_errors[name] = ProviderError("HTTP 422: could not be resolved", status=422)
    try:
        text = await server.web_fetch("https://example.invalid/a")
    finally:
        server.FETCH_PREFERRED, server.FETCH_KEYED_FALLBACKS = saved
    assert [name for name, _ in FAKES.fetch_calls] == ["exa", "parallel", "keenable"], FAKES.fetch_calls
    assert "为节省额度，没有继续尝试 tavily（key）" in text, text


@case("使用记录里的网址不带账号口令和 ? 后面的参数")
async def t_fetch_url_logged_safely():
    from . import server, usage

    # 测试用的网址在运行时拼出来，源码里不出现任何长得像口令或令牌的字面量
    params = "&".join(f"{name}=1" for name in ("page", "sig"))
    userinfo = ":".join(("someone", "something"))
    await server.web_fetch(f"https://example.com/a/b?{params}")
    assert usage.read_events(1)[-1]["url"] == "https://example.com/a/b?…"
    assert (
        server._loggable_url(f"https://{userinfo}@example.com:8443/x?{params}")
        == "https://example.com:8443/x?…"
    )
    assert server._loggable_url("https://example.com/plain") == "https://example.com/plain"

    # 后端的报错里带着完整网址时，记录里同样不留 ? 后面的参数
    from .providers.base import ProviderError

    _reset()
    target = f"https://example.com/private?{params}"
    for name in ("parallel", "keenable", "tavily", "jina"):
        FAKES.fetch_errors[name] = ProviderError(f"Error fetching {target}: upstream failed")
    reply = await server.web_fetch(target)
    assert target in reply, reply                       # 返回给调用方的内容不受影响
    raw = config_log_text()
    assert params not in raw and "https://example.com/private?…" in raw, raw

    # 报错里的网址被截断过、或已经脱敏过，整串对不上原网址时也一样处理
    from .providers.base import scrub_urls

    long_url = "https://example.com/file?" + "&".join(f"k{index}=v{index}" for index in range(40))
    assert scrub_urls(f"Error fetching {long_url[:120]} boom") == "Error fetching https://example.com/file?… boom"
    assert scrub_urls(f"got https://{userinfo}@example.com/x?{params} and https://example.com/y") == (
        "got https://***@example.com/x?… and https://example.com/y"
    )


@case(".env 写错一行不会让服务起不来，告警里也不显示等号右边的内容")
def t_dotenv_tolerant():
    from . import config

    saved_log, before = config.log, len(config.WARNINGS)
    config.log = lambda message: None                            # 别让预期内的告警刷屏
    try:
        values = config.parse_dotenv(
            "============ 一、搜索厂商 ============\n"           # 分隔线被误删了开头的 #
            "MY-BAD-NAME=right-hand-side\n"
            "export GOOD=1   # 行内注释\n"
            "GOOD=2\n"
        )
        warnings = config.WARNINGS[before:]
    finally:
        config.log = saved_log
        del config.WARNINGS[before:]
    assert values == {"GOOD": "2"}, values                       # 重复的变量以最后一次为准
    assert len(warnings) == 2 and "第 1 行" in warnings[0] and "第 2 行" in warnings[1], warnings
    assert not any("right-hand-side" in message for message in warnings), warnings


@case("报错文案的判断：参数错误不会被当成 key 失效或限流")
def t_business_status():
    import time

    from . import config, keypool
    from .providers.base import business_error, business_status, fetch_error

    for text, expected in (
        ("You have hit the free-tier rate limit", 429),
        ("Too Many Requests", 429),
        ("请求过于频繁", 429),
        ("Invalid API key provided", 401),
        ("Unauthorized", 401),
        ("insufficient credits", 402),
        ("账户余额不足", 402),
        ("Unrecognized key(s) in object: session_id", 0),       # 以前：命中 "key" → 判成 key 失效
        ("limit must be less than or equal to 10", 0),          # 以前：命中 "limit" → 判成限流
        ("failed to generate a moderate summary", 0),           # 以前：命中 "rate" → 判成限流
        ("internal server error", 0),
    ):
        assert business_status(text) == expected, (text, business_status(text))
    assert business_error("Unauthorized").status == 401
    # 抓取工具的报错说的多半是目标网页（401 / 403 / 被限流），不拿它判断我们的账号
    for text in (
        "Error fetching https://example.com: 401 Unauthorized",
        "target site returned 429 Too Many Requests (rate limit)",
    ):
        blocked = fetch_error(text)
        assert blocked.status == 0 and not blocked.retryable_with_other_key and blocked.soft, text

    # 状态码是从文案里猜的（soft）：最多按额度冷却，不会把 key 封一整天
    soft = keypool.KeyPool("demo-soft", ["soft-demo-key-0001"])
    soft.record_failure("soft-demo-key-0001", 401, "unauthorized", soft=True)
    assert soft.earliest_reset() - time.time() <= config.QUOTA_COOLDOWN + 5
    hard = keypool.KeyPool("demo-hard", ["hard-demo-key-0001"])
    hard.record_failure("hard-demo-key-0001", 401, "unauthorized")
    assert hard.earliest_reset() - time.time() > config.QUOTA_COOLDOWN + 5


@case("补搜名单里没写到的厂商，配了 key 也会被用上；明确排除的不会")
def t_escalation_covers_all_keyed():
    from . import providers, routing, server

    assert providers.has_keys("perplexity")                      # 沙盒里配了 key，但默认被排除
    saved = routing.PAID_PROVIDERS, routing.DOMESTIC_PROVIDERS, routing.GLOBAL_PROVIDERS
    routing.PAID_PROVIDERS, routing.DOMESTIC_PROVIDERS, routing.GLOBAL_PROVIDERS = ["volcengine"], [], []
    try:
        ordered, _ = server._escalation_order("MCP 协议是什么", "auto")
    finally:
        routing.PAID_PROVIDERS, routing.DOMESTIC_PROVIDERS, routing.GLOBAL_PROVIDERS = saved
    assert ordered[0] == "volcengine", ordered
    for name in ("tavily", "querit", "exa", "firecrawl", "jina", "parallel"):
        assert name in ordered, (name, ordered)
    assert "perplexity" not in ordered and len(ordered) == len(set(ordered)), ordered
    assert "baidu" not in ordered and "duckduckgo" not in ordered, ordered   # 没启用的不会混进来
    # 匿名源即使配了 key，补搜时也先走匿名，被限流才用 key
    assert server._escalation_plan("parallel", {}, {}) == "anon+key"
    assert server._escalation_plan("keenable", {}, {}) == "anon"
    assert server._escalation_plan("tavily", {}, {}) == "key"
    assert server._escalation_plan("perplexity", {}, {}) == "key"   # 能调，只是不在自动补搜的名单里


@case("带 key 的请求不会跟着跳转到别的主机")
async def t_no_cross_host_redirect_with_key():
    import httpx

    from . import config
    from .providers import _keep_keys_on_host
    from .providers.base import ProviderError

    key = config.API_KEYS["exa"][0]
    seen: list[tuple[str, bool]] = []          # [(主机, 这次请求里有没有带 key)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, key in " ".join(request.headers.values())))
        if request.url.path == "/away":
            return httpx.Response(302, headers={"location": "https://elsewhere.example/collect"})
        if request.url.path == "/moved":
            return httpx.Response(307, headers={"location": "/v2/search"})
        if request.url.path == "/apex":
            return httpx.Response(308, headers={"location": "https://exa.ai/v2/search"})
        if request.url.path == "/downgrade":
            return httpx.Response(302, headers={"location": "http://api.exa.ai/v2/search"})
        return httpx.Response(200, json={"ok": True})

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [_keep_keys_on_host]},
        )

    async with client() as http:
        try:
            await http.post("https://api.exa.ai/away", headers={"x-api-key": key}, json={})
        except ProviderError as exc:
            assert key not in str(exc), exc
        else:
            raise AssertionError("带 key 的跨主机跳转应当被中止")
    assert seen == [("api.exa.ai", True)], seen            # 另一个站点根本没收到请求

    seen.clear()
    async with client() as http:                           # 父子域名算同一个站点，照常跟随
        response = await http.post("https://api.exa.ai/apex", headers={"x-api-key": key}, json={})
    assert response.status_code == 200 and seen[-1] == ("exa.ai", True), seen
    from .providers import _same_site
    assert _same_site("ollama.com", "www.ollama.com") and _same_site("API.exa.ai", "api.exa.ai")
    assert not _same_site("api.exa.ai", "ai") and not _same_site("api.exa.ai", "www.exa.ai")
    assert not _same_site("api.exa.ai", "notexa.ai") and not _same_site("api.exa.ai", "")

    seen.clear()
    async with client() as http:                           # 同一站点，但从 https 跳到不加密的 http
        try:
            await http.post("https://api.exa.ai/downgrade", headers={"x-api-key": key}, json={})
        except ProviderError as exc:
            assert "不加密" in str(exc) and key not in str(exc), exc
        else:
            raise AssertionError("带 key 的请求不应跟着跳到 http")
    assert seen == [("api.exa.ai", True)], seen

    seen.clear()
    async with client() as http:                           # 同一主机内的跳转照常跟随
        response = await http.post("https://api.exa.ai/moved", headers={"x-api-key": key}, json={})
    assert response.status_code == 200 and [host for host, _ in seen] == ["api.exa.ai"] * 2, seen

    seen.clear()
    async with client() as http:                           # 不带 key 的请求不受限制
        response = await http.get("https://api.exa.ai/away")
    assert response.status_code == 200 and seen[-1] == ("elsewhere.example", False), seen


@case("工具说明跟着本机的默认模式走，不会在默认是 all 时还说「不消耗额度」")
def t_description_follows_mode():
    from . import server

    assert "auto（默认）" in server._search_description()
    saved = server.SEARCH_MODE
    server.SEARCH_MODE = "all"
    try:
        text = server._search_description()
    finally:
        server.SEARCH_MODE = saved
    assert "all（默认）" in text and "auto（默认）" not in text, text
    assert "本机把默认模式设成了 all" in text and "默认先做匿名搜索" not in text, text
    fetch = server._fetch_description()
    assert "parallel（匿名） → keenable（匿名） → tavily（key）" in fetch, fetch


@case("握手时报的是本服务的版本号，日志在没有 stderr 时也不会写进协议通道")
def t_version_and_log():
    import contextlib
    import io

    from . import __version__, config, server

    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_version == __version__, options.server_version

    captured, saved = io.StringIO(), sys.stderr
    sys.stderr = None                                            # 有些宿主不给子进程 stderr
    try:
        with contextlib.redirect_stdout(captured):
            config.log("这行不能出现在 stdout")
    finally:
        sys.stderr = saved
    assert captured.getvalue() == "", captured.getvalue()


@case("状态文件被手工改坏也不会报错，坏的条目直接丢弃")
def t_corrupt_state_files():
    import time

    from . import config, keypool, usage

    config.KEY_STATE_FILE.write_text(
        '{"tavily": {"aaaa": {"reset_at": "oops"}, "bbbb": {"reset_at": NaN}, "cccc": "x",'
        f' "dddd": {{"code": "rate", "reset_at": {time.time() + 60}}}}}, "exa": []}}',
        encoding="utf-8",
    )
    keypool._CACHE, keypool._CACHE_MTIME = None, -1.0
    assert list(keypool._load_state().get("tavily", {})) == ["dddd"], keypool._load_state()

    day = time.strftime("%Y-%m-%d")
    config.USAGE_COUNTER_FILE.write_text(
        f'{{"day": {{"{day}": {{"tavily": {{"x": NaN, "y": "3", "z": 5, "w": true}}}}}}, "month": 7}}',
        encoding="utf-8",
    )
    usage._CACHE, usage._CACHE_MTIME = None, -1.0
    assert usage.used("tavily", "z") == (5, 0) and usage.used("tavily", "x") == (0, 0)

    config.USAGE_LOG_FILE.write_text(
        'not json\n[1, 2]\n{"ts": "2001-01-01T00:00:00", "tool": "web_search"}\n'
        '{"ts": "bad", "tool": "web_search"}\n'
        f'{{"ts": "{usage.now_iso()}", "tool": "web_search", "calls": "oops", "mode": "auto"}}\n',
        encoding="utf-8",
    )
    assert len(usage.read_events(1)) == 1
    assert "共 1 次" in usage.summarize(1)


@case("默认模式设成 free 时，抓取也不发送账号 key")
async def t_fetch_respects_free():
    from . import providers, server

    saved = server.SEARCH_MODE, server.FETCH_PREFERRED
    server.SEARCH_MODE, server.FETCH_PREFERRED = "free", ["tavily", "parallel"]
    try:
        chain = server.fetch_chain()
        refused = await server.web_fetch("https://example.com/a", "tavily")
        await server.web_fetch("https://example.com/a", "parallel")
        description = server._fetch_description()
        search_description = server._search_description()
    finally:
        server.SEARCH_MODE, server.FETCH_PREFERRED = saved
    assert chain == [("parallel", True), ("keenable", True)], chain
    assert "必须带 key 才能抓取" in refused, refused
    assert providers.has_keys("parallel") and FAKES.fetch_calls == [("parallel", "")], FAKES.fetch_calls
    assert "抓取只走匿名后端" in description, description
    assert "只能指定可匿名使用的后端" in description and "就会带 key" not in description, description
    assert "点名的厂商也只做匿名调用" in search_description, search_description
    assert "配了 key 的会带 key" not in search_description, search_description


@case("配置文件是 UTF-16 或 GBK 编码时也能读出来，读不了会明确告警")
def t_config_encodings():
    from . import config

    home = Path(os.environ["AGGREGATE_SEARCH_HOME"])
    sample = home / "encoding-sample.env"
    text = "# 中文注释：搜索厂商\nDEMO_NAME=demo-value\n"
    saved_log, before = config.log, len(config.WARNINGS)
    config.log = lambda message: None
    try:
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "gbk"):   # utf-16 = PowerShell 5 的 echo > 文件
            sample.write_bytes(text.encode(encoding))
            assert config.parse_dotenv(config._read_text(sample)) == {"DEMO_NAME": "demo-value"}, encoding
        assert len(config.WARNINGS) == before, config.WARNINGS[before:]
        assert config._read_text(home / "no-such-file.env") is None
        assert len(config.WARNINGS) == before + 1, config.WARNINGS[before:]
    finally:
        config.log = saved_log
        del config.WARNINGS[before:]
        sample.unlink(missing_ok=True)


@case(".env 告警只给行号：写错的行里即使有密钥片段也不会被带出来")
def t_dotenv_warning_no_leak():
    from . import config

    fragment = "".join(chr(97 + index % 26) for index in range(24))   # 运行时拼出来的假片段
    saved_log, before = config.log, len(config.WARNINGS)
    config.log = lambda message: None
    try:
        values = config.parse_dotenv(
            f'"SOME_API_KEY": "{fragment}=="\n'          # 粘成了 JSON 的写法
            f"{fragment}-{fragment}==\n"                 # 单独粘了一行带 = 结尾的 key
            f"SOME_API_KEY: {fragment}\n"                # 用了冒号，没有等号
            "GOOD=1\n"
        )
        warnings = config.WARNINGS[before:]
    finally:
        config.log = saved_log
        del config.WARNINGS[before:]
    assert values == {"GOOD": "1"}, values
    assert len(warnings) == 3 and all("行已忽略" in message for message in warnings), warnings
    assert not any(fragment in message or "SOME_API_KEY" in message for message in warnings), warnings


@case("配置留空等于用默认值，不会把功能悄悄关掉；写 none 才是清空")
def t_blank_config_uses_default():
    from . import config

    name = "SELFTEST_DEMO_LIST"
    try:
        os.environ[name] = ""
        assert config._names(name, "Alpha,beta,alpha") == ["alpha", "beta"]
        assert config._env_text(name, "fallback") == "fallback"
        os.environ[name] = "   "
        assert config._terms(name, "A,B") == ["A", "B"]
        os.environ[name] = "none"
        assert config._names(name, "alpha") == []
        os.environ[name] = "Gamma, delta ,gamma"
        assert config._names(name, "alpha") == ["gamma", "delta"]
    finally:
        os.environ.pop(name, None)


@case("客户端传入的环境变量优先于 .env；传空串不算数")
def t_env_precedence():
    from . import config

    sample = Path(os.environ["AGGREGATE_SEARCH_HOME"]) / "precedence-sample.env"
    names = ("SELFTEST_FROM_CLIENT", "SELFTEST_BLANK_IN_CLIENT", "SELFTEST_ONLY_IN_FILE")
    sample.write_text("".join(f"{name}=from-file\n" for name in names), encoding="utf-8")
    saved = config.ACTIVE_ENV_FILE
    os.environ[names[0]] = "from-client"
    os.environ[names[1]] = ""
    os.environ.pop(names[2], None)
    config.ACTIVE_ENV_FILE = sample
    try:
        config._load_dotenv()
        assert [os.environ[name] for name in names] == ["from-client", "from-file", "from-file"]
    finally:
        config.ACTIVE_ENV_FILE = saved
        for name in names:
            os.environ.pop(name, None)
        sample.unlink(missing_ok=True)


@case("auto：第一阶段没有可查的厂商时，直接进入补搜而不是放弃")
async def t_auto_empty_first_stage():
    from . import server

    saved = server.FREE_PROVIDERS
    server.FREE_PROVIDERS = []
    try:
        text = await server.web_search("MCP 协议是什么")
    finally:
        server.FREE_PROVIDERS = saved
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    assert "阶段: 补搜(volcengine, querit, tavily)" in text, text


@case("缓存里的「试过」只记实际发出的请求，没发出去的通道过一会儿还能再试")
async def t_cache_marks_only_sent():
    import time

    from . import providers, server

    query = "MCP 协议是什么"
    providers._ANON_COOLDOWN["parallel"] = time.time() + 60     # 匿名通道冷却中，配了 key
    providers._ANON_COOLDOWN["keenable"] = time.time() + 60     # 匿名通道冷却中，没有 key
    await server.web_search(query)
    assert [key for name, key in FAKES.calls if name == "parallel"] != [""], FAKES.calls
    _, tried, _ = server._recall(query)
    assert tried.get("parallel") == {"key"}, tried              # 直接用了 key，匿名请求没发
    assert "keenable" not in tried, tried                       # 一个请求都没发出去
    assert tried.get("anysearch") == {"anon"}, tried


@case("排队等上一次 escalate 的时间也算进总时限")
async def t_queue_time_counts():
    from . import server

    saved = server.TOTAL_TIMEOUT, server._MIN_ROUND_SECONDS
    server.TOTAL_TIMEOUT, server._MIN_ROUND_SECONDS = 0.6, 0.4
    FAKES.delay = 0.3
    query = "MCP 协议是什么"
    try:
        first, second = await asyncio.gather(
            server.web_search(query, search_mode="escalate"),
            server.web_search(query, search_mode="escalate"),
        )
    finally:
        server.TOTAL_TIMEOUT, server._MIN_ROUND_SECONDS = saved
    # 第二个排队等了 0.3 秒，剩下的时间不够再开一轮：带着第一个的结果返回，不再花额度
    assert _keyed() == ["volcengine", "querit", "tavily"], FAKES.calls
    assert "阶段: 补搜(volcengine, querit, tavily)" in first, first
    assert "沿用此前结果" in second and "没有继续递补" in second, second


@case("某一家返回类型不对的字段时，整次搜索照常完成，那几条被丢掉")
async def t_bad_fields_do_not_break_search():
    from . import providers, server
    from .providers.base import SearchResult

    async def junk(client, key, query, max_results):
        return [
            SearchResult("keenable", 5, 7, 9, 0),                                  # 网址是数字
            SearchResult("keenable", None, "没有网址", "x", 1),
            SearchResult("keenable", "ftp://example.com/a", "不是 http", "x", 2),
            "不是结果对象",
            SearchResult("keenable", " https://docs.example.com/ok ", 42, ["a"], "3", 2026),
        ]

    saved = providers.REGISTRY["keenable"]
    providers.REGISTRY["keenable"] = (junk, saved[1])
    try:
        text = await server.web_search("MCP 协议是什么", providers=["keenable"])
        _, _, records = await providers.run_search(["keenable"], "问题", 5)
    finally:
        providers.REGISTRY["keenable"] = saved
    assert "keenable（匿名）: OK（1 条" in text and "https://docs.example.com/ok" in text, text
    item = records[0].results[0]
    assert (item.url, item.title, item.snippet, item.published, item.rank) == (
        "https://docs.example.com/ok", "42", "", "2026", 0), item


# ---------------- 真实适配器（不走假后端）----------------

def _mock_client(status: int, body, headers: dict | None = None):
    """造一个不联网的 httpx 客户端：不管请求什么，都回同一个响应。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(status, text=str(body), headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _rpc(result=None, error=None) -> dict:
    message = {"jsonrpc": "2.0", "id": 1}
    message.update({"error": error} if error is not None else {"result": result})
    return message


# 上游可能回来的各种「不对劲」的响应。适配器遇到它们，只允许正常返回或抛 ProviderError，
# 不允许冒出 NameError / AttributeError / TypeError / KeyError 之类——那说明适配器自己有 bug。
_ODD_RESPONSES = [
    (200, {}),
    (200, []),
    (200, "this is not json"),
    (200, "null"),
    (200, "123"),
    (500, "upstream exploded"),
    (401, {"error": "invalid api key"}),
    (403, "<html>blocked by firewall</html>"),
    (429, {"error": "rate limit exceeded"}),
    (200, {"error": "rate limit exceeded"}),
    (200, {"error": {"code": 1302, "message": "rate limit"}}),
    (200, {"code": 500, "message": "quota exhausted", "msg": "quota exhausted"}),
    (200, {"code": "216100", "message": "invalid param: limit must be <= 10"}),
    (200, {"success": False, "error": "Insufficient credits"}),
    (200, {"success": False}),
    (200, {"ResponseMetadata": {"Error": {"Code": "10406", "Message": "Free quota has been exhausted"}}}),
    (200, {"ResponseMetadata": {"Error": "boom"}}),
    (200, {"ResponseMetadata": "oops", "Result": "oops"}),
    (200, {"Response": {"Error": {"Code": "RequestLimitExceeded", "Message": "too fast"}}}),
    (200, {"Response": {"Error": "boom"}}),
    (200, {"Response": {"Pages": "oops"}}),
    (200, {"Response": {"Pages": [1, None, "not json", "{\"url\": 5}", "[1,2]", {"url": None}]}}),
    (200, {"Response": "oops"}),
    (200, {"error_code": 10406, "error_msg": "quota"}),
    (200, {"error_code": "500", "error_msg": None}),
    (200, _rpc(error={"code": -32000, "message": "You have hit the free-tier rate limit"})),
    (200, _rpc(error="boom")),
    (200, _rpc(result={"isError": True, "content": [{"type": "text", "text": "403 Forbidden"}]})),
    (200, _rpc(result={"isError": True, "content": "oops"})),
    (200, _rpc(result={"content": "oops"})),
    (200, _rpc(result={"content": [1, None, {"type": "text", "text": None}]})),
    (200, _rpc(result={"content": [{"type": "text", "text": "not json"}]})),
    (200, _rpc(result={"content": [{"type": "text", "text": "{\"results\": \"oops\"}"}]})),
    (200, _rpc(result={"content": [{"type": "text", "text": "{\"results\": [1, null, {\"url\": 5, \"excerpts\": 7}]}"}]})),
    (200, _rpc(result={"content": [], "structuredContent": "oops"})),
    (200, _rpc(result={"content": [], "structuredContent": {"results": [1, None, {"url": None, "snippets": 5}]}})),
    (200, _rpc(result="oops")),
    (200, {"results": "oops", "data": "oops", "choices": "oops", "web": "oops", "search_result": "oops"}),
    (200, {"results": [None, 1, "x", {}], "data": [None, 1, {}], "choices": [{}], "search_result": [1, {}],
           "references": [None, {}], "citations": "oops"}),
    (200, {"results": [{"url": None, "title": None, "content": None, "snippets": 5, "text": 7}],
           "data": [{"url": 5, "title": 7, "description": 9}], "choices": [{"message": "oops"}],
           "search_result": [{"link": 5, "title": None, "content": 3}],
           "references": [{"url": 5, "title": 7, "content": 9}]}),
    (200, {"data": {"web": [1, None, {}], "results": [None, {}], "webPages": {"value": [1, None, {}]},
                    "markdown": 5, "metadata": "oops"},
           "results": {"web": [None, {}], "result": [None, {}]}}),
    (200, {"data": {"webPages": "oops", "web": "oops", "results": "oops"}, "results": {"result": "oops"}}),
    (200, {"code": 0, "data": "oops"}),
    (200, {"code": 0, "data": {"results": "oops"}}),
    (200, {"Result": {"WebResults": [1, None, {"Url": None, "Title": 5, "Summary": 7, "SiteName": 9}]}}),
    (200, {"choices": [{"message": {"content": 5}}], "citations": [1, None, "https://example.com/a"]}),
    (200, {"choices": [{"message": None}], "search_results": "oops"}),
]


def _wrong_typed_responses() -> list[tuple[int, object]]:
    """结构是对的，但每个字段的类型都不对（数字 / 布尔 / 列表 / 对象）的响应。

    上游悄悄改了某个字段的类型时，适配器应当把它当空值处理，而不是在 .strip() 之类的地方报错。
    """
    import json as _json

    fields = (
        "url", "Url", "link", "title", "Title", "name", "content", "snippet", "description",
        "summary", "Summary", "Snippet", "text", "raw_content", "passage", "markdown",
        "highlights", "snippets", "excerpts", "SiteName", "site_name", "PublishTime",
        "publishedDate", "publishedTime", "published", "published_at", "published_date",
        "publish_date", "date", "datePublished", "page_age", "age", "message", "metadata",
    )
    responses: list[tuple[int, object]] = []
    for value in (7, 3.5, True, ["x", 1], {"a": 1}):
        item = {name: value for name in fields}
        listing = [item, dict(item, url="https://example.com/ok", Url="https://example.com/ok",
                              link="https://example.com/ok")]
        responses += [
            (200, {  # 列表直接挂在顶层的厂商
                "results": listing, "data": listing, "search_result": listing, "references": listing,
                "choices": listing, "citations": listing, "web": {"results": listing},
                "Result": {"WebResults": listing}, "Response": {"Pages": [_json.dumps(item), item]},
                "code": 0, "title": value, "content": value,
            }),
            (200, {  # 列表套在对象里的厂商
                "results": {"result": listing, "web": listing},
                "data": {"web": listing, "results": listing, "webPages": {"value": listing},
                         "markdown": value, "metadata": {"title": value}},
                "webPages": {"value": listing}, "code": 0, "success": True,
            }),
            (200, _rpc(result={
                "content": [{"type": "text", "text": _json.dumps({"results": listing, "pages": listing})}],
                "structuredContent": {"results": listing, "title": value, "content": value},
            })),
            (200, _rpc(result={"content": [{"type": "text", "text": value}, {"type": value, "text": "x"}]})),
            (200, _rpc(result={"content": [], "structuredContent": {"results": listing, "content": value}})),
        ]
    return responses


@case("真实适配器：19 家遇到各种异常响应，都只会正常返回或报「调用失败」，不会冒出程序错误")
async def t_adapters_survive_odd_responses():
    from .providers import parallel
    from .providers.base import FetchResult, ProviderError, SearchResult

    assert len(REAL) == 19, len(REAL)
    saved = parallel._handshake_required
    problems: list[str] = []
    calls = 0
    try:
        for index, (status, body) in enumerate(_ODD_RESPONSES + _wrong_typed_responses()):
            async with _mock_client(status, body) as client:
                for name, (search_fn, fetch_fn) in REAL.items():
                    for key in ("", _fake_key(name)):
                        jobs = [("search", search_fn, (client, key, "测试问题", 5))]
                        if fetch_fn:
                            jobs.append(("fetch", fetch_fn, (client, key, "https://example.com/a")))
                        for kind, fn, args in jobs:
                            calls += 1
                            try:
                                value = await fn(*args)
                            except ProviderError as exc:
                                if _fake_key(name) in str(exc):
                                    problems.append(f"{name}.{kind} 响应#{index}: 报错里带出了 key")
                                continue
                            except Exception as exc:
                                problems.append(
                                    f"{name}.{kind} 响应#{index}（HTTP {status}）: {type(exc).__name__}: {exc}"
                                )
                                continue
                            if kind == "search":
                                if not isinstance(value, list) or not all(
                                    isinstance(item, SearchResult) for item in value
                                ):
                                    problems.append(f"{name}.search 响应#{index}: 返回的不是结果列表")
                                elif not all(
                                    isinstance(item.url, str) and isinstance(item.title, str)
                                    and isinstance(item.snippet, str) and isinstance(item.published, str)
                                    for item in value
                                ):
                                    problems.append(f"{name}.search 响应#{index}: 结果里的字段不是文本")
                            elif not isinstance(value, FetchResult) or not isinstance(value.content, str):
                                problems.append(f"{name}.fetch 响应#{index}: 返回的不是抓取结果")
    finally:
        parallel._handshake_required = saved
    assert calls > 3000, calls
    assert not problems, f"共 {len(problems)} 处:\n  " + "\n  ".join(sorted(set(problems))[:40])


@case("真实适配器：各家的业务报错能换算成正确的状态（该换 key 的换 key，该冷却的冷却）")
async def t_adapters_error_mapping():
    from .providers.base import ProviderError

    async def failure(name: str, kind: str, status: int, body) -> ProviderError:
        fn = REAL[name][0 if kind == "search" else 1]
        args = ("测试问题", 5) if kind == "search" else ("https://example.com/a",)
        async with _mock_client(status, body) as client:
            try:
                await fn(client, _fake_key(name), *args)
            except ProviderError as exc:
                return exc
        raise AssertionError(f"{name}.{kind} 应当报错")

    def volc(code: str, message: str = "x") -> dict:
        return {"ResponseMetadata": {"Error": {"Code": code, "Message": message}}}

    # 火山引擎：明确的业务码（第二轮复审发现这里曾因漏导入而直接抛 NameError）
    for code, status in (("10406", 402), ("10408", 429), ("10403", 403), ("invalid_api_key", 401)):
        exc = await failure("volcengine", "search", 200, volc(code))
        assert exc.status == status and exc.retryable_with_other_key and not exc.soft, (code, exc.status)
    exc = await failure("volcengine", "search", 200, volc("99999", "failed to generate a moderate summary"))
    assert exc.status == 0 and not exc.retryable_with_other_key, exc.status      # 不认识的码不乱猜

    # Querit
    for code, status in ((10406, 402), (10408, 429), (10403, 403), (401, 401)):
        exc = await failure("querit", "search", 200, {"error_code": code, "error_msg": "x"})
        assert exc.status == status and exc.retryable_with_other_key, (code, exc.status)
    exc = await failure("querit", "search", 200, {"error_code": 500, "error_msg": "internal error"})
    assert exc.status == 0 and not exc.retryable_with_other_key

    # Firecrawl：搜索时按文案判断额度；抓取时不拿报错判断账号
    exc = await failure("firecrawl", "search", 200, {"success": False, "error": "Insufficient credits"})
    assert exc.status == 402 and exc.soft and exc.retryable_with_other_key
    exc = await failure("firecrawl", "fetch", 200, {"success": False, "error": "403 Forbidden: unauthorized"})
    assert exc.status == 0 and not exc.retryable_with_other_key

    # Parallel / Keenable：对方服务自己报的限流要认；抓取工具转述的目标网页报错不认
    limited = _rpc(error={"code": -32000, "message": "You have hit the free-tier rate limit"})
    blocked = _rpc(result={"isError": True, "content": [{"type": "text", "text": "401 Unauthorized, rate limit"}]})
    for name in ("parallel", "keenable"):
        assert (await failure(name, "search", 200, limited)).status == 429
        assert (await failure(name, "fetch", 200, limited)).status == 429
        assert (await failure(name, "search", 200, blocked)).status in (401, 429)
        exc = await failure(name, "fetch", 200, blocked)
        assert exc.status == 0 and not exc.retryable_with_other_key, (name, exc.status)

    # 其它用「HTTP 200 + 报错文案」的厂商
    assert (await failure("yuanbao", "search", 200, {"Response": {"Error": {"Code": "RequestLimitExceeded"}}})).status == 429
    assert (await failure("baidu", "search", 200, {"code": 1, "message": "QPS 超限，请求过于频繁"})).status == 429
    assert (await failure("bocha", "search", 200, {"code": 403, "msg": "余额不足"})).status == 402
    assert (await failure("zhipu", "search", 200, {"error": {"message": "Invalid API key"}})).status == 401
    assert (await failure("anysearch", "search", 200, {"code": 1, "message": "limit must be <= 10"})).status == 0

    # HTTP 层的状态码原样保留，并带上 Retry-After
    async with _mock_client(429, {"error": "slow down"}, {"Retry-After": "7"}) as client:
        try:
            await REAL["tavily"][0](client, _fake_key("tavily"), "测试问题", 5)
        except ProviderError as exc:
            assert exc.status == 429 and exc.retry_after == 7 and not exc.soft
        else:
            raise AssertionError("HTTP 429 应当报错")


@case("真实适配器：正常响应能解析出结果")
async def t_adapters_parse_normal():
    import json as _json

    page = {"url": "https://example.com/a", "title": "标题 A", "content": "摘要 A", "snippet": "摘要 A",
            "description": "摘要 A"}
    samples = {
        "tavily": {"results": [page]},
        "exa": {"results": [dict(page, text="摘要 A")]},
        "querit": {"error_code": 200, "results": {"result": [page]}},
        "volcengine": {"Result": {"WebResults": [{"Url": page["url"], "Title": "标题 A", "Summary": "摘要 A"}]}},
        "firecrawl": {"success": True, "data": {"web": [page]}},
        "jina": {"code": 200, "data": [page]},
        "anysearch": {"code": 0, "data": {"results": [page]}},
        "keenable": _rpc(result={"content": [], "structuredContent": {"results": [dict(page, snippets=["摘要 A"])]}}),
        "parallel": _rpc(result={"content": [{"type": "text", "text": _json.dumps(
            {"results": [dict(page, excerpts=["摘要 A"])]})}]}),
    }
    for name, body in samples.items():
        async with _mock_client(200, body) as client:
            results = await REAL[name][0](client, _fake_key(name), "测试问题", 5)
        assert len(results) == 1 and results[0].url == page["url"], (name, results)
        assert results[0].title == "标题 A" and results[0].source == name, (name, results[0])
        assert "摘要 A" in results[0].snippet, (name, results[0])


def run() -> int:
    from . import config

    expected = SANDBOX_ENV["TAVILY_API_KEYS"].split(",")
    if not os.environ.get("AGGREGATE_SEARCH_HOME") or config.API_KEYS.get("tavily") != expected:
        print("请运行 `aggregate-search selftest`：自测必须在它创建的临时沙盒里进行，以免动到你的真实配置。")
        return 2

    _install_fakes()
    failed = 0
    for name, fn in CASES:
        _reset()
        try:
            outcome = fn()
            if asyncio.iscoroutine(outcome):
                asyncio.run(outcome)
        except Exception:
            failed += 1
            print(f"[失败] {name}")
            traceback.print_exc(file=sys.stdout)
        else:
            print(f"[通过] {name}")
    passed = len(CASES) - failed
    print(f"\n共 {len(CASES)} 项，通过 {passed} 项" + (f"，失败 {failed} 项" if failed else "，全部通过"))
    return 1 if failed else 0
