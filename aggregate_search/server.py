"""聚合搜索 MCP Server（stdio 传输）。

对外工具：
- web_search: 匿名优先的多厂商聚合搜索，合并去重后返回带来源的证据
- web_fetch:  抓取网页正文，免费后端优先，失败自动换下一家
- web_status: 各家的 key 数量、冷却与用量、当前配置，以及近期使用记录

每家可配多个 key，运行期轮询；额度耗尽 / 限流 / key 失效会自动换下一个。
配置放在全局配置目录（默认 ~/.aggregate-search/），与代码装在哪里无关，见 README。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from . import __version__, usage
from .config import (
    ACTIVE_ENV_FILE,
    ACTIVE_KEYS_FILE,
    ANON_KEY_FALLBACK,
    AUTO_ESCALATE_MAX,
    CONFIG_DIR,
    DAILY_LIMITS,
    DEFAULT_MAX_RESULTS,
    ENV_FILE,
    ENV_IS_LEGACY,
    ESCALATE_COUNT,
    ESCALATE_EXCLUDE,
    FETCH_KEYED_FALLBACKS,
    FETCH_MIN_CHARS,
    FETCH_PREFERRED,
    FREE_PROVIDERS,
    KEYS_IS_LEGACY,
    MAX_RESULTS_CAP,
    MONTHLY_LIMITS,
    NEAR_DEDUP,
    PROXY_PROVIDERS,
    SEARCH_MODE,
    SEARCH_MODES,
    SEARCH_PROXY,
    SEARCH_PROXY_ALL,
    SEARCH_TIMEOUT,
    TOTAL_RESULTS,
    TOTAL_TIMEOUT,
    USAGE_LOG,
    USAGE_LOG_FILE,
    USE_SYSTEM_PROXY,
    WARNINGS,
    file_exists,
    log,
)
from .providers import (
    FETCH_PROVIDERS,
    KEYLESS_ENABLED,
    POOLS,
    REGISTRY,
    CallRecord,
    anon_cooling,
    auth_for,
    can_anonymous,
    enabled_search_providers,
    has_keys,
    pool_status,
    provider_enabled,
    redact,
    run_fetch,
    run_search,
    ssl_context,
)
from .providers.base import ProviderError, fold_near_duplicates, merge_results, scrub_urls
from .quality import assess_quality
from .routing import REGION_LABELS, RegionDecision, normalize_region, order_escalation

mcp = FastMCP("aggregate-search")
try:  # 让客户端看到的是本服务的版本号，而不是 MCP SDK 的版本号
    mcp._mcp_server.version = __version__
except AttributeError:
    pass

# 没配 key 的家在结果里提示一下，避免误以为「搜过了但没有」
_UNCONFIGURED_HINT = {
    "querit": "QUERIT_API_KEY",
    "brave": "BRAVE_API_KEY",
    "exa": "EXA_API_KEY",
    "jina": "JINA_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "volcengine": "VOLCENGINE_API_KEY",
    "searxng": "SEARXNG_BASE_URL（不需要 key，填实例地址即可）",
    "tavily": "TAVILY_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "duckduckgo": "无需 key，但需 ENABLE_KEYLESS=true 才默认参与",
    "parallel": "PARALLEL_API_KEY（可选；匿名也免费）",
    "anysearch": "ANYSEARCH_API_KEY（可选；匿名也免费）",
    "keenable": "KEENABLE_API_KEY（可选；匿名也免费）",
    "youcom": "YDC_API_KEY（可选；匿名免费档需代理时配置 SEARCH_PROXY）",
    "baidu": "BAIDU_API_KEY",
    "yuanbao": "TENCENTCLOUD_WSA_APIKEY",
    "zhipu": "ZHIPU_API_KEY",
    "bocha": "BOCHA_API_KEY",
}

# 补搜时有厂商失败或没搜到，最多再递补几轮（每轮只补缺的数量）
_ESCALATE_ROUNDS = 3
# 离总时限不到这么多秒，就不再开新一轮补搜
_MIN_ROUND_SECONDS = 3.0

# 抓取顺序里的两段：匿名可用的免费后端在前，带 key 的在后
_FETCH_FREE = ("parallel", "keenable")
_FETCH_KEYED = ("tavily", "jina", "firecrawl", "exa", "ollama", "parallel", "keenable")


# ---------------------------------------------------------------------------
# 同一问题的近期结果。escalate 模式靠它把补搜结果和之前的结果合并，并避开已经查过的厂商。
# 只在内存里，进程退出即清空。
#   {问题: {"results": {厂商: (结果列表, 写入时间)}, "tried": {厂商: {通道: 时间}}}}
# ---------------------------------------------------------------------------
_RECENT_TTL = 15 * 60
_RECENT_MAX = 32
_RECENT: OrderedDict[str, dict] = OrderedDict()
# 问题 -> [锁, 正在使用或等待它的调用数]
_SERIAL: dict[str, list] = {}


def _query_key(query: str) -> str:
    return " ".join(query.split()).casefold()


def _channels(record: CallRecord) -> set[str]:
    """这次调用碰过哪些通道。匿名被限流后改用 key 的，两个通道都算试过。"""
    return {record.auth, "anon"} if record.fallback else {record.auth}


def _mark_tried(tried: dict[str, set[str]], records: list[CallRecord]) -> None:
    for record in records:
        tried.setdefault(record.provider, set()).update(_channels(record))


def _recall(query: str) -> tuple[dict, dict[str, set[str]], float]:
    """取回同一问题还没过期的内容：(各家结果, 各家试过的通道, 最老那份结果的年龄秒数)。"""
    entry = _RECENT.get(_query_key(query))
    if not entry:
        return {}, {}, 0.0
    now = time.time()
    fresh = {
        name: (items, at)
        for name, (items, at) in entry["results"].items()
        if now - at <= _RECENT_TTL
    }
    tried: dict[str, set[str]] = {}
    for name, marks in entry["tried"].items():
        alive = {auth for auth, at in marks.items() if now - at <= _RECENT_TTL}
        if alive:
            tried[name] = alive
    age = max((now - at for _, at in fresh.values()), default=0.0)
    return {name: items for name, (items, _) in fresh.items()}, tried, age


def _remember(query: str, records: list[CallRecord]) -> None:
    """把这次新查到的结果和调用痕迹并入缓存。

    是合并而不是覆盖：同一个问题再跑一次 auto、或者点名查了某一家，
    都不会把「哪些厂商已经补过」忘掉，下一次 escalate 不会重复花同一批额度。
    """
    key = _query_key(query)
    now = time.time()
    entry = _RECENT.setdefault(key, {"results": {}, "tried": {}})
    entry["results"] = {
        name: value for name, value in entry["results"].items() if now - value[1] <= _RECENT_TTL
    }
    entry["tried"] = {
        name: alive
        for name, marks in entry["tried"].items()
        if (alive := {auth: at for auth, at in marks.items() if now - at <= _RECENT_TTL})
    }
    for record in records:
        # 只按实际发出的请求来记：没发出去的通道（key 冷却 / 到量、匿名通道冷却中）
        # 不算试过，过一会儿还可以再试
        marks = {}
        if record.anon_sent:
            marks["anon"] = now
        if record.keys_tried > 0:
            marks["key"] = now
        if not marks:
            continue
        entry["tried"].setdefault(record.provider, {}).update(marks)
        if record.ok:
            entry["results"][record.provider] = (record.results, now)
    _RECENT.move_to_end(key)
    while len(_RECENT) > _RECENT_MAX:
        _RECENT.popitem(last=False)


@asynccontextmanager
async def _serialized(query: str):
    """同一个问题的 escalate 排队执行。

    两个并发的 escalate 如果同时读缓存，会挑中同一批厂商、把额度花两遍；
    排队之后，后一个能看到前一个的结果，自动换下一批。
    """
    key = _query_key(query)
    slot = _SERIAL.setdefault(key, [asyncio.Lock(), 0])
    slot[1] += 1
    try:
        async with slot[0]:
            yield
    finally:
        slot[1] -= 1
        if not slot[1]:
            _SERIAL.pop(key, None)


# ---------------------------------------------------------------------------
# 补搜
# ---------------------------------------------------------------------------

def _escalation_order(query: str, region: str) -> tuple[list[str], RegionDecision]:
    """完整的补搜顺序：按地域排好的名单在前；名单里没写但已经启用的厂商
    （配了 key 的、填了地址的自建 SearXNG 等）排在最后，不会被漏掉；
    再去掉明确排除的（ESCALATE_EXCLUDE）。"""
    ordered, decision = order_escalation(query, region)
    ordered += [name for name in REGISTRY if name not in ordered and provider_enabled(name)]
    return [name for name in ordered if name not in ESCALATE_EXCLUDE], decision


def _escalation_plan(name: str, results: dict, tried: dict[str, set[str]]) -> str:
    """这家现在能不能拿来补搜，以及怎么调。

    返回 ''（不能）/ 'key'（带 key）/ 'anon'（匿名）/ 'anon+key'（先匿名，被限流再用 key）。
    已经有它的结果、已经试过的通道、冷却中或到了用量上限的 key，都不会再选。
    """
    if name not in REGISTRY or REGISTRY[name][0] is None or name in results:
        return ""
    seen = tried.get(name, ())
    key_ok = has_keys(name) and "key" not in seen and POOLS[name].usable() > 0
    anon_ok = name in KEYLESS_ENABLED and "anon" not in seen and not anon_cooling(name)
    if anon_ok and (name in FREE_PROVIDERS or not key_ok):
        # 声明为匿名源的，哪怕配了 key 也先走匿名；别的厂商只有 key 用不了时才退回匿名
        return "anon+key" if key_ok and ANON_KEY_FALLBACK else "anon"
    return "key" if key_ok else ""


def _escalation_ready(name: str, results: dict, tried: dict[str, set[str]]) -> bool:
    return bool(_escalation_plan(name, results, tried))


def _candidates(
    query: str, region: str, results: dict, tried: dict[str, set[str]]
) -> tuple[list[str], RegionDecision]:
    ordered, decision = _escalation_order(query, region)
    return [name for name in ordered if _escalation_plan(name, results, tried)], decision


async def _escalate(
    query: str,
    per_provider: int,
    region: str,
    target: int,
    results: dict,
    records: list[CallRecord],
    tried: dict[str, set[str]],
    deadline: float,
) -> tuple[list[str], RegionDecision, int, str]:
    """追加 target 个不同厂商，返回 (调用过的厂商, 地域判断, 拿到结果的家数, 提前停下的原因)。

    先过滤掉没配 key、冷却中、已到用量上限的厂商，再按地域顺序取前几家；
    有厂商失败或没搜到结果就从后面递补，尽量凑满 target 家有结果。
    两种情况会提前停下：快到总时限（'time'），或一整轮全是网络错误（'network'）。
    """
    queue, decision = _candidates(query, region, results, tried)
    picked: list[str] = []
    with_results = 0
    for _ in range(_ESCALATE_ROUNDS):
        batch = queue[: target - with_results]
        queue = queue[len(batch):]
        if not batch:
            break
        if deadline - time.monotonic() < _MIN_ROUND_SECONDS:
            return picked, decision, with_results, "time"
        plans = {name: _escalation_plan(name, results, tried) for name in batch}
        batch = [name for name in batch if plans[name]]  # 上一轮之后状态可能变了
        if not batch:
            continue
        got, _, recs = await run_search(
            batch,
            query,
            per_provider,
            anonymous={name for name in batch if plans[name].startswith("anon")},
            key_fallback={name for name in batch if plans[name] == "anon+key"},
            deadline=deadline,
        )
        results.update(got)
        records.extend(recs)
        _mark_tried(tried, recs)
        picked.extend(batch)
        # 返回 0 条不算数：响应格式变了的厂商也会表现为空结果，得继续往后补
        with_results += sum(1 for items in got.values() if items)
        if with_results >= target:
            break
        if all(record.network_failed for record in recs):
            return picked, decision, with_results, "network"  # 网络整体不通，再补也是白等
    return picked, decision, with_results, ""


def _cost_line(records: list[CallRecord]) -> str:
    """成本说明以实际发出的请求为准：哪几家真的带了 key、哪几家是匿名。"""
    keyed = list(dict.fromkeys(r.label for r in records if r.used_key))
    anon = list(dict.fromkeys(r.provider for r in records if r.sent and not r.used_key))
    if keyed:
        providers = {r.provider for r in records if r.used_key}
        text = f"- 成本: 本次动用了 **{len(providers)} 个厂商的账号 key**（{', '.join(keyed)}）"
        fell_back = list(dict.fromkeys(r.provider for r in records if r.fallback and r.used_key))
        if fell_back:
            text += f"，其中 {', '.join(fell_back)} 是匿名通道被限流后才改用 key"
        if anon:
            text += f"；匿名调用 {', '.join(anon)}"
        return text + "。"
    if anon:
        return f"- 成本: 本次全部为匿名调用（{', '.join(anon)}），**没有发送任何账号 key**。"
    return "- 成本: 本次没有发出任何请求。"


def _next_step(mode: str, query: str, region: str, requested: bool) -> str:
    """告诉 Agent：觉得证据不够时，下一步怎么只补几家而不是全量。

    按缓存里的状态来预测，所以和紧接着调用 escalate 时实际会选的厂商一致。
    """
    if mode == "free":
        return ""
    results, tried, _ = _recall(query)
    upcoming, decision = _candidates(query, region, results, tried)
    upcoming = upcoming[:ESCALATE_COUNT]
    if not upcoming:
        if mode == "all":
            return ""
        return "- 下一步: 已没有尚未查询且可用的厂商；若证据仍不足，请换一种问法或把问题拆开再搜。"
    plans = {name: _escalation_plan(name, results, tried) for name in upcoming}
    keyed = [name for name in upcoming if plans[name] == "key"]
    text = (
        '- 下一步: 若你阅读证据后认为仍不足以回答，用同一个 query 再调一次并传 search_mode="escalate"'
        + ("（这次不要带 providers）" if requested else "")
        + f"，会追加 {len(upcoming)} 个尚未查询的厂商（预计 {', '.join(upcoming)}）并与本次结果合并；"
        + (f"会用到 {', '.join(keyed)} 的账号额度。" if keyed else "都是匿名调用，不消耗账号额度。")
    )
    if region == "auto":
        text += (
            f"当前补搜偏好: {REGION_LABELS[decision.region]}；"
            '可传 region="domestic" / "global" / "mixed" 指定。'
        )
    return text


def _format_search(
    query: str,
    merged: list[dict],
    headline: str,
    audit: list[str],
    records: list[CallRecord],
    notes: dict[str, str],
    carried: dict[str, int],
) -> str:
    lines = [f"## 联网搜索统一汇总：{query}", "", headline]
    consensus = [item for item in merged if item["source_count"] >= 2]
    if consensus:
        lines.append("")
        lines.append("### 多源共识")
        for item in consensus[:5]:
            lines.append(
                f"- {item['title'] or item['url']}（{item['source_count']} 家："
                f"{', '.join(item['sources'])}）"
            )
    lines.append("")
    lines.extend(["### 质量门禁与成本审计", *audit, ""])
    lines.append("### 去重后的证据列表")
    if not merged:
        lines.append("（没有返回任何结果）")
    for i, item in enumerate(merged, 1):
        title = item["title"] or item["url"]
        lines.append(f"{i}. [{title}]({item['url']})")
        # 按来源展示证据，折叠后的摘要仍能追溯到具体 URL。
        evidence = []
        seen_evidence = set()
        for entry in item["evidence"]:
            key = (entry["source"], entry["url"], entry["snippet"], entry["published"])
            if key not in seen_evidence:
                seen_evidence.add(key)
                evidence.append(entry)
        for entry in evidence[:3]:
            snippet = entry["snippet"]
            suffix = "…[摘要截断]" if len(snippet) > 500 else ""
            date = f" ｜ {entry['published']}" if entry["published"] else ""
            lines.append(f"   - **{entry['source']}**{date}: {snippet[:500]}{suffix}")
            if entry["url"] != item["url"]:
                lines.append(f"     证据 URL: {entry['url']}")
        if len(evidence) > 3:
            lines.append(f"   - 另有 {len(evidence) - 3} 条同源/转载证据因单页展示预算省略")
        meta = "来源: " + ", ".join(item["sources"])
        if item["published_dates"]:
            dates = item["published_dates"]
            meta += " ｜ 发布: " + " / ".join(dates[:2])
            if len(dates) > 2:
                meta += f" 等 {len(dates)} 个日期"
        if item.get("alternate_urls"):
            meta += f" ｜ 近似转载 {len(item['alternate_urls'])} 条"
        lines.append(f"   {meta}")
        if item.get("alternate_urls"):
            lines.append("   转载链接: " + "；".join(item["alternate_urls"]))
        lines.append("")

    lines.append("### 提供商状态")
    for record in records:
        if not record.sent:
            lines.append(f"- {record.label}: 没有发出请求（{record.error}）")
            continue
        detail = f"{record.ms / 1000:.1f}s"
        if record.fallback:
            detail += "；匿名通道被限流，改用 key"
        if record.keys_tried > 1:
            detail += f"；试了 {record.keys_tried} 个 key"
        if record.ok:
            lines.append(f"- {record.label}: OK（{record.count} 条，{detail}）")
        else:
            lines.append(f"- {record.label}: 失败（{record.error}；{detail}）")
    lines.extend(f"- {name}: 沿用此前结果（{count} 条）" for name, count in carried.items())
    lines.extend(f"- {name}: 未调用（{msg}）" for name, msg in notes.items())

    statuses = pool_status()
    pool_lines = [
        f"- {name}: {statuses[name]}"
        for name in dict.fromkeys(r.provider for r in records if r.auth == "key")
        if name in statuses
    ]
    if pool_lines:
        lines.extend(["", "### key 状态", *pool_lines])
    return "\n".join(lines)


def _search_description() -> str:
    free = ", ".join(name for name in FREE_PROVIDERS if name in REGISTRY) or "（未配置）"
    auto_rule = (
        f"本地门禁判断覆盖明显不足时，才自动追加 {AUTO_ESCALATE_MAX} 个不同厂商"
        if AUTO_ESCALATE_MAX
        else "不会自动补搜，要不要补由你决定"
    )
    has_fallback = ANON_KEY_FALLBACK and any(
        has_keys(name) for name in FREE_PROVIDERS if can_anonymous(name)
    )
    anon_rule = (
        "不带账号 key（某家匿名通道被限流时才改用它的 key，并会在结果里注明）"
        if has_fallback
        else "不发送任何账号 key"
    )
    # 说明要跟着本机的默认模式走：默认被改成 all / escalate 时，不能还写「默认不消耗额度」
    default_note = {
        "auto": "默认先做匿名搜索，简单问题不消耗账号额度。",
        "free": "默认只做匿名搜索，不消耗账号额度。",
        "escalate": (
            "注意：本机把默认模式设成了 escalate，不传 search_mode 就会直接消耗账号额度；"
            '想省额度请显式传 search_mode="auto"。'
        ),
        "all": (
            "注意：本机把默认模式设成了 all，不传 search_mode 就会查询全部厂商、最费额度；"
            '想省额度请显式传 search_mode="auto"。'
        ),
    }[SEARCH_MODE]
    first = "先用默认的 auto" if SEARCH_MODE == "auto" else '先用 search_mode="auto"'

    def mark(mode: str) -> str:
        return f"{mode}（默认）" if mode == SEARCH_MODE else mode

    providers_rule = (
        '本机默认是 free，点名的厂商也只做匿名调用（要带 key 请显式传 search_mode="auto"）'
        if SEARCH_MODE == "free"
        else '配了 key 的会带 key，只想匿名请同时传 search_mode="free"'
    )

    return f"""联网搜索：多厂商聚合、去重，返回带来源的证据。{default_note}

推荐流程（省额度）：
1. {first}：匿名查询 {free}，{anon_rule}；{auto_rule}。
2. 阅读返回的证据，由你判断是否足以回答用户。
3. 不够时，用同一个 query 再调一次并传 search_mode="escalate"：追加 {ESCALATE_COUNT} 个尚未查询的不同厂商，
   与此前的结果合并去重后一起返回。可重复调用，每次换一批厂商。
4. 仍不够才用 all。

search_mode:
- {mark("auto")}: 匿名优先，覆盖不足才限量补搜
- {mark("free")}: 只做匿名调用，绝不发送账号 key
- {mark("escalate")}: 跳过匿名阶段，直接补 {ESCALATE_COUNT} 个不同厂商（消耗这些厂商的额度）；不能和 providers 一起用
- {mark("all")}: 查询全部已启用厂商（最费额度）

region（可选）只决定补搜时先用哪几家，不过滤任何结果：
- domestic: 中国大陆的政策、公司、产品、本地信息 → 优先国内搜索厂商
- global: 海外信息、国际技术文档 → 优先国际搜索厂商
- mixed: 中外对比，或两边都需要
- 不传: 按问题里的地名、机构名自动判断

Args:
    query: 搜索问题或关键词
    max_results: 每个厂商取几条，默认 {DEFAULT_MAX_RESULTS}
    providers: 只查指定厂商，如 ["tavily", "exa"]；{providers_rule}。可选 {', '.join(REGISTRY)}
    total_results: 最终返回多少个页面，默认 {TOTAL_RESULTS}
    search_mode: {' / '.join(SEARCH_MODES)}
    region: domestic / global / mixed，不传则自动判断
"""


@mcp.tool(description=_search_description())
async def web_search(
    query: str,
    max_results: int | None = None,
    providers: list[str] | None = None,
    total_results: int | None = None,
    search_mode: str | None = None,
    region: str | None = None,
) -> str:
    """联网搜索、质量门禁、去重和统一证据汇总。给模型看的说明见 _search_description()。"""
    started = time.monotonic()  # 总时限从这里算起：排队等同一问题的上一次 escalate 也算在内
    query = (query or "").strip()
    if not query:
        return "query 不能为空"
    mode = (search_mode or SEARCH_MODE).strip().lower()
    if mode not in SEARCH_MODES:
        return f"search_mode 仅支持 {' / '.join(SEARCH_MODES)}"
    region_choice = normalize_region(region)
    if region_choice is None:
        return "region 仅支持 domestic / global / mixed（不传则自动判断）"
    per_provider = min(
        max_results if max_results and max_results > 0 else DEFAULT_MAX_RESULTS,
        MAX_RESULTS_CAP,
    )
    limit = total_results if total_results and total_results > 0 else TOTAL_RESULTS
    # 去重且保序：["tavily","tavily"] 不该并发打两次、白耗两份额度
    requested = list(dict.fromkeys(
        p.strip().lower() for p in (providers or []) if isinstance(p, str) and p.strip()
    ))
    if mode == "escalate" and requested and not (search_mode or "").strip():
        mode = "auto"  # escalate 只是本机的默认值，调用方点了名，意思就是「查这几家」
    if mode == "escalate":
        if requested:
            # 两个一起传的话，以前会退化成「把点名的厂商带 key 重查一遍」，白花额度又不补搜
            return (
                'search_mode="escalate" 不能和 providers 一起用：escalate 会自己挑还没查过的厂商。'
                "去掉 providers 再调一次即可。"
            )
        async with _serialized(query):
            return await _search(query, mode, region_choice, requested, per_provider, limit, started)
    return await _search(query, mode, region_choice, requested, per_provider, limit, started)


async def _search(
    query: str,
    mode: str,
    region_choice: str,
    requested: list[str],
    per_provider: int,
    limit: int,
    started: float,
) -> str:
    deadline = started + TOTAL_TIMEOUT
    results: dict = {}
    records: list[CallRecord] = []
    tried: dict[str, set[str]] = {}
    notes: dict[str, str] = {}      # 没发起调用就被排除的厂商及原因
    carried: dict[str, int] = {}    # escalate 时沿用的此前结果：厂商 -> 条数
    carried_age = 0.0
    phases: list[tuple[str, list[str]]] = []

    # ---- 第一阶段：决定查哪几家，以及哪些必须匿名 ----
    plan: list[str] = []
    anonymous: set[str] = set()
    stage = ""
    if requested:
        stage = "指定厂商（匿名）" if mode == "free" else "指定厂商"
        for name in requested:
            if name not in REGISTRY:
                notes[name] = f"未知厂商，可选: {', '.join(REGISTRY)}"
            elif mode == "free":
                # free 的承诺是“不发送账号 key”，不是“配了 key 的厂商不能用”：
                # 能匿名调用的照常查，只是不带 key。
                if can_anonymous(name):
                    plan.append(name)
                    anonymous.add(name)
                else:
                    notes[name] = "free 模式只做匿名调用，这家必须带 key 才能用"
            elif provider_enabled(name, explicit=True):
                plan.append(name)
            else:
                hint = _UNCONFIGURED_HINT.get(name, f"{name.upper()}_API_KEY")
                notes[name] = f"未配置（需要 {hint}）"
    elif mode in ("auto", "free"):
        stage = "匿名免费"
        for name in FREE_PROVIDERS:
            if name not in REGISTRY:
                continue
            if can_anonymous(name):
                # 即使配了这家的 key，第一阶段也不带——key 留给补搜。
                plan.append(name)
                anonymous.add(name)
            elif mode == "auto" and provider_enabled(name):
                plan.append(name)  # 用户自己把必须带 key 的厂商排进了第一阶段
    elif mode == "all":
        stage = "全部已启用"
        plan = enabled_search_providers()

    def nothing_available() -> str:
        configured = ", ".join(enabled_search_providers()) or "无"
        detail = "；".join(f"{name}: {msg}" for name, msg in notes.items()) or "无"
        return f"没有可用的搜索提供商（已启用: {configured}）。原因: {detail}"

    # auto 且没点名时，第一阶段没有可查的厂商（比如 FREE_PROVIDERS 配错了）不算失败，继续走补搜
    if not plan and (requested or mode in ("free", "all")):
        return nothing_available()

    if plan:
        # auto：匿名通道被限流、而这家又配了 key 时，允许改用 key；free 和点名指定时不允许。
        fallback = anonymous if mode == "auto" and not requested and ANON_KEY_FALLBACK else ()
        got, _, recs = await run_search(plan, query, per_provider, anonymous, fallback, deadline)
        results.update(got)
        records.extend(recs)
        _mark_tried(tried, recs)
        phases.append((stage, list(plan)))

    def assess():
        items = merge_results(results)
        failures = {r.provider: r.error for r in records if not r.ok and r.provider not in results}
        return items, assess_quality(query, items, failures)

    # ---- 第二阶段：补搜 ----
    target = 0
    if mode == "escalate":
        prior, prior_tried, carried_age = _recall(query)
        results.update(prior)
        carried = {name: len(items) for name, items in prior.items()}
        for name, auths in prior_tried.items():
            tried.setdefault(name, set()).update(auths)
        target = ESCALATE_COUNT
    elif mode == "auto" and not requested and AUTO_ESCALATE_MAX > 0:
        _, first_pass = assess()
        if not first_pass.sufficient:
            target = AUTO_ESCALATE_MAX

    escalated: list[str] = []
    decision: RegionDecision | None = None
    with_results = 0
    stopped = ""
    if target:
        escalated, decision, with_results, stopped = await _escalate(
            query, per_provider, region_choice, target, results, records, tried, deadline
        )
        if escalated:
            phases.append(("补搜", escalated))

    if not records and not results:
        if stopped == "time":
            return (
                f"排队等同一问题的上一次补搜用掉了总时限（{TOTAL_TIMEOUT:g} 秒），这次没有发出任何请求。"
                "请再调用一次。"
            )
        if mode == "escalate":
            return (
                "现在没有可以补搜的厂商：还没查过的厂商要么没配 key，要么在冷却中 / 已到本地用量上限。"
                "可用 web_status 查看各家状态。"
            )
        return nothing_available()

    # ---- 汇总 ----
    unique_items, quality = assess()
    delivered_items, folded = (
        fold_near_duplicates(unique_items) if NEAR_DEDUP else (unique_items, 0)
    )
    merged = delivered_items[:limit]
    omitted = max(0, len(delivered_items) - len(merged))

    called = list(dict.fromkeys(r.provider for r in records if r.sent))
    with_hits = [name for name in called if results.get(name)]
    empty = [name for name in called if name in results and not results[name]]
    failed = [name for name in called if name not in results]
    raw = sum(len(items) for items in results.values())
    headline = (
        f"本次查询 **{len(called)} 家**，有效返回 **{len(with_hits)} 家**"
        + (f"，空结果 **{len(empty)} 家**" if empty else "")
        + (f"，失败 **{len(failed)} 家**" if failed else "")
        + (
            f"；并入 {max(1, round(carried_age / 60))} 分钟内同一问题的 **{len(carried)} 家**结果"
            if carried else ""
        )
        + f"；原始命中 **{raw} 条**，同 URL 归并后 **{len(unique_items)} 个页面**"
        + (f"，近似重复折叠 **{folded} 条**" if folded else "")
        + f"，以下交付 **{len(merged)} 个页面**"
        + (f"；另有 **{omitted} 个页面**因 total_results 上限未展示" if omitted else "")
        + "。"
    )

    audit = [quality.as_markdown()]
    if phases:
        audit.append(
            f"- 执行模式: **{mode}**；阶段: "
            + " → ".join(f"{name}({', '.join(names)})" for name, names in phases)
        )
    else:
        audit.append(f"- 执行模式: **{mode}**；本次没有新增调用。")
    if decision is not None:
        audit.append(f"- 补搜路由: {decision.describe()}")
    if target and with_results < target:
        why = {
            "time": f"已到总时限 {TOTAL_TIMEOUT:g} 秒，没有继续递补",
            "network": "这一轮全部是网络错误，没有继续递补",
        }.get(stopped, "其余厂商没配 key、在冷却中、已到本地用量上限、调用失败或没有搜到结果")
        audit.append(f"- 补搜数量: 目标 {target} 家，实际拿到结果 {with_results} 家（{why}）。")
    if mode == "escalate" and not carried:
        audit.append("- 说明: 没有找到同一问题的近期结果，本次只包含补搜厂商的结果。")
    audit.append(_cost_line(records))

    _remember(query, records)
    hint = _next_step(mode, query, region_choice, bool(requested))
    if hint:
        audit.append(hint)

    usage.log_event({
        "tool": "web_search",
        "query": redact(query)[:300],
        "mode": mode,
        "region": decision.region if decision else region_choice,
        "explicit": bool(requested),
        "calls": [record.as_log() for record in records],
        "escalated": escalated,
        "gate": "pass" if quality.sufficient else "fail",
        "unique": len(unique_items),
        "delivered": len(merged),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    })
    return _format_search(query, merged, headline, audit, records, notes, carried)


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def fetch_chain(include_unavailable: bool = False) -> list[tuple[str, bool]]:
    """没指定后端时的尝试顺序：[(后端, 是否强制匿名)]。

    1. WEB_FETCH_PROVIDER 指定的首选（用户明确要先用它）
    2. 匿名可用的免费后端
    3. 配了 key 且当前可用的后端

    默认模式设成 free 时只保留匿名后端：free 的承诺是「不发送账号 key」，抓取也得守住。
    include_unavailable=True 时不排除正在冷却 / 已到用量上限的 key（给工具说明用）。
    """
    free_only = SEARCH_MODE == "free"
    chain: list[tuple[str, bool]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, anonymous: bool) -> None:
        if name not in FETCH_PROVIDERS:
            return
        if anonymous and not can_anonymous(name):
            return
        mark = (name, auth_for(name, anonymous))
        if mark not in seen:
            seen.add(mark)
            chain.append((name, anonymous))

    for name in FETCH_PREFERRED:
        if provider_enabled(name, explicit=True):
            add(name, free_only)  # free 时首选也只走匿名；不能匿名的会被 add() 丢掉
    for name in _FETCH_FREE:
        add(name, True)
    if not free_only:
        for name in _FETCH_KEYED:
            if has_keys(name) and (include_unavailable or POOLS[name].usable() > 0):
                add(name, False)
    return chain


def _fetch_label(name: str, anonymous: bool) -> str:
    return f"{name}（{'匿名' if auth_for(name, anonymous) == 'anon' else 'key'}）"


def _fetch_description() -> str:
    order = " → ".join(
        _fetch_label(name, anon) for name, anon in fetch_chain(include_unavailable=True)
    ) or "（没有可用后端）"
    if SEARCH_MODE == "free":
        keyed_rule = "本机默认模式是 free，抓取只走匿名后端，不发送账号 key"
    elif FETCH_KEYED_FALLBACKS:
        keyed_rule = f"免费后端都抓不到时，最多再试 {FETCH_KEYED_FALLBACKS} 个带 key 的后端"
    else:
        keyed_rule = "不会自动换到其它带 key 的后端"
    provider_rule = (
        "只能指定可匿名使用的后端，不带 key" if SEARCH_MODE == "free" else "配了它的 key 就会带 key"
    )
    return f"""抓取网页正文，返回 Markdown / 纯文本。

不指定后端时按这个顺序尝试，抓到正文即停：{order}。
某个后端失败会自动换下一个；{keyed_rule}。正文过短（疑似反爬页或空壳）时会再换一个后端复核。
可选后端: {', '.join(FETCH_PROVIDERS)}。

Args:
    url: 要抓取的网页地址（http / https）
    provider: 可选，只用指定的后端（{provider_rule}）
"""


def _loggable_url(url: str) -> str:
    """写进使用记录的网址：去掉账号口令和 ? 后面的参数——预签名链接、token 之类不该落盘。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        text = f"{parts.scheme}://{host}{parts.path}" + ("?…" if parts.query else "")
    except ValueError:
        return "（无法解析的网址）"
    return redact(text)[:300]


@mcp.tool(description=_fetch_description())
async def web_fetch(url: str, provider: str | None = None) -> str:
    """抓取网页正文。给模型看的说明见 _fetch_description()。"""
    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return f"只支持 http/https 链接，收到: {url[:120]}"

    # 显式指定后端时只试这一家；未指定时按顺序回退，某家挂了不至于抓不到正文。
    explicit = (provider or "").strip().lower()
    if not explicit:
        chain = fetch_chain()
    elif SEARCH_MODE == "free":
        if explicit in FETCH_PROVIDERS and not can_anonymous(explicit):
            anonymous_backends = [name for name in FETCH_PROVIDERS if can_anonymous(name)]
            return (
                f"本机默认模式是 free（只做匿名调用），而 {explicit} 必须带 key 才能抓取。"
                f"可匿名使用的后端: {', '.join(anonymous_backends)}"
            )
        chain = [(explicit, True)]
    else:
        chain = [(explicit, False)]
    failures: list[str] = []
    attempts: list[dict] = []
    best = None
    successes = 0
    short = ""            # 第一个返回过短正文的后端
    rechecked = False     # 过短之后的那一次复核是否已经用掉
    keyed_failures = 0
    skipped: list[str] = []
    out_of_time = False
    for name, anonymous in chain:
        if short and rechecked:
            break
        label = _fetch_label(name, anonymous)
        keyed = auth_for(name, anonymous) == "key"
        # 用户用 WEB_FETCH_PROVIDER 点名的首选后端不受上限约束
        capped = keyed and not explicit and name not in FETCH_PREFERRED
        if capped and keyed_failures >= FETCH_KEYED_FALLBACKS:
            # 免费后端和几家带 key 的都抓不到，多半是地址本身有问题，不再继续消耗额度
            skipped.append(label)
            continue
        remaining = deadline - time.monotonic()
        if remaining < 2 and attempts:
            out_of_time = True
            break
        was_short = bool(short)
        tick = time.monotonic()
        result = None
        sent = True
        try:
            result = await asyncio.wait_for(
                run_fetch(name, url, anonymous), timeout=max(remaining, 2.0)
            )
            message = ""
        except asyncio.TimeoutError:
            message = f"超过总时限 {TOTAL_TIMEOUT:g} 秒，已放弃"
            out_of_time = True
        except ProviderError as exc:
            message = redact(str(exc))
            sent = exc.sent
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}")
            log(f"web_fetch 异常: {message}")
        cost = int((time.monotonic() - tick) * 1000)
        # 复核只有一次机会：抓到了算用掉；带 key 的请求发出去了（哪怕失败）也算用掉。
        # 匿名后端失败不花额度，不占这次机会。
        if was_short and sent and (result is not None or keyed):
            rechecked = True
        if result is None:
            if capped and sent:
                keyed_failures += 1
            failures.append(f"{label}: {message}")
            entry = {
                "p": name, "auth": auth_for(name, anonymous), "ok": False,
                # 后端的报错里常常带着被抓的网址（可能已被截断或脱敏过），落盘前去掉所有网址的参数
                "ms": cost, "err": scrub_urls(message)[:160],
            }
            if not sent:
                entry["sent"] = False
            attempts.append(entry)
            if out_of_time:
                break
            continue
        successes += 1
        size = len(result.content)
        attempts.append({"p": name, "auth": result.auth, "ok": True, "ms": cost, "chars": size})
        if best is None or size > len(best.content):
            best = result
        if explicit or size >= FETCH_MIN_CHARS:
            break
        # 正文过短：可能是反爬页、登录墙或空壳。换一家比一比。
        short = short or label

    usage.log_event({
        "tool": "web_fetch",
        "url": _loggable_url(url),
        "backend": best.provider if best else "",
        "auth": best.auth if best else "",
        "chars": len(best.content) if best else 0,
        "attempts": attempts,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    })
    if best is None:
        if not chain:
            return "没有可用的抓取后端。"
        text = "抓取失败，已尝试后端：\n- " + "\n- ".join(failures or ["（没有后端发出请求）"])
        if skipped:
            text += (
                f"\n\n为节省额度，没有继续尝试 {', '.join(skipped)}；"
                "确认地址无误后，可用 provider 参数指定其中一家再试。"
            )
        if out_of_time:
            text += f"\n\n已到总时限 {TOTAL_TIMEOUT:g} 秒，没有继续尝试剩下的后端。"
        return text

    prefix = f"抓取后端: {best.provider}（{'账号 key' if best.auth == 'key' else '匿名'}）\n\n"
    if failures:
        prefix += "已跳过失败后端: " + "；".join(failures) + "\n\n"
    if short:
        if len(best.content) >= FETCH_MIN_CHARS:
            prefix += f"提示: {short} 返回的正文过短，已换后端重抓。\n\n"
        elif successes > 1:
            prefix += (
                f"提示: 换了后端复核，正文仍只有 {len(best.content)} 字符——"
                "页面可能本身很短，也可能需要登录或有反爬。\n\n"
            )
        else:
            prefix += (
                f"提示: 正文只有 {len(best.content)} 字符，且没能用别的后端复核成功——"
                "页面可能本身很短，也可能需要登录或有反爬。可用 provider 参数指定后端再试。\n\n"
            )
    header = f"# {best.title}\n\n{best.url}\n\n" if best.title else f"{best.url}\n\n"
    return prefix + header + (best.content or "（正文为空）")


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def _limits_text() -> str:
    parts = [f"{name} 每日 {count} 次/key" for name, count in DAILY_LIMITS.items() if count]
    parts += [f"{name} 每月 {count} 次/key" for name, count in MONTHLY_LIMITS.items() if count]
    return "；".join(parts) or "未设置"


def status_text(usage_days: int | None = 0) -> str:
    lines = [f"## 聚合搜索状态（v{__version__}）", ""]
    if WARNINGS:
        lines.append("### 配置里有问题（已自动跳过，建议修正）")
        lines.extend(f"- {redact(message)}" for message in WARNINGS)
        lines.append("")

    lines.append("### 配置位置")
    lines.append(f"- 配置目录: {CONFIG_DIR}")
    if ENV_IS_LEGACY:
        lines.append(
            f"- 配置文件: {ACTIVE_ENV_FILE}（旧位置。运行 `aggregate-search init` 可迁移到 {ENV_FILE}）"
        )
    elif file_exists(ENV_FILE):
        lines.append(f"- 配置文件: {ENV_FILE}")
    else:
        lines.append(f"- 配置文件: 还没有。运行 `aggregate-search init` 会生成 {ENV_FILE}")
    if KEYS_IS_LEGACY:
        lines.append(f"- keys.json: {ACTIVE_KEYS_FILE}（旧位置，建议移到配置目录）")
    lines.append("")

    lines.append("### 搜索提供商")
    statuses = pool_status()
    stage_one = {name for name in FREE_PROVIDERS if can_anonymous(name)}
    for name in REGISTRY:
        anon_calls, _ = usage.today_counts(name)
        # 带 key 的用量已经在 key 状态里（今日已用 N），这里只补充匿名调用次数
        today = f"；今日匿名调用 {anon_calls} 次" if anon_calls else ""
        if provider_enabled(name):
            detail = statuses.get(name) or "匿名可用，无需 key"
            if name in stage_one and has_keys(name):
                detail += (
                    "；第一阶段匿名调用，匿名被限流、补搜或点名指定时才用 key"
                    if ANON_KEY_FALLBACK
                    else "；第一阶段匿名调用，key 只在补搜或点名指定时使用"
                )
            cooling = anon_cooling(name) if name in stage_one else 0
            if cooling:
                detail += f"；匿名通道限流冷却中，约 {cooling} 秒"
            lines.append(f"- {name}: 已启用（{detail}{today}）")
        elif can_anonymous(name):
            lines.append(
                f"- {name}: 未默认参与（可匿名调用：用 providers 点名，或设 ENABLE_KEYLESS=true{today}）"
            )
        else:
            hint = _UNCONFIGURED_HINT.get(name, f"{name.upper()}_API_KEY")
            lines.append(f"- {name}: 未配置（需要 {hint}{today}）")
    lines.append("")

    chain = fetch_chain()
    lines.append("### 抓取后端")
    lines.append(f"- 可选: {', '.join(FETCH_PROVIDERS)}")
    lines.append(
        "- 默认顺序: " + (" → ".join(_fetch_label(name, anon) for name, anon in chain) or "无")
    )
    if SEARCH_MODE == "free":
        lines.append("- 默认模式是 free：抓取只走匿名后端，不发送账号 key")
    elif FETCH_KEYED_FALLBACKS:
        lines.append(f"- 免费后端都抓不到时，最多再试 {FETCH_KEYED_FALLBACKS} 个带 key 的后端")
    else:
        lines.append("- 不会自动换到带 key 的后端（FETCH_KEYED_FALLBACKS=0）")
    lines.append("")

    ordered, _ = _escalation_order("", "auto")
    ready = [name for name in ordered if "key" in _escalation_plan(name, {}, {})]
    excluded = [name for name in sorted(ESCALATE_EXCLUDE) if has_keys(name)]
    lines.append("### 搜索成本模式")
    lines.append(f"- 默认模式: {SEARCH_MODE}")
    lines.append(
        "- 第一阶段（匿名，不带 key）: "
        + (", ".join(name for name in FREE_PROVIDERS if name in REGISTRY) or "无")
    )
    lines.append(
        f"- 当前可用于补搜的带 key 厂商: {', '.join(ready) or '无（没配 key，或都在冷却 / 已到用量上限）'}"
    )
    if excluded:
        lines.append(f"- 不参与自动补搜（仍可用 providers 点名）: {', '.join(excluded)}")
    lines.append(
        f"- auto 覆盖不足时自动补 {AUTO_ESCALATE_MAX} 家；escalate 每次补 {ESCALATE_COUNT} 家"
        if AUTO_ESCALATE_MAX
        else f"- auto 不自动补搜（AUTO_ESCALATE_MAX=0）；escalate 每次补 {ESCALATE_COUNT} 家"
    )
    lines.append(f"- 本地用量上限: {_limits_text()}")
    lines.append("")

    lines.append("### 网络")
    if SEARCH_PROXY:
        who = "全部厂商" if SEARCH_PROXY_ALL else (", ".join(sorted(PROXY_PROVIDERS)) or "无")
        lines.append(f"- SEARCH_PROXY: {redact(SEARCH_PROXY)}（用于 {who}）")
    else:
        lines.append("- SEARCH_PROXY: 未设置")
    if not (SEARCH_PROXY and SEARCH_PROXY_ALL):
        lines.append(
            "- 其余厂商: "
            + ("跟随系统的代理设置" if USE_SYSTEM_PROXY else "强制直连（USE_SYSTEM_PROXY=false）")
        )
    lines.append(f"- 单家超时 {SEARCH_TIMEOUT:g} 秒；一次搜索 / 抓取的总时限 {TOTAL_TIMEOUT:g} 秒")
    lines.append("")

    lines.append("### 使用记录")
    if not USAGE_LOG:
        lines.append("- 已关闭（USAGE_LOG=false）")
    else:
        lines.append(f"- 记录文件: {USAGE_LOG_FILE}（只在本机）")
        if usage_days and usage_days > 0:
            lines.extend(["", usage.summarize(usage_days)])
        else:
            lines.append("- 传 usage_days=7 可查看近 7 天各家调用次数、成功率和耗时")
    return "\n".join(lines)


@mcp.tool()
async def web_status(usage_days: int | None = None) -> str:
    """查看各搜索厂商的配置与 key 状态（数量、可用数、冷却、今日用量）、抓取顺序和配置文件位置。

    用来排查「为什么某家没出结果」：key 是否配了、是否在冷却中、是否到了本地用量上限。

    Args:
        usage_days: 大于 0 时附带近 N 天的使用记录汇总（各家调用次数、成功率、耗时、动用 key 的比例）
    """
    return status_text(usage_days)


def main() -> None:
    # Windows 管道默认 GBK，统一为 UTF-8 避免日志乱码（协议通道 stdout 用的是 buffer，不受影响）
    if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    enabled = enabled_search_providers()
    # 日志一律走 stderr，不会污染 stdio 协议通道
    log(f"v{__version__} 配置目录: {CONFIG_DIR}; 启用搜索提供商: {', '.join(enabled) or '无'}")
    legacy = [
        str(path)
        for path, is_legacy in ((ACTIVE_ENV_FILE, ENV_IS_LEGACY), (ACTIVE_KEYS_FILE, KEYS_IS_LEGACY))
        if is_legacy
    ]
    if legacy:
        log(
            f"正在读取旧位置的配置（{'、'.join(legacy)}）。"
            f"运行 aggregate-search init 可迁移到 {CONFIG_DIR}"
        )
    elif not file_exists(ENV_FILE):
        log(
            "还没有配置文件，目前只有匿名源可用。"
            f"运行 aggregate-search init 会生成 {ENV_FILE}"
        )
    # 证书加载约 0.8 秒：放到后台线程预热，既不拖慢握手，也不让第一次搜索多等
    threading.Thread(target=ssl_context, daemon=True).start()
    mcp.run()  # 默认 stdio


if __name__ == "__main__":
    main()
