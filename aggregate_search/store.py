"""状态持久化：SQLite（WAL 模式）。

替代原来的 key-state.json 与 usage-counters.json。用 SQLite 的原因：
多个客户端各自起一个服务进程时，两个进程同时改同一个 JSON 文件会互相覆盖，
冷却状态和当日计数会丢（表现为「明明到量了又去请求」）。SQLite 的写事务天然互斥。

设计要点：
- 只存 key 的 SHA-256 指纹，不存明文 key
- 开 WAL + busy_timeout，读写不互相阻塞
- 首次启动时若发现旧 JSON，自动迁移过来，旧文件改名留档（不删，方便回滚）
- 任何数据库错误都不影响搜索：读失败按「没有状态」处理，写失败只告警
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .config import CONFIG_DIR, ensure_home, log

DB_FILE: Path = CONFIG_DIR / "state.db"
# 旧版文件（迁移后改名留档）
LEGACY_KEY_STATE = CONFIG_DIR / "key-state.json"
LEGACY_COUNTERS = CONFIG_DIR / "usage-counters.json"

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_MIGRATED = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS key_state (
    provider TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    reset_at REAL NOT NULL,
    PRIMARY KEY (provider, fingerprint)
);
CREATE TABLE IF NOT EXISTS usage_counters (
    scope TEXT NOT NULL,      -- 'day' 或 'month'
    period TEXT NOT NULL,     -- '2026-11-26' 或 '2026-11'
    provider TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, period, provider, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_usage_period ON usage_counters(scope, period);
"""


def _connect() -> sqlite3.Connection | None:
    """取连接（同进程复用）。失败返回 None，调用方按「没有持久化」处理。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            return _CONN
        try:
            ensure_home()
            conn = sqlite3.connect(str(DB_FILE), timeout=5.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            _CONN = conn
        except sqlite3.Error as exc:
            log(f"状态数据库不可用（冷却与计数不会持久化）：{exc}")
            return None
        return _CONN


def reset_connection() -> None:
    """关掉当前连接（测试里换数据库文件时用）。"""
    global _CONN, _MIGRATED
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except sqlite3.Error:
                pass
        _CONN = None
        _MIGRATED = False


# ---------------------------------------------------------------- 迁移

def migrate_legacy() -> bool:
    """把旧版两个 JSON 导入数据库，并把旧文件改名留档。返回是否发生了迁移。"""
    global _MIGRATED
    with _LOCK:
        if _MIGRATED:
            return False
        _MIGRATED = True
        conn = _connect()
        if conn is None:
            return False
        moved: list[str] = []
        if LEGACY_KEY_STATE.is_file():
            moved += _migrate_key_state(conn)
        if LEGACY_COUNTERS.is_file():
            moved += _migrate_counters(conn)
        if moved:
            log("已把旧的 JSON 状态迁移到数据库：" + "、".join(moved))
        return bool(moved)


def _rename_legacy(path: Path) -> None:
    """迁移后把旧文件改名留档；改不了就留着（下次不会重复导入，因为数据已在库里）。"""
    try:
        backup = path.with_name(path.name + ".migrated")
        if backup.exists():
            backup.unlink()
        os.replace(path, backup)
    except OSError:
        pass


def _migrate_key_state(conn: sqlite3.Connection) -> list[str]:
    try:
        raw = json.loads(LEGACY_KEY_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
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
            except (TypeError, ValueError):
                continue
            if reset_at <= now:
                continue      # 已经过期，不用导
            rows.append((str(provider), str(fp), str(item.get("code", "")),
                         str(item.get("message", ""))[:200], reset_at))
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO key_state (provider, fingerprint, code, message, reset_at)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except sqlite3.Error:
        return []
    _rename_legacy(LEGACY_KEY_STATE)
    return [LEGACY_KEY_STATE.name]


def _migrate_counters(conn: sqlite3.Connection) -> list[str]:
    try:
        raw = json.loads(LEGACY_COUNTERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    day, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    keep = {"day": day, "month": month}
    rows = []
    for scope in ("day", "month"):
        periods = raw.get(scope)
        if not isinstance(periods, dict):
            continue
        period_data = periods.get(keep[scope])
        if not isinstance(period_data, dict):
            continue
        for provider, bucket in period_data.items():
            if not isinstance(bucket, dict):
                continue
            for fp, count in bucket.items():
                if isinstance(count, bool) or not isinstance(count, (int, float)):
                    continue
                rows.append((scope, keep[scope], str(provider), str(fp), max(0, int(count))))
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO usage_counters (scope, period, provider, fingerprint, count)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except sqlite3.Error:
        return []
    _rename_legacy(LEGACY_COUNTERS)
    return [LEGACY_COUNTERS.name]


# ---------------------------------------------------------------- key 冷却状态

def load_key_state() -> dict[str, dict[str, dict]]:
    """读取全部冷却状态：{厂商: {指纹: {code, message, reset_at}}}。顺带清掉过期项。"""
    migrate_legacy()
    conn = _connect()
    if conn is None:
        return {}
    now = time.time()
    with _LOCK:
        try:
            rows = conn.execute(
                "SELECT provider, fingerprint, code, message, reset_at FROM key_state WHERE reset_at > ?",
                (now,),
            ).fetchall()
            # 顺手把过期行删掉，表不会无限增长
            conn.execute("DELETE FROM key_state WHERE reset_at <= ?", (now,))
            conn.commit()
        except sqlite3.Error:
            return {}
    data: dict[str, dict[str, dict]] = {}
    for provider, fp, code, message, reset_at in rows:
        data.setdefault(str(provider), {})[str(fp)] = {
            "code": str(code), "message": str(message), "reset_at": float(reset_at),
        }
    return data


def save_key_state(states: dict[str, dict[str, dict]]) -> None:
    """整体写入冷却状态（一次事务）。写失败只告警。"""
    conn = _connect()
    if conn is None:
        return
    rows = [
        (str(provider), str(fp), str(st.get("code", "")), str(st.get("message", ""))[:200],
         float(st.get("reset_at", 0) or 0))
        for provider, bucket in states.items() for fp, st in bucket.items()
    ]
    with _LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM key_state")
            conn.executemany(
                "INSERT OR REPLACE INTO key_state (provider, fingerprint, code, message, reset_at)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            log(f"冷却状态写入失败：{exc}")


# ---------------------------------------------------------------- 用量计数

def load_counters() -> dict[tuple[str, str, str, str], int]:
    """读取当日 / 当月计数：{(scope, period, 厂商, 指纹): 次数}。"""
    migrate_legacy()
    conn = _connect()
    if conn is None:
        return {}
    day, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    with _LOCK:
        try:
            rows = conn.execute(
                "SELECT scope, period, provider, fingerprint, count FROM usage_counters"
                " WHERE (scope = 'day' AND period = ?) OR (scope = 'month' AND period = ?)",
                (day, month),
            ).fetchall()
            # 过期周期顺手清掉
            conn.execute(
                "DELETE FROM usage_counters WHERE (scope = 'day' AND period <> ?)"
                " OR (scope = 'month' AND period <> ?)",
                (day, month),
            )
            conn.commit()
        except sqlite3.Error:
            return {}
    return {(str(s), str(p), str(pr), str(fp)): int(c) for s, p, pr, fp, c in rows}


def increment_counters(entries: list[tuple[str, str, str, str, int]]) -> None:
    """把若干 (scope, period, 厂商, 指纹, 增量) 累加进库（一次事务）。"""
    if not entries:
        return
    conn = _connect()
    if conn is None:
        return
    with _LOCK:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for scope, period, provider, fp, delta in entries:
                conn.execute(
                    "INSERT INTO usage_counters (scope, period, provider, fingerprint, count)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(scope, period, provider, fingerprint)"
                    " DO UPDATE SET count = count + excluded.count",
                    (scope, period, provider, fp, int(delta)),
                )
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            log(f"用量计数写入失败：{exc}")
