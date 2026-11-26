"""冷却和用量存在本地 SQLite（WAL），只记录 key 指纹。

冷却逐行更新；带 key 的调用在同一写事务内检查状态和预占额度。
数据库不可用时，有本地上限的 key 不发送；匿名调用的记账只尽力而为。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from pathlib import Path

from .config import CONFIG_DIR, KEY_STATE_FILE, USAGE_COUNTER_FILE, ensure_home, log

DB_FILE: Path = CONFIG_DIR / "state.db"
LEGACY_KEY_STATE = KEY_STATE_FILE
LEGACY_COUNTERS = USAGE_COUNTER_FILE

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
# 成功迁移或没有旧文件的表；失败会暂缓该表的新写入，下一次再重试。
_LEGACY_READY: set[str] = set()
_MAX_COUNT = (1 << 63) - 1  # SQLite INTEGER 上界，超过后加法会变成 REAL

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS key_state (
        provider TEXT NOT NULL, fingerprint TEXT NOT NULL, code TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '', reset_at REAL NOT NULL, PRIMARY KEY (provider, fingerprint)
    )""",
    """CREATE TABLE IF NOT EXISTS usage_counters (
        scope TEXT NOT NULL, period TEXT NOT NULL, provider TEXT NOT NULL, fingerprint TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scope, period, provider, fingerprint)
    )""",
    "CREATE TABLE IF NOT EXISTS legacy_migrations (name TEXT NOT NULL PRIMARY KEY)",
)
_INSERT_COOLDOWN = "INSERT INTO key_state (provider, fingerprint, code, message, reset_at) VALUES (?, ?, ?, ?, ?)"
_INSERT_COUNTER = "INSERT INTO usage_counters (scope, period, provider, fingerprint, count) VALUES (?, ?, ?, ?, ?)"
_INCREMENT_COUNTER = _INSERT_COUNTER + """
    ON CONFLICT(scope, period, provider, fingerprint) DO UPDATE SET count = count + excluded.count
    WHERE typeof(count) = 'integer' AND count >= 0
        AND typeof(count + excluded.count) = 'integer' AND count + excluded.count >= 0
"""
_CURRENT_PERIODS = "((scope = 'day' AND period = ?) OR (scope = 'month' AND period = ?))"


def _connect() -> sqlite3.Connection | None:
    """取连接（同进程复用）。首次升级之外不为建表获取写锁。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            return _CONN
        conn = None
        try:
            ensure_home()
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0, check_same_thread=False)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() != "wal":
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode.lower() != "wal":
                    raise sqlite3.OperationalError("无法启用 WAL")
            # 预占提交后就会花费上游额度，写 WAL 时也要保证提交的持久性。
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                        for statement in _SCHEMA:
                            conn.execute(statement)
                        conn.execute("PRAGMA user_version=1")
            _CONN = conn
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            log(f"状态数据库不可用（无法记录冷却和用量）：{exc}")
        return _CONN


def reset_connection() -> None:
    """关掉当前连接（测试里换数据库文件时用）。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except sqlite3.Error:
                pass
        _CONN = None
        _LEGACY_READY.clear()


def periods(now: float | None = None) -> tuple[str, str]:
    local = time.localtime(now)
    return time.strftime("%Y-%m-%d", local), time.strftime("%Y-%m", local)


# ---------------------------------------------------------------- 迁移


def migrate_legacy() -> bool:
    """导入旧 JSON 并改名留档。返回本次实际导入了没有。"""
    with _LOCK:
        conn = _connect()
        return conn is not None and _migrate_legacy(conn)


def _migrate_legacy(conn: sqlite3.Connection) -> bool:
    moved = []
    for name, path, parse, statement in (
        ("key_state", LEGACY_KEY_STATE, _legacy_cooldowns, _INSERT_COOLDOWN),
        ("usage_counters", LEGACY_COUNTERS, _legacy_counters, _INSERT_COUNTER),
    ):
        if name in _LEGACY_READY:
            continue
        try:
            if _migrate_file(conn, name, path, parse, statement):
                moved.append(path.name)
        except (OSError, ValueError, sqlite3.Error) as exc:
            # 不把失败当作已迁移：下一次重试，期间不能写入该表以免遮掉旧基线。
            log(f"旧状态 {name} 迁移失败：{exc}")
        else:
            _LEGACY_READY.add(name)
    if moved:
        log("已把旧的 JSON 状态迁移到数据库：" + "、".join(moved))
    return bool(moved)


class _LegacyNumber(str):
    """保留 JSON 数字的原文，只在当前周期内转换。"""


def _migrate_file(conn: sqlite3.Connection, name: str, path: Path, parse, statement: str) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    imported = False
    if conn.execute("SELECT 1 FROM legacy_migrations WHERE name = ?", (name,)).fetchone() is None:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False  # 另一进程已提交并归档
        if name == "usage_counters":
            raw = json.loads(text, parse_int=_LegacyNumber, parse_float=_LegacyNumber)
        else:
            raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("旧状态不是 JSON 对象")
        rows = parse(raw)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            imported = conn.execute(
                "INSERT INTO legacy_migrations (name) VALUES (?) ON CONFLICT DO NOTHING", (name,)
            ).rowcount > 0
            if imported:
                # 不覆盖已有 SQLite 行；记录与标记同事务提交，归档失败也不会在重启后重复迁移。
                conn.executemany(statement + " ON CONFLICT DO NOTHING", rows)
    try:
        os.replace(path, path.with_name(path.name + ".migrated"))
    except OSError:
        pass
    return imported


def _legacy_cooldowns(raw: dict) -> list[tuple]:
    now = time.time()
    rows = []
    for provider, bucket in raw.items():
        if not isinstance(bucket, dict):
            continue
        for fp, item in bucket.items():
            if not isinstance(item, dict):
                continue
            try:
                reset_at = float(item.get("reset_at", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if _active_reset(reset_at, now):
                rows.append((provider, fp, str(item.get("code", "")), str(item.get("message", ""))[:200], reset_at))
    return rows


def _legacy_counters(raw: dict) -> list[tuple]:
    rows = []
    for scope, period in zip(("day", "month"), periods()):
        if scope not in raw:
            continue
        periods_data = raw[scope]
        if not isinstance(periods_data, dict):
            raise ValueError(f"旧用量 {scope} 周期表不是 JSON 对象")
        if period not in periods_data:
            continue  # 过期周期不参与迁移，也不用校验其内容
        period_data = periods_data[period]
        if not isinstance(period_data, dict):
            raise ValueError(f"旧用量 {scope}/{period} 厂商表不是 JSON 对象")
        for provider, bucket in period_data.items():
            if not isinstance(bucket, dict):
                raise ValueError(f"旧用量 {scope}/{period} 厂商计数不是 JSON 对象")
            for fp, count in bucket.items():
                if isinstance(count, _LegacyNumber):
                    # 不经二进制浮点舍入；只接受能无损写入 SQLite 的数值。
                    try:
                        number = Decimal(count)
                        if (number.is_finite() and 0 <= number <= _MAX_COUNT
                                and number == number.to_integral_value()):
                            count = int(number)
                    except DecimalException:
                        pass
                if not _valid_count(count):
                    raise ValueError(f"旧用量 {scope}/{period} 的计数不是有效的非负整数")
                rows.append((scope, period, provider, fp, count))
    return rows


# ---------------------------------------------------------------- 冷却和用量读取


def _active_reset(reset_at, now: float) -> bool:
    return isinstance(reset_at, (int, float)) and reset_at > now and math.isfinite(reset_at)


def _valid_count(count) -> bool:
    return type(count) is int and 0 <= count <= _MAX_COUNT


def load_key_state() -> dict[str, dict[str, dict]]:
    """读取全库当前仍有效的冷却状态，不清理过期数据。"""
    data: dict[str, dict[str, dict]] = {}
    with _LOCK:
        conn = _connect()
        if conn is None:
            return data
        _migrate_legacy(conn)
        now = time.time()
        try:
            for provider, fp, code, message, reset_at in conn.execute(
                "SELECT provider, fingerprint, code, message, reset_at FROM key_state WHERE reset_at > ?", (now,)
            ):
                if _active_reset(reset_at, now):
                    data.setdefault(provider, {})[fp] = {"code": code, "message": message, "reset_at": reset_at}
        except sqlite3.Error:
            return {}
    return data


def load_counters() -> dict[tuple[str, str, str, str], int]:
    """读取全库当日 / 当月计数（旧周期不参与，不在读取时删除）。"""
    with _LOCK:
        conn = _connect()
        if conn is None:
            return {}
        _migrate_legacy(conn)
        try:
            return {
                (scope, period, provider, fp): count
                for scope, period, provider, fp, count in conn.execute(
                    "SELECT scope, period, provider, fingerprint, count FROM usage_counters WHERE " + _CURRENT_PERIODS,
                    periods(),
                ) if _valid_count(count)
            }
        except sqlite3.Error:
            return {}


@dataclass
class ProviderState:
    cooldowns: dict[str, float] = field(default_factory=dict)
    daily: dict[str, int | None] = field(default_factory=dict)
    monthly: dict[str, int | None] = field(default_factory=dict)
    reliable: bool = True

    def used(self, fp: str) -> tuple[int | None, int | None]:
        return (self.daily.get(fp, 0), self.monthly.get(fp, 0)) if self.reliable else (None, None)


def _read_provider(
    conn: sqlite3.Connection, provider: str, now: float, cooldowns: bool, counters: bool,
) -> ProviderState:
    state = ProviderState(reliable={"key_state", "usage_counters"} <= _LEGACY_READY)
    queries: list[str] = []
    params: list[str | float] = []
    if cooldowns:
        queries.append("SELECT 'cooldown', fingerprint, reset_at FROM key_state WHERE provider = ? AND reset_at > ?")
        params.extend((provider, now))
    if counters:
        queries.append("SELECT scope, fingerprint, count FROM usage_counters WHERE provider = ? AND " + _CURRENT_PERIODS)
        params.extend((provider, *periods(now)))
    if queries:
        for scope, fp, value in conn.execute(" UNION ALL ".join(queries), params):
            if scope == "cooldown":
                if _active_reset(value, now):
                    state.cooldowns[fp] = value
            else:
                bucket = state.daily if scope == "day" else state.monthly
                bucket[fp] = value if _valid_count(value) else None
    return state


def read_provider_state(provider: str, *, cooldowns: bool = True, counters: bool = True) -> ProviderState:
    """单条 SELECT 取得该厂商的状态快照；读不到用量时与实际零用量区分。"""
    with _LOCK:
        conn = _connect()
        if conn is not None:
            _migrate_legacy(conn)
            try:
                return _read_provider(conn, provider, time.time(), cooldowns, counters)
            except sqlite3.Error:
                pass
        return ProviderState(reliable=False)


# ---------------------------------------------------------------- 冷却与用量写入


def upsert_key_state(provider: str, fp: str, code: str, message: str, reset_at: float) -> None:
    """只更新指定 key；后返回的短冷却不能缩短已经落地的长冷却。"""
    if not _active_reset(reset_at, time.time()):
        return
    with _LOCK:
        conn = _connect()
        if conn is None:
            return
        _migrate_legacy(conn)
        if "key_state" not in _LEGACY_READY:
            return
        try:
            with conn:
                conn.execute(_INSERT_COOLDOWN + """
                    ON CONFLICT(provider, fingerprint) DO UPDATE SET
                        code = excluded.code, message = excluded.message, reset_at = excluded.reset_at
                    WHERE excluded.reset_at >= key_state.reset_at
                        OR typeof(key_state.reset_at) NOT IN ('integer', 'real')
                        OR key_state.reset_at = ?
                """, (provider, fp, code, message[:200], reset_at, math.inf))
        except sqlite3.Error as exc:
            log(f"冷却状态写入失败：{exc}")


def delete_key_state(provider: str, fp: str) -> None:
    """显式清除指定 key 的冷却；请求成功不调用此方法。"""
    with _LOCK:
        conn = _connect()
        if conn is None:
            return
        _migrate_legacy(conn)
        if "key_state" not in _LEGACY_READY:
            return
        try:
            with conn:
                conn.execute("DELETE FROM key_state WHERE provider = ? AND fingerprint = ?", (provider, fp))
        except sqlite3.Error as exc:
            log(f"冷却状态清除失败：{exc}")


def _increment(conn: sqlite3.Connection, entries: list[tuple[str, str, str, str, int]]) -> None:
    if conn.executemany(_INCREMENT_COUNTER, entries).rowcount != len(entries):
        raise sqlite3.IntegrityError("用量计数损坏或无法再自增")


def increment_counters(entries: list[tuple[str, str, str, str, int]]) -> None:
    """普通计数尽力持久化；有额度约束的请求应使用 reserve_key。"""
    if not entries:
        return
    with _LOCK:
        conn = _connect()
        if conn is None:
            return
        _migrate_legacy(conn)
        if "usage_counters" not in _LEGACY_READY:
            return
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                _increment(conn, entries)
        except sqlite3.Error as exc:
            log(f"用量计数写入失败：{exc}")


def reserve_key(provider: str, fingerprints: list[str], limits: Callable[[], tuple[int, int]]) -> str | None:
    """按优先顺序选第一个可用指纹，提交 day/month 预占后才返回。

    `limits` 在拿到写锁后取值，以便复核等待期间热加载的更严格上限。
    """
    if not fingerprints:
        return None
    with _LOCK:
        conn = _connect()
        if conn is not None:
            _migrate_legacy(conn)
        if conn is not None and "usage_counters" in _LEGACY_READY:
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    daily, monthly = limits()
                    capped = bool(daily or monthly)
                    now = time.time()
                    state = _read_provider(conn, provider, now, cooldowns=True, counters=capped)
                    for fp in fingerprints:
                        if fp in state.cooldowns:
                            continue
                        if capped:
                            today, month = state.used(fp)
                            if today is None or month is None or today == _MAX_COUNT or month == _MAX_COUNT:
                                continue  # 未能可靠记账，不能拿这个 key
                            if (daily and today >= daily) or (monthly and month >= monthly):
                                continue
                        day_period, month_period = periods(now)
                        _increment(conn, [("day", day_period, provider, fp, 1), ("month", month_period, provider, fp, 1)])
                        return fp  # 退出 with 时提交，提交失败会进入 except，不会返回指纹
                    return None
            except sqlite3.Error as exc:
                log(f"用量预占失败：{exc}")
        if any(limits()):
            return None
        # 没设上限的调用延续尽力记账；若仍读得到数据库（例如另有进程持写锁），仍遵守冷却。
        if conn is not None:
            try:
                state = _read_provider(conn, provider, time.time(), cooldowns=True, counters=False)
                return next((fp for fp in fingerprints if fp not in state.cooldowns), None)
            except sqlite3.Error:
                pass
        return fingerprints[0]
