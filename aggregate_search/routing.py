"""查询地域信号与补源顺序。

地域分类只影响第二阶段补源，不限制匿名源，也不删除任何地区的搜索结果。
中文查询不自动等于国内信息；只有明确的地域实体才产生偏好。

判断来源的优先级：
    1. 调用方（Agent）传的 region 参数 —— 它读得懂问题，最准
    2. 环境变量 REGION_ROUTING=cn/global/off —— 用户的固定偏好
    3. 问题里的地名、机构名关键词 —— 兜底
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import config


def _load_stats() -> dict[str, dict]:
    """读近 7 天的厂商表现。读失败就当没有数据（路由退回静态顺序，不影响可用性）。"""
    try:
        from . import usage
        return usage.provider_stats(7)
    except Exception:
        return {}


# 易歧义短词后面跟这些内容时，才算在说这门技术
_TECH_SUFFIX = r"(?=\s*(?:\d|语言|编程|开发者|并发|泛型|接口|goroutine|module|test))"

_REGION_ALIASES: dict[str, str] = {
    "": "auto", "auto": "auto", "自动": "auto",
    "domestic": "domestic", "cn": "domestic", "china": "domestic", "国内": "domestic", "中国": "domestic",
    "global": "global", "international": "global", "intl": "global", "overseas": "global",
    "国外": "global", "国际": "global", "海外": "global",
    "mixed": "mixed", "both": "mixed", "混合": "mixed", "国内外": "mixed",
}

REGION_LABELS: dict[str, str] = {
    "domestic": "国内", "global": "国际", "mixed": "国内外混合", "auto": "通用",
}


def normalize_region(value: str | None) -> str | None:
    """把调用方传的 region 归一成 auto / domestic / global / mixed；认不出来返回 None。"""
    return _REGION_ALIASES.get((value or "").strip().casefold())


@dataclass(frozen=True)
class RegionDecision:
    region: str
    matched_terms: tuple[str, ...]
    reason: str

    def describe(self) -> str:
        if self.matched_terms:
            return f"{self.reason}（{'、'.join(self.matched_terms)}）"
        return self.reason


def _matches(term: str, query: str) -> bool:
    """这个地域词是否真的出现在 query 里。

    中文直接包含匹配；英文要求词边界。对 "Go" 这类既可能是编程语言、也可能是日常动词的
    短词额外收紧：必须区分大小写，且后面紧跟版本号或语言名（"Go 1.24"、"Go 语言"），
    这样 "how to go back" 不会被误判成国际技术查询。
    """
    if any("\u4e00" <= ch <= "\u9fff" for ch in term):
        return term.casefold() in query.casefold()
    pattern = rf"(?<![a-zA-Z0-9_]){re.escape(term)}(?![a-zA-Z0-9_])"
    if term.casefold() in config.CASE_SENSITIVE_QUERY_TERMS:
        return re.search(pattern + _TECH_SUFFIX, query) is not None
    return re.search(pattern, query, re.I) is not None


def detect_region(query: str, override: str = "auto") -> RegionDecision:
    if override in ("domestic", "global", "mixed"):
        return RegionDecision(
            override, (), f"调用方指定 region={override}，按{REGION_LABELS[override]}顺序补源"
        )
    settings = config.get_settings()
    if settings.REGION_ROUTING in ("cn", "domestic"):
        return RegionDecision("domestic", (), "REGION_ROUTING 固定为国内补源")
    if settings.REGION_ROUTING in ("global", "international"):
        return RegionDecision("global", (), "REGION_ROUTING 固定为国际补源")
    if settings.REGION_ROUTING in ("off", "false", "0", "none"):
        return RegionDecision("auto", (), "地区路由已关闭，使用通用补源顺序")

    # 词边界适用于英文；中文实体直接包含匹配。长词优先，避免“美国”被泛词覆盖。
    lowered = query.casefold()
    domestic = sorted(
        (term for term in settings.DOMESTIC_QUERY_TERMS if term and term.casefold() in lowered),
        key=len,
        reverse=True,
    )
    global_terms = sorted(
        (term for term in settings.GLOBAL_QUERY_TERMS if term and _matches(term, query)),
        key=len,
        reverse=True,
    )
    if domestic and not global_terms:
        return RegionDecision("domestic", tuple(domestic[:4]), "识别到国内实体，仅调整补源顺序")
    if global_terms and not domestic:
        return RegionDecision("global", tuple(global_terms[:4]), "识别到国际实体，仅调整补源顺序")
    if domestic and global_terms:
        return RegionDecision(
            "mixed",
            tuple((domestic + global_terms)[:4]),
            "同时包含国内与国际实体，补源混排，不做硬分流",
        )
    return RegionDecision("auto", (), "无明确地域实体，使用通用补源顺序")


def _verified_first(names: list[str], unverified: frozenset[str]) -> list[str]:
    """已实测的厂商排在前面，未实测的排后面，各自保持静态顺序。"""
    return sorted(names, key=unverified.__contains__)


@dataclass(frozen=True)
class _Performance:
    tier: int
    hit_rate: float
    avg_ms: float | None


def _performance(row: dict, settings: config.RuntimeSettings) -> _Performance | None:
    # 真实空结果既不是命中也不是失败；全空或证据不足的厂商不参与调序。
    if row.get("calls", 0) - row.get("empty", 0) < settings.ADAPTIVE_MIN_CALLS:
        return None
    hit_rate = row.get("hit_rate", row.get("ok_rate"))
    if not isinstance(hit_rate, (int, float)) or not 0 <= hit_rate <= 1:
        return None
    tier = (2 if hit_rate < settings.ADAPTIVE_BAD_RATE else
            0 if hit_rate >= settings.ADAPTIVE_GOOD_RATE else 1)
    avg_ms = row.get("avg_ms")
    if (row.get("latency_samples", 0) < settings.ADAPTIVE_MIN_CALLS
            or not isinstance(avg_ms, (int, float)) or not math.isfinite(avg_ms) or avg_ms < 0):
        avg_ms = None
    return _Performance(tier, hit_rate, avg_ms)


def _prefer(right: _Performance, left: _Performance) -> bool:
    if right.tier != left.tier:
        return right.tier < left.tier
    # 同可靠性档内先尊重明显的命中率差距；只有接近时才拿延迟做次级信号。
    if abs(right.hit_rate - left.hit_rate) >= 0.05:
        return right.hit_rate > left.hit_rate
    if right.avg_ms is None or left.avg_ms is None:
        return False
    return left.avg_ms - right.avg_ms >= max(100.0, 0.2 * left.avg_ms)


def _adaptive_order(names: list[str], stats: dict[str, dict]) -> list[str]:
    """同地域、同验证档内只做一轮相邻换位；小/未知样本保持原位。"""
    settings = config.get_settings()
    domestic, international = set(settings.DOMESTIC_PROVIDERS), set(settings.GLOBAL_PROVIDERS)
    unverified = settings.UNVERIFIED_PROVIDERS

    def group(name: str) -> tuple[bool, bool, bool]:
        return name in domestic, name in international, name in unverified

    ordered = list(names)
    perf = {name: _performance(stats.get(name, {}), settings) for name in ordered}
    i = 0
    while i + 1 < len(ordered):
        left, right = ordered[i:i + 2]
        a, b = perf[left], perf[right]
        if a is not None and b is not None and group(left) == group(right) and _prefer(b, a):
            ordered[i], ordered[i + 1] = right, left
            i += 2  # 已换位的两家都不能再挪
        else:
            i += 1
    return ordered


def order_escalation(query: str, region: str = "auto") -> tuple[list[str], RegionDecision]:
    """按地域偏好排出完整的补源顺序（保序去重）。

    这里只管排序，不截断、也不判断厂商是否可用——哪些厂商配了 key、
    有没有在冷却或到量，由调用方过滤之后再取前几家，这样才能凑满数量。
    """
    decision = detect_region(query, region)
    settings = config.get_settings()
    unverified = settings.UNVERIFIED_PROVIDERS
    # 组内先给实测过的厂商机会，未实测的往后排（不跨组，不打乱国内/国际优先级）
    domestic = _verified_first(list(settings.DOMESTIC_PROVIDERS), unverified)
    international = _verified_first(list(settings.GLOBAL_PROVIDERS), unverified)
    generic = _verified_first(list(settings.PAID_PROVIDERS), unverified)
    stats = _load_stats() if settings.ADAPTIVE_ROUTING else {}
    if stats and decision.region != "auto":
        domestic = _adaptive_order(domestic, stats)
        international = _adaptive_order(international, stats)
    elif stats:
        generic = _adaptive_order(generic, stats)

    if decision.region == "domestic":
        ordered = domestic + international + generic
    elif decision.region == "global":
        ordered = international + domestic + generic
    elif decision.region == "mixed":
        # 交叉混排，避免一边完全没有权威来源。
        ordered = []
        for pair in zip(domestic, international):
            ordered.extend(pair)
        ordered += domestic[len(international):] + international[len(domestic):] + generic
    else:
        ordered = generic

    # 每一项都是不同厂商；同厂商多个 key 在 provider 内部轮转。
    return list(dict.fromkeys(ordered)), decision
