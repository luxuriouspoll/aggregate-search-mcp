"""查询地域信号与补源顺序。

地域分类只影响第二阶段补源，不限制匿名源，也不删除任何地区的搜索结果。
中文查询不自动等于国内信息；只有明确的地域实体才产生偏好。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from config import (
    DOMESTIC_PROVIDERS,
    DOMESTIC_QUERY_TERMS,
    GLOBAL_PROVIDERS,
    GLOBAL_QUERY_TERMS,
    PAID_PROVIDERS,
    REGION_ROUTING,
)


@dataclass(frozen=True)
class RegionDecision:
    region: str
    matched_terms: tuple[str, ...]
    reason: str


def detect_region(query: str) -> RegionDecision:
    if REGION_ROUTING in ("cn", "domestic"):
        return RegionDecision("domestic", (), "用户手动指定国内补源")
    if REGION_ROUTING in ("global", "international"):
        return RegionDecision("global", (), "用户手动指定国际补源")
    if REGION_ROUTING in ("off", "false", "0", "none"):
        return RegionDecision("auto", (), "地区路由已关闭")

    # 词边界适用于英文；中文实体直接包含匹配。长词优先，避免“美国”被泛词覆盖。
    domestic = sorted(
        (term for term in DOMESTIC_QUERY_TERMS if term and term.casefold() in query.casefold()),
        key=len,
        reverse=True,
    )
    global_terms = sorted(
        (
            term for term in GLOBAL_QUERY_TERMS
            if term and (
                term.casefold() in query.casefold()
                if any("\u4e00" <= ch <= "\u9fff" for ch in term)
                else re.search(rf"(?<![a-zA-Z0-9_]){re.escape(term)}(?![a-zA-Z0-9_])", query, re.I)
            )
        ),
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


def escalation_providers(query: str, already_used: list[str], limit: int) -> tuple[list[str], RegionDecision]:
    decision = detect_region(query)
    used = set(already_used)
    domestic = [name for name in DOMESTIC_PROVIDERS if name not in used]
    international = [name for name in GLOBAL_PROVIDERS if name not in used]
    generic = [name for name in PAID_PROVIDERS if name not in used]

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

    # 保序去重，且每一项都是不同厂商；同厂商多个 key 在 provider 内部处理。
    selected = list(dict.fromkeys(name for name in ordered if name not in used))[:limit]
    return selected, decision
