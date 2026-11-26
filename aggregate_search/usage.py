"""本地使用记录与用量上限。全部只落在本机配置目录，不进仓库。

- usage.jsonl          每次 web_search / web_fetch 一行：查询、模式、各家耗时与条数
- usage-counters.json  每家每个 key 当日 / 当月已发出的请求数，用于本地上限

上限的意义：部分厂商（如百度千帆）超出免费额度后会按量计费。
这里按“发出的请求数”保守计数；多个进程同时搜索时可能有个位数误差，
所以默认值留了余量。它是本地软保护，不能替代厂商控制台里的额度告警。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

from .config import (
    DAILY_LIMITS,
    MONTHLY_LIMITS,
    USAGE_COUNTER_FILE,
    USAGE_LOG,
    USAGE_LOG_FILE,
    USAGE_LOG_MAX_MB,
    ensure_home,
    log,
)

# 匿名调用没有 key，统一记在这个桶里：只统计，不设上限
ANON = "anon"

_LOCK = threading.RLock()
_CACHE: dict | None = None
_CACHE_MTIME: float = -1.0
_WARNED: set[str] = set()


def _warn_once(tag: str, message: str) -> None:
    if tag not in _WARNED:
        _WARNED.add(tag)
        log(message)


def _periods() -> tuple[str, str]:
    now = time.localtime()
    return time.strftime("%Y-%m-%d", now), time.strftime("%Y-%m", now)


# ---- 计数器 ----

def _file_mtime() -> float:
    try:
        return USAGE_COUNTER_FILE.stat().st_mtime
    except OSError:
        return -1.0


def _load() -> dict:
    """{"day": {"2026-11-25": {厂商: {key 指纹: 次数}}}, "month": {"2026-11": {...}}}"""
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        mtime = _file_mtime()
        if _CACHE is not None and mtime == _CACHE_MTIME:
            return _CACHE
        try:
            raw = json.loads(USAGE_COUNTER_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        data: dict = {"day": {}, "month": {}}
        if isinstance(raw, dict):
            for scope in ("day", "month"):
                periods = raw.get(scope)
                if not isinstance(periods, dict):
                    continue
                for period, providers in periods.items():
                    if not isinstance(providers, dict):
                        continue
                    clean: dict[str, dict[str, int]] = {}
                    for provider, bucket in providers.items():
                        if isinstance(bucket, dict):
                            clean[str(provider)] = {
                                str(fp): number
                                for fp, count in bucket.items()
                                if (number := _as_count(count)) is not None
                            }
                    data[scope][str(period)] = clean
        _CACHE = data
        _CACHE_MTIME = mtime
        return data


def _as_count(value) -> int | None:
    """计数文件被手工改坏（NaN、无穷大、字符串）时丢弃那一项，而不是让整个服务报错。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return max(0, int(value))
    except (ValueError, OverflowError):
        return None


def _save(data: dict) -> None:
    global _CACHE, _CACHE_MTIME
    day, month = _periods()
    # 只保留当天和当月，文件不会越写越大
    data["day"] = {day: data["day"].get(day, {})}
    data["month"] = {month: data["month"].get(month, {})}
    _CACHE = data
    try:
        ensure_home()
        USAGE_COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件名带进程号，多个进程同时写时不会互相踩
        tmp = USAGE_COUNTER_FILE.with_name(f"{USAGE_COUNTER_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, USAGE_COUNTER_FILE)
        _CACHE_MTIME = _file_mtime()
    except OSError as exc:
        _warn_once("counter", f"用量计数写入失败（本地上限在本次运行内仍生效）: {exc}")


def count_call(provider: str, fp: str) -> None:
    """记一次发出的请求。fp 是 key 指纹；匿名调用传 ANON。"""
    day, month = _periods()
    with _LOCK:
        data = _load()
        for scope, period in (("day", day), ("month", month)):
            bucket = data[scope].setdefault(period, {}).setdefault(provider, {})
            bucket[fp] = bucket.get(fp, 0) + 1
        _save(data)


def used(provider: str, fp: str) -> tuple[int, int]:
    """（今日已用，本月已用）。"""
    day, month = _periods()
    data = _load()
    return (
        data["day"].get(day, {}).get(provider, {}).get(fp, 0),
        data["month"].get(month, {}).get(provider, {}).get(fp, 0),
    )


def limits_for(provider: str) -> tuple[int, int]:
    """（每日上限，每月上限），0 表示不限。"""
    return DAILY_LIMITS.get(provider, 0), MONTHLY_LIMITS.get(provider, 0)


def limit_reached(provider: str, fp: str) -> str:
    """到量返回原因，否则返回空串。"""
    daily, monthly = limits_for(provider)
    if not daily and not monthly:
        return ""
    today, month = used(provider, fp)
    if daily and today >= daily:
        return f"今日已用 {today}/{daily}"
    if monthly and month >= monthly:
        return f"本月已用 {month}/{monthly}"
    return ""


def today_counts(provider: str) -> tuple[int, int]:
    """（今日匿名请求数，今日带 key 请求数）。"""
    day, _ = _periods()
    bucket = _load()["day"].get(day, {}).get(provider, {})
    anon = bucket.get(ANON, 0)
    return anon, sum(bucket.values()) - anon


# ---- 使用记录 ----

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_event(event: dict) -> None:
    """追加一行使用记录。写失败不影响搜索。"""
    if not USAGE_LOG:
        return
    line = json.dumps({"ts": now_iso(), **event}, ensure_ascii=False)
    with _LOCK:
        try:
            ensure_home()
            USAGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                too_big = USAGE_LOG_FILE.stat().st_size > USAGE_LOG_MAX_MB * 1024 * 1024
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
