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

from config import DEFAULT_FETCH_PROVIDER, DEFAULT_MAX_RESULTS, MAX_RESULTS_CAP
from providers import (
    FETCH_PROVIDERS,
    REGISTRY,
    enabled_search_providers,
    pool_status,
    provider_enabled,
    run_fetch,
    run_search,
)
from providers.base import ProviderError, merge_results

mcp = FastMCP("aggregate-search")

# 聚合结果总量上限：每家各限 max_results，去重合并后再整体截到这里（倍数）
_MERGE_MULTIPLIER = 2

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
    "serper": "SERPER_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
    "linkup": "LINKUP_API_KEY",
}


def _format_search(query: str, merged: list[dict], results: dict, errors: dict) -> str:
    lines = [f"## 搜索结果：{query}", ""]
    if not merged:
        lines.append("（没有返回任何结果）")
    for i, item in enumerate(merged, 1):
        title = item["title"] or item["url"]
        lines.append(f"{i}. [{title}]({item['url']})")
        if item["snippet"]:
            lines.append(f"   {item['snippet']}")
        meta = "来源: " + ", ".join(item["sources"])
        if item["published"]:
            meta += f" ｜ 发布: {item['published']}"
        lines.append(f"   {meta}")
        lines.append("")
    lines.append("### 提供商状态")
    lines.extend(f"- {name}: OK（{len(items)} 条）" for name, items in results.items())
    lines.extend(f"- {name}: 失败（{msg}）" for name, msg in errors.items())

    # key 池状态：只在有 key 的家上显示，便于发现额度问题
    pool_lines = []
    for name, line in pool_status().items():
        if name in results or name in errors:
            pool_lines.append(f"- {name}: {line}")
    if pool_lines:
        lines.append("")
        lines.append("### key 状态")
        lines.extend(pool_lines)
    return "\n".join(lines)


@mcp.tool()
async def web_search(
    query: str,
    max_results: int | None = None,
    providers: list[str] | None = None,
) -> str:
    """联网搜索（聚合）：并发调用所有已启用的搜索提供商（Tavily/Querit/Firecrawl/Ollama/
    Brave/Exa/Jina/Perplexity/火山引擎/DuckDuckGo/SearXNG），合并去重后返回 Markdown 结果列表，
    同一 URL 聚合标记全部来源。单家失败不影响整体；同一家配了多个 key 会自动轮询。

    Args:
        query: 搜索关键词
        max_results: 每家返回条数上限，默认 5；合并去重后总条数约不超过它的 2 倍
        providers: 可选，限定本次使用的提供商子集，如 ["tavily", "brave"]
    """
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
            elif not provider_enabled(name, explicit=True):
                hint = _UNCONFIGURED_HINT.get(name, f"{name.upper()}_API_KEY")
                errors[name] = f"未配置（需要 {hint}）"
            else:
                names.append(name)
    else:
        names = enabled_search_providers()
    if not names:
        configured = ", ".join(enabled_search_providers()) or "无"
        return f"没有可用的搜索提供商（已配置的: {configured}）。错误: {errors or '无'}"

    results, run_errors = await run_search(names, query, per_provider)
    errors.update(run_errors)
    merged = merge_results(results)[: per_provider * _MERGE_MULTIPLIER]
    return _format_search(query, merged, results, errors)


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
