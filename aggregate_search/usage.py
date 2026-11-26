"""本地调用日志与用量上限，均位于本机配置目录。

- usage.jsonl：每次 web_search / web_fetch 一行（各家用量、耗时与条数）
- state.db：各 key 的当日 / 当月用量；带 key 的调用在发送前预占

上限是本机的保守保护，不能替代厂商控制台的额度告警。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

from . import config, store
from .config import USAGE_LOG_FILE, ensure_home, log

# 匿名调用没有 key，统一记在这个桶里：只统计，不设上限
ANON = "anon"
ACCOUNTING_UNAVAILABLE = "本地用量暂无法确认"

_LOCK = threading.RLock()
_WARNED: set[str] = set()


def _warn_once(tag: str, message: str) -> None:
    if tag not in _WARNED:
        _WARNED.add(tag)
        log(message)


# ---- 计数器 ----


def count_call(provider: str, fp: str) -> None:
    """尽力记录一次调用。匿名调用传 ANON；key 池在返回 key 前已经预占，不再调用此函数。"""
    day, month = store.periods()
    store.increment_counters([("day", day, provider, fp, 1), ("month", month, provider, fp, 1)])


def used(provider: str, fp: str) -> tuple[int, int]:
    """（今日已用，本月已用）。展示用的容错值，不用于预占。"""
    day, month = store.read_provider_state(provider, cooldowns=False).used(fp)
    return day or 0, month or 0


def limits_for(provider: str, *, reservation: bool = False) -> tuple[int, int]:
    """（每日上限，每月上限），0 表示不限。预占时兼顾热加载后收紧的额度。"""
    settings = config.get_settings()
    daily = settings.DAILY_LIMITS.get(provider, 0)
    monthly = settings.MONTHLY_LIMITS.get(provider, 0)
    if reservation:
        live = config.get_live_settings()
        if live.revision > settings.revision:
            live_daily = live.DAILY_LIMITS.get(provider, 0)
            live_monthly = live.MONTHLY_LIMITS.get(provider, 0)
            daily = min(daily, live_daily) if daily and live_daily else daily or live_daily
            monthly = min(monthly, live_monthly) if monthly and live_monthly else monthly or live_monthly
    return daily, monthly


def limit_reason(counts: tuple[int | None, int | None], limits: tuple[int, int]) -> str:
    daily, monthly = limits
    today, month = counts
    if not (daily or monthly):
        return ""
    if daily and today is not None and today >= daily:
        return f"今日已用 {today}/{daily}"
    if monthly and month is not None and month >= monthly:
        return f"本月已用 {month}/{monthly}"
    if today is None or month is None:
        return ACCOUNTING_UNAVAILABLE
    return ""


def limit_reached(provider: str, fp: str) -> str:
    """到量或无法可靠记账时返回原因；匿名调用不受上限限制。"""
    limits = limits_for(provider)
    if fp == ANON or not any(limits):
        return ""
    state = store.read_provider_state(provider, cooldowns=False)
    return limit_reason(state.used(fp), limits)


def today_counts(provider: str) -> tuple[int, int]:
    """（今日匿名请求数，今日带 key 请求数）。"""
    state = store.read_provider_state(provider, cooldowns=False)
    anon = state.daily.get(ANON) or 0
    return anon, sum(value or 0 for value in state.daily.values()) - anon


# ---- 使用记录 ----

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_event(event: dict) -> None:
    """追加一行使用记录。写失败不影响搜索。"""
    settings = config.get_settings()
    if not settings.USAGE_LOG:
        return
    line = json.dumps({"ts": now_iso(), **event}, ensure_ascii=False)
    with _LOCK:
        try:
            ensure_home()
            USAGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                too_big = USAGE_LOG_FILE.stat().st_size > settings.USAGE_LOG_MAX_MB * 1024 * 1024
            except OSError:
                too_big = False
            if too_big:
                # 只留一份旧文件，避免记录无限增长
                os.replace(USAGE_LOG_FILE, USAGE_LOG_FILE.with_name(USAGE_LOG_FILE.name + ".1"))
            with open(USAGE_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            _warn_once("log", f"使用记录写入失败: {exc}")


def read_events(days: int = 7) -> list[dict]:
    since = datetime.now().astimezone() - timedelta(days=max(1, days))
    events: list[dict] = []
    for path in (USAGE_LOG_FILE.with_name(USAGE_LOG_FILE.name + ".1"), USAGE_LOG_FILE):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
                stamp = datetime.fromisoformat(event["ts"])
                if stamp.tzinfo is None:
                    stamp = stamp.astimezone()  # 没带时区的按本地时间算
                recent = stamp >= since
            except (ValueError, KeyError, TypeError, OverflowError, OSError):
                continue
            if recent:
                events.append(event)
    return events


def _used_key(call: dict) -> bool:
    """这条调用记录是否真的发出过带 key 的请求。旧版记录没有 keys 字段，按用过算。"""
    return call.get("auth") == "key" and call.get("keys", 1) > 0


def summarize(days: int = 7, recent: int = 10) -> str:
    """把使用记录汇总成一段 Markdown：各家调用次数、成功率、耗时和补搜比例。"""
    events = read_events(days)
    searches = [e for e in events if e.get("tool") == "web_search"]
    fetches = [e for e in events if e.get("tool") == "web_fetch"]
    lines = [f"## 近 {days} 天使用记录", "", f"记录文件: {USAGE_LOG_FILE}", ""]
    if not events:
        lines.append("（还没有记录。搜索或抓取一次后再来看。）")
        return "\n".join(lines)

    if searches:
        for event in searches:
            calls = event.get("calls")
            event["calls"] = (
                [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []
            )
        auto_escalated = sum(1 for e in searches if e.get("mode") == "auto" and e.get("escalated"))
        asked_escalated = sum(1 for e in searches if e.get("mode") == "escalate" and e.get("escalated"))
        keyed = sum(1 for e in searches if any(_used_key(c) for c in e["calls"]))
        passed = sum(1 for e in searches if e.get("gate") == "pass")
        avg_ms = sum(e.get("elapsed_ms", 0) for e in searches) / len(searches)
        modes: dict[str, int] = {}
        for event in searches:
            modes[event.get("mode", "?")] = modes.get(event.get("mode", "?"), 0) + 1
        lines += [
            "### 搜索",
            f"- 共 {len(searches)} 次；模式分布: "
            + "，".join(f"{mode} {count}" for mode, count in sorted(modes.items())),
            f"- 动用过账号 key 的搜索: {keyed} 次（{keyed * 100 // len(searches)}%）；"
            f"其中 auto 自动补搜 {auto_escalated} 次，escalate 主动补搜 {asked_escalated} 次",
            f"- 本地门禁通过: {passed} 次（{passed * 100 // len(searches)}%）；"
            f"平均耗时 {avg_ms / 1000:.1f} 秒",
            "",
            "| 厂商 | 调用 | 匿名 | 带 key | 有结果 | 空结果 | 失败 | 平均耗时 | 平均条数 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        stats: dict[str, dict] = {}
        for event in searches:
            for call in event.get("calls", []):
                if call.get("sent") is False:
                    continue  # 没发出请求（key 冷却 / 到量），不计入调用次数
                row = stats.setdefault(call.get("p", "?"), {
                    "n": 0, "anon": 0, "key": 0, "hit": 0, "empty": 0, "fail": 0, "ms": 0, "items": 0,
                })
                row["n"] += 1
                row["key" if _used_key(call) else "anon"] += 1
                row["ms"] += call.get("ms", 0)
                if not call.get("ok"):
                    row["fail"] += 1
                elif call.get("n", 0):
                    row["hit"] += 1
                    row["items"] += call.get("n", 0)
                else:
                    row["empty"] += 1
        for name, row in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
            avg_items = row["items"] / row["hit"] if row["hit"] else 0
            lines.append(
                f"| {name} | {row['n']} | {row['anon']} | {row['key']} | {row['hit']} | "
                f"{row['empty']} | {row['fail']} | {row['ms'] / row['n'] / 1000:.1f}s | {avg_items:.1f} |"
            )
        lines.append("")

    if fetches:
        backends: dict[str, int] = {}
        for event in fetches:
            name = event.get("backend") or "失败"
            backends[name] = backends.get(name, 0) + 1
        lines += [
            "### 抓取",
            f"- 共 {len(fetches)} 次；最终成功的后端: "
            + "，".join(f"{name} {count}" for name, count in sorted(backends.items(), key=lambda kv: -kv[1])),
            "",
        ]

    if searches and recent > 0:
        lines.append(f"### 最近 {min(recent, len(searches))} 次搜索")
        for event in searches[-recent:]:
            used_keys = [c.get("p", "?") for c in event["calls"] if _used_key(c)]
            lines.append(
                f"- {event.get('ts', '')[:19]} ｜ {event.get('mode', '?')} ｜ "
                f"{event.get('elapsed_ms', 0) / 1000:.1f}s ｜ "
                + ("key: " + ",".join(used_keys) if used_keys else "零 key")
                + f" ｜ {str(event.get('query', ''))[:60]}"
            )
    return "\n".join(lines)


def _metric(value) -> int | float | None:
    # 排除 bool、NaN、无穷和坏日志中的离谱数值。
    return value if type(value) in (int, float) and 0 <= value < (1 << 63) else None


def provider_stats(days: int = 7) -> dict[str, dict]:
    """只汇总已发送的搜索。真正空结果既不奖励也不惩罚，`filtered` 不改写原始 `n`。

    `calls` 包含 `empty`；`hit_rate`/`failure_rate` 只以命中和失败为分母。
    保留旧 `ok`（命中次数）、`fail`、`ms`（总耗时）、`ok_rate`；后者同 `hit_rate`。
    `avg_ms` 只使用命中且耗时合法的样本；`latency_samples` 为 0 时 `avg_ms` 为 0。
    """
    stats: dict[str, dict] = {}
    for event in read_events(days):
        if event.get("tool") != "web_search":
            continue
        calls = event.get("calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict) or call.get("sent") is False:
                continue
            name = str(call.get("p") or "")
            if not name:
                continue
            row = stats.setdefault(name, {
                "calls": 0, "ok": 0, "empty": 0, "fail": 0, "ms": 0,
                "hit_ms": 0, "latency_samples": 0,
            })
            row["calls"] += 1
            elapsed = _metric(call.get("ms"))
            if elapsed is not None:
                row["ms"] += elapsed
            if call.get("ok") is not True:
                row["fail"] += 1
            elif (_metric(call.get("n")) or 0) > 0:
                row["ok"] += 1
                if elapsed is not None:
                    row["hit_ms"] += elapsed
                    row["latency_samples"] += 1
            else:
                row["empty"] += 1
    for row in stats.values():
        decisive = row["ok"] + row["fail"]
        row["ok_rate"] = row["hit_rate"] = row["ok"] / decisive if decisive else 0.0
        row["failure"] = row["fail"]
        row["failure_rate"] = row["fail"] / decisive if decisive else 0.0
        samples = row["latency_samples"]
        hit_ms = row.pop("hit_ms")
        row["avg_ms"] = hit_ms / samples if samples else 0.0
    return stats
