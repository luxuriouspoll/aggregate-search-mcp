"""聚合搜索 MCP Server（stdio 传输）。

对外工具：
- web_search: 并发调用所有已启用的搜索提供商，合并去重后返回 Markdown
- web_fetch:  抓取网页正文（默认 Tavily，可切 firecrawl / ollama / exa / jina）
- web_status: 查看各家的 key 数量、可用状态与冷却情况

每家可配多个 key，运行期轮询；额度耗尽 / 限流 / key 失效会自动换下一个。
密钥与开关全部走环境变量、.env 或 keys.json，见 README。
"""
from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from config import (
    AUTO_ESCALATE_MAX,
    DEFAULT_FETCH_PROVIDER,
    DEFAULT_MAX_RESULTS,
    FREE_PROVIDERS,
    MAX_RESULTS_CAP,
    NEAR_DEDUP,
    PAID_PROVIDERS,
    SEARCH_MODE,
    AUTO_ESCALATE_MAX,
    TOTAL_RESULTS,
)
from providers import (
    FETCH_PROVIDERS,
    POOLS,
    REGISTRY,
    enabled_search_providers,
    pool_status,
    provider_enabled,
    run_fetch,
    run_search,
)
from providers.base import ProviderError, fold_near_duplicates, merge_results
from quality import assess_quality
from routing import escalation_providers

mcp = FastMCP("aggregate-search")

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


def _format_search(
    query: str,
    merged: list[dict],
    results: dict,
    errors: dict,
    stats: dict,
) -> str:
    lines = [f"## 联网搜索统一汇总：{query}", ""]
    lines.append(
        f"已查询 **{stats['attempted']} 家**，有效返回 **{stats['succeeded']} 家**"
        + (f"，空结果 **{stats['empty']} 家**" if stats["empty"] else "")
        + (f"，失败 **{len(errors)} 家**" if errors else "")
        + f"；原始命中 **{stats['raw']} 条**，同 URL 归并后 **{stats['unique']} 个页面**"
        + (f"，近似重复折叠 **{stats['folded']} 条**" if stats["folded"] else "")
        + f"，以下交付 **{len(merged)} 个页面**"
        + (f"；另有 **{stats['omitted_pages']} 个页面**因 total_results 上限未展示" if stats["omitted_pages"] else "")
        + "。"
    )
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
    if stats.get("quality"):
        lines.extend(["### 质量门禁与成本审计", stats["quality"], ""])
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
    lines.extend(f"- {name}: OK（{len(items)} 条）" for name, items in results.items())
    lines.extend(f"- {name}: 失败（{msg}）" for name, msg in errors.items())

    statuses = pool_status()
    pool_lines = [
        f"- {name}: {line}"
        for name, line in statuses.items()
        if name in results or name in errors
    ]
    if pool_lines:
        lines.extend(["", "### key 状态", *pool_lines])
    return "\n".join(lines)


@mcp.tool()
async def web_search(
    query: str,
    max_results: int | None = None,
    providers: list[str] | None = None,
    total_results: int | None = None,
    search_mode: str = "",
) -> str:
    """联网搜索、质量门禁、去重和统一证据汇总。

    默认 auto：先查 Parallel/Keenable/AnySearch 匿名源，不消耗 key；
    覆盖不足时最多补查 2 个已配置的 key 源，再统一交付给当前 Agent 语义审核。
    free：仅匿名源，保证不消耗账号积分。
    all：查询全部已启用厂商，保持完整聚合行为。

    Args:
        query: 搜索关键词
        max_results: 每家抓取条数上限，默认 5
        providers: 显式指定厂商；此时按指定厂商执行，不再自动筛选
        total_results: 最终交付的页面数，默认 12
        search_mode: auto（默认）/ free / all
    """
    mode = (search_mode or SEARCH_MODE).strip().lower()
    if mode not in ("auto", "free", "all"):
        return "search_mode 仅支持 auto / free / all"
    per_provider = min(
        max_results if max_results and max_results > 0 else DEFAULT_MAX_RESULTS,
        MAX_RESULTS_CAP,
    )
    # 去重且保序：["tavily","tavily"] 不该并发打两次、白耗两份额度
    requested = list(dict.fromkeys(p.strip().lower() for p in (providers or []) if p.strip()))
    errors: dict[str, str] = {}
    if requested:
        names = []
        for name in requested:
            if name not in REGISTRY:
                errors[name] = f"未知提供商，可选: {', '.join(REGISTRY)}"
            elif mode == "free" and POOLS.get(name) and POOLS[name].keys:
                errors[name] = "free 模式禁止使用带 key 的厂商，避免消耗账号积分"
            elif not provider_enabled(name, explicit=True):
                hint = _UNCONFIGURED_HINT.get(name, f"{name.upper()}_API_KEY")
                errors[name] = f"未配置（需要 {hint}）"
            else:
                names.append(name)
    elif mode in ("auto", "free"):
        names = [name for name in FREE_PROVIDERS if provider_enabled(name, explicit=True)]
    else:
        names = enabled_search_providers()
    if not names:
        configured = ", ".join(enabled_search_providers()) or "无"
        return f"没有可用的搜索提供商（已配置的: {configured}）。错误: {errors or '无'}"

    results, run_errors = await run_search(names, query, per_provider)
    errors.update(run_errors)
    unique_items = merge_results(results)
    quality = assess_quality(query, unique_items, errors)
    phases = [{
        "stage": "匿名免费" if not requested and mode in ("auto", "free") else "指定/全量",
        "providers": list(names),
    }]
    escalated: list[str] = []
    region_reason = ""

    # 只有 auto 且门禁不足才升级；free 模式永远不调用带 key 的厂商。
    if mode == "auto" and not requested and not quality.sufficient and AUTO_ESCALATE_MAX > 0:
        candidates, decision = escalation_providers(query, names, AUTO_ESCALATE_MAX)
        candidates = [name for name in candidates if provider_enabled(name)]
        region_reason = decision.reason
        if decision.matched_terms:
            region_reason += "（" + "、".join(decision.matched_terms) + "）"
        if candidates:
            extra_results, extra_errors = await run_search(candidates, query, per_provider)
            errors.update(extra_errors)
            results.update(extra_results)
            names.extend(candidates)
            escalated = candidates
            phases.append({"stage": "限量积分/免费额度", "providers": candidates})
            unique_items = merge_results(results)
            quality = assess_quality(query, unique_items, errors)

    delivered_items, folded = (
        fold_near_duplicates(unique_items) if NEAR_DEDUP else (unique_items, 0)
    )
    limit = total_results if total_results and total_results > 0 else TOTAL_RESULTS
    merged = delivered_items[:limit]
    stats = {
        "attempted": len(names),
        "succeeded": sum(1 for items in results.values() if items),
        "empty": sum(1 for items in results.values() if not items),
        "raw": sum(len(items) for items in results.values()),
        "unique": len(unique_items),
        "folded": folded,
        "omitted_pages": max(0, len(delivered_items) - len(merged)),
        "quality": quality.as_markdown()
        + f"\n- 执行模式: **{mode}**；阶段: "
        + " → ".join(f"{p['stage']}({', '.join(p['providers'])})" for p in phases)
        + (f"\n- 补源路由: {region_reason}" if region_reason else "")
        + (
            f"\n- 成本: 第一阶段匿名源不足，追加 {len(escalated)} 个 key 源；未启用其余厂商。"
            if escalated else
            "\n- 成本: 仅使用匿名/免费源，本次未消耗任何账号 key 积分。"
            if mode in ("auto", "free") and not requested else
            f"\n- 成本: 按 {mode}/指定厂商执行；带 key 的厂商可能消耗账户额度。"
        ),
    }
    return _format_search(query, merged, results, errors, stats)


@mcp.tool()
async def web_fetch(url: str, provider: str = "") -> str:
    """抓取网页正文。后端可选：tavily（extract）/ firecrawl（markdown）/ ollama（web_fetch）/
    exa（contents）/ jina（reader），默认取环境变量 WEB_FETCH_PROVIDER（未设置时为 tavily）。

    Args:
        url: 要抓取的网页地址
        provider: 可选，临时指定抓取后端
    """
    name = (provider or DEFAULT_FETCH_PROVIDER).strip().lower()
    if not url.strip().lower().startswith(("http://", "https://")):
        return f"只支持 http/https 链接，收到: {url[:120]}"

    # 显式指定后端时只试这一家；未指定时按顺序回退，某家挂了不至于抓不到正文。
    if provider.strip():
        candidates = [name]
    else:
        candidates = [name] + [p for p in FETCH_PROVIDERS if p != name and provider_enabled(p, explicit=True)]
    failures: list[str] = []
    for candidate in candidates:
        try:
            result = await run_fetch(candidate, url)
        except ProviderError as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        except Exception as exc:
            print(f"[aggregate-search] web_fetch 异常: {type(exc).__name__}: {exc}", file=sys.stderr)
            failures.append(f"{candidate}: {type(exc).__name__}: {exc}")
            continue
        header = f"# {result.title}\n\n{result.url}\n\n" if result.title else f"{result.url}\n\n"
        prefix = f"抓取后端: {result.provider}\n\n"
        if failures:
            prefix += "已跳过失败后端: " + "；".join(failures) + "\n\n"
        return prefix + header + (result.content or "（正文为空）")
    return "抓取失败，已尝试后端：\n- " + "\n- ".join(failures)


@mcp.tool()
async def web_status() -> str:
    """查看各搜索提供商的配置与 key 状态（数量、可用数、冷却中数量）。

    用来排查「为什么某家没出结果」：key 是否配了、是否都在冷却中。
    """
    lines = ["## 聚合搜索状态", ""]
    lines.append("### 搜索提供商")
    statuses = pool_status()
    for name in REGISTRY:
        enabled = provider_enabled(name)
        pool = statuses.get(name, "")
        if enabled:
            detail = pool or "免 key"
            lines.append(f"- {name}: 已启用（{detail}）")
        else:
            hint = _UNCONFIGURED_HINT.get(name, f"{name.upper()}_API_KEY")
            lines.append(f"- {name}: 未配置（需要 {hint}）")
    lines.append("")
    lines.append(f"### 抓取后端\n- 可选: {', '.join(FETCH_PROVIDERS)}\n- 默认: {DEFAULT_FETCH_PROVIDER}")
    lines.append(
        f"\n### 搜索成本模式\n- 默认: {SEARCH_MODE}\n"
        f"- 匿名优先池: {', '.join(FREE_PROVIDERS)}\n"
        f"- 自动升级顺序: {', '.join(PAID_PROVIDERS)}（最多 {AUTO_ESCALATE_MAX} 家）\n"
        "- auto: 匿名源先查，覆盖不足才限量启用 key；free: 禁止 key；all: 全量查询"
    )
    return "\n".join(lines)


def main() -> None:
    # Windows 控制台/管道默认 GBK，统一为 UTF-8 避免日志乱码（stdio 用的是 buffer，不受影响）
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    enabled = enabled_search_providers()
    # 打到 stderr，不会污染 stdio 协议通道
    print(
        f"[aggregate-search] 启用搜索提供商: {', '.join(enabled) or '无'}; "
        f"抓取后端: {DEFAULT_FETCH_PROVIDER}",
        file=sys.stderr,
    )
    mcp.run()  # 默认 stdio


if __name__ == "__main__":
    main()
