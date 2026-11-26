"""查询地域信号与补源顺序。

地域分类只影响第二阶段补源，不限制匿名源，也不删除任何地区的搜索结果。
中文查询不自动等于国内信息；只有明确的地域实体才产生偏好。

判断来源的优先级：
    1. 调用方（Agent）传的 region 参数 —— 它读得懂问题，最准
    2. 环境变量 REGION_ROUTING=cn/global/off —— 用户的固定偏好
    3. 问题里的地名、机构名关键词 —— 兜底
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import (
    ADAPTIVE_BAD_RATE,
    ADAPTIVE_GOOD_RATE,
    ADAPTIVE_MIN_CALLS,
    ADAPTIVE_ROUTING,
    CASE_SENSITIVE_QUERY_TERMS,
    DOMESTIC_PROVIDERS,
    DOMESTIC_QUERY_TERMS,
    GLOBAL_PROVIDERS,
    GLOBAL_QUERY_TERMS,
    PAID_PROVIDERS,
    REGION_ROUTING,
    UNVERIFIED_PROVIDERS,
)

def _load_stats() -> dict[str, dict]:
    """读近 7 天的厂商表现。读失败就当没有数据（路由退回静态顺序，不影响可用性）。"""
    try:
        from . import usage
        return usage.provider_stats(7)
    except Exception:
        return {}


# 易歧义短词后面跟这些内容时，才算在说这门技术
_TECH_SUFFIX = r"(?=\s*(?:\d|语言|编程|开发者|并发|泛型|接口|goroutine|module|test|语言))"

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
    if term.casefold() in CASE_SENSITIVE_QUERY_TERMS:
        return re.search(pattern + _TECH_SUFFIX, query) is not None
    return re.search(pattern, query, re.I) is not None


def detect_region(query: str, override: str = "auto") -> RegionDecision:
    if override in ("domestic", "global", "mixed"):
        return RegionDecision(
            override, (), f"调用方指定 region={override}，按{REGION_LABELS[override]}顺序补源"
        )
    if REGION_ROUTING in ("cn", "domestic"):
        return RegionDecision("domestic", (), "REGION_ROUTING 固定为国内补源")
    if REGION_ROUTING in ("global", "international"):
        return RegionDecision("global", (), "REGION_ROUTING 固定为国际补源")
    if REGION_ROUTING in ("off", "false", "0", "none"):
        return RegionDecision("auto", (), "地区路由已关闭，使用通用补源顺序")

    # 词边界适用于英文；中文实体直接包含匹配。长词优先，避免“美国”被泛词覆盖。
    domestic = sorted(
        (term for term in DOMESTIC_QUERY_TERMS if term and term.casefold() in query.casefold()),
        key=len,
        reverse=True,
    )
    global_terms = sorted(
        (term for term in GLOBAL_QUERY_TERMS if term and _matches(term, query)),
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


def _verified_first(names: list[str]) -> list[str]:
    """已实测的厂商排在前面，未实测的排后面（保持各自的相对顺序）。

    未实测的厂商并不是不能用——只是作者没真实调用过，稳定性未知，
    所以放在后面：国内/国际的优先顺序不变，只是同一组内先给验证过的机会。
    """
    verified = [name for name in names if name not in UNVERIFIED_PROVIDERS]
    unverified = [name for name in names if name in UNVERIFIED_PROVIDERS]
    return verified + unverified


def _adaptive_order(names: list[str], stats: dict[str, dict]) -> list[str]:
    """在**同一组内部**按近 7 天表现做轻微调权，返回组内重排后的顺序。

    防死锁的几条硬约束：
    - 只有累计真实调用 >= ADAPTIVE_MIN_CALLS 的厂商才参与调权；
    - 每次最多把一家往后挪一位，绝不把任何厂商剔出名单；
    - 成功率低的只是排到同组末尾（下次仍会被尝试），成功率高且调用多的略提前；
    - 完全没有统计数据的厂商保持原位，不因为「没见过」就被压到后面。
    """
    if not names:
        return names
    qualified = {
        name: row for name, row in stats.items()
        if row.get("calls", 0) >= ADAPTIVE_MIN_CALLS and name in names
    }
    if not qualified:
        return names

    def tier(name: str) -> int:
        row = qualified.get(name)
        if row is None:
            return 1                                    # 没数据：保持中间，不被惩罚
        if row["ok_rate"] < ADAPTIVE_BAD_RATE:
            return 2                                    # 表现差：同组末尾
        if row["ok_rate"] >= ADAPTIVE_GOOD_RATE:
            return 0                                    # 表现好：同组靠前
        return 1

    # 稳定排序：只按档次分，档次内保持原相对顺序，最多相当于挪一位
    return sorted(names, key=tier)


def order_escalation(query: str, region: str = "auto") -> tuple[list[str], RegionDecision]:
    """按地域偏好排出完整的补源顺序（保序去重）。

    这里只管排序，不截断、也不判断厂商是否可用——哪些厂商配了 key、
    有没有在冷却或到量，由调用方过滤之后再取前几家，这样才能凑满数量。
    """
    decision = detect_region(query, region)
    # 组内先给实测过的厂商机会，未实测的往后排（不跨组，不打乱国内/国际优先级）
    domestic = _verified_first(list(DOMESTIC_PROVIDERS))
    international = _verified_first(list(GLOBAL_PROVIDERS))
    generic = _verified_first(list(PAID_PROVIDERS))
    if ADAPTIVE_ROUTING:
        # 自适应调权只在各自组内做，绝不跨区
        stats = _load_stats()
        domestic = _adaptive_order(domestic, stats)
        international = _adaptive_order(international, stats)
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
