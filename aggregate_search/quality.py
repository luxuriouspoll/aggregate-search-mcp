"""搜索结果质量门禁与免费优先的成本控制。

这里不声称能证明搜索结果“正确”。本地门禁只判断覆盖度、共识和明显缺口；
真正的语义审核由当前 Agent 阅读证据后完成。门禁不确定时宁可给出升级建议。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config
from .providers.base import is_official_url, normalize_url, numeric_terms

_STOPWORDS = {
    "什么", "如何", "怎么", "是否", "一个", "一下", "最新", "今天", "近期",
    "the", "what", "how", "is", "are", "and", "for", "with", "from", "that",
}


def is_complex_query(query: str) -> bool:
    lowered = query.casefold()
    if any(pattern.casefold() in lowered for pattern in config.get_settings().COMPLEX_QUERY_PATTERNS if pattern):
        return True
    # 多个并列对象、长问题或明确多步骤任务都需要更多独立证据。
    return len(query) > 80 or query.count("和") + query.count("与") + query.count(",") >= 2


def _query_terms(query: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}", query.casefold())
    terms: list[str] = []
    for word in words:
        if word in _STOPWORDS:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", word):
            # 中文没有空格，保留全词并补充二元片段用于宽松命中。
            terms.append(word)
            terms.extend(word[i:i + 2] for i in range(0, len(word) - 1, 2))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))


def _evidence_hits(title: str, snippet: str, terms: list[str], numbers: set[str]) -> int:
    text = (title + " " + snippet).casefold()
    # 带明确数字的查询不应仅靠「政策」「价格」等通用词通过；多个数字要求同份证据包含。
    text_hits = sum(term in text for term in terms)
    if (terms and not text_hits) or (numbers and not numbers <= numeric_terms(text)):
        return 0
    return len(numbers) + text_hits


def _relevant_items(query: str, items: list[dict]) -> list[dict]:
    terms = _query_terms(query)
    numbers = numeric_terms(query)
    relevant = []
    for item in items:
        evidence = []
        for entry in item.get("evidence") or [item]:
            if not (terms or numbers) or _evidence_hits(entry.get("title", ""), entry.get("snippet", ""), terms, numbers):
                evidence.append(entry)
        if evidence:
            relevant.append({**item, "evidence": evidence})
    return relevant


@dataclass
class QualityReport:
    sufficient: bool
    confidence: str
    reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    review_required: bool = True

    def as_markdown(self) -> str:
        status = "本地门禁通过" if self.sufficient else "建议补充搜索"
        lines = [
            f"- 覆盖度判断：**{status}**（{self.confidence}）",
            "- 指标: " + "；".join(f"{k}={v}" for k, v in self.metrics.items()),
        ]
        if self.reasons:
            lines.append("- 依据: " + "；".join(self.reasons))
        if self.gaps:
            lines.append("- 缺口: " + "；".join(self.gaps))
        lines.append("- 语义审核: 仍需当前 Agent 阅读下方证据；本地门禁不等于事实证明。")
        return "\n".join(lines)


def _effective_evidence_count(item: dict) -> int:
    # 同一相关 URL 的厂商数；跨 URL 转载和重复条目均不能抬高共识。
    sources_by_url: dict[str, set[str]] = {}
    for entry in item["evidence"]:
        url = normalize_url(entry.get("url", item.get("url", "")))
        if source := entry.get("source"):
            sources_by_url.setdefault(url, set()).add(source)
    return max(map(len, sources_by_url.values()), default=0)


def assess_quality(query: str, items: list[dict], errors: dict | None = None) -> QualityReport:
    settings = config.get_settings()
    errors = errors or {}
    relevant = _relevant_items(query, items)
    unique = len(relevant)
    counts = [_effective_evidence_count(item) for item in relevant]
    evidence_count = sum(counts)
    consensus = sum(count >= 2 for count in counts)
    strong_consensus = sum(count >= 3 for count in counts)
    sources = {entry.get("source") for item in relevant for entry in item["evidence"]} - {None, ""}
    official_hits = sum(any(is_official_url(entry.get("url", item.get("url", "")), settings.OFFICIAL_SOURCE_DOMAINS)
                            for entry in item["evidence"]) for item in relevant)
    complex_query = is_complex_query(query)
    metrics = {
        "相关独立页面": unique,
        "有效证据": evidence_count,
        "跨源共识页面": consensus,
        "三源共识页面": strong_consensus,
        "官方一手来源": official_hits,
        "有效厂商": len(sources),
        "复杂查询": "是" if complex_query else "否",
    }
    reasons: list[str] = []
    gaps: list[str] = []

    if unique < settings.AUTO_MIN_UNIQUE:
        gaps.append(f"相关独立页面不足（{unique}/{settings.AUTO_MIN_UNIQUE}）")
    if evidence_count < settings.AUTO_MIN_RESULTS:
        gaps.append(f"有效证据数量不足（{evidence_count}/{settings.AUTO_MIN_RESULTS}）")
    # AUTO_MIN_CONSENSUS=0 表示复杂问题也不要求多源共识
    if (
        complex_query
        and settings.AUTO_MIN_CONSENSUS > 0
        and strong_consensus < settings.AUTO_MIN_CONSENSUS
        and official_hits < 1
    ):
        gaps.append(f"复杂/时效性查询既无三源共识，也无官方一手来源（{strong_consensus} 个三源页面）")
    if not relevant and items:
        gaps.append("返回内容与查询关键词匹配度低")
    if not items:
        gaps.append("没有可审核的搜索结果")
    if errors and len(sources) <= 1:
        gaps.append("部分搜索源失败且缺少其他来源交叉验证")

    # 简单事实查询：有足够独立证据即可由 Agent 审核后交付，无需强求多源。
    # 复杂查询：必须有多源交叉验证；没有就限量升级带 key 的源。
    if not gaps:
        reasons.append("独立页面和证据数量达到门槛")
        if consensus:
            reasons.append(f"有 {consensus} 个页面获得多家来源交叉命中")
        if official_hits:
            reasons.append(f"有 {official_hits} 个官方一手来源，可作为三源共识的补充证据")
        return QualityReport(True, "覆盖度足够，待 Agent 语义复核", reasons, [], metrics)

    # 接近门槛但已有交叉证据时标为边界情况，由 Agent 决定是否值得花积分。
    borderline = unique >= 2 and evidence_count >= 2 and (consensus >= 1 or not complex_query)
    confidence = "边界结果，建议 Agent 审核后决定是否升级" if borderline else "覆盖不足，建议限量升级"
    return QualityReport(False, confidence, reasons, gaps, metrics)
