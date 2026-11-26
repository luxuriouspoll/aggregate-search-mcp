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
    DOMESTIC_PROVIDERS,
    DOMESTIC_QUERY_TERMS,
    GLOBAL_PROVIDERS,
    GLOBAL_QUERY_TERMS,
    PAID_PROVIDERS,
    REGION_ROUTING,
)

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


def order_escalation(query: str, region: str = "auto") -> tuple[list[str], RegionDecision]:
    """按地域偏好排出完整的补源顺序（保序去重）。

    这里只管排序，不截断、也不判断厂商是否可用——哪些厂商配了 key、
    有没有在冷却或到量，由调用方过滤之后再取前几家，这样才能凑满数量。
    """
    decision = detect_region(query, region)
    domestic = list(DOMESTIC_PROVIDERS)
    international = list(GLOBAL_PROVIDERS)
    generic = list(PAID_PROVIDERS)

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
