"""多 key 轮询池 + 限额状态持久化。

设计参考 Agent2API 的 next_key / record_failure / record_success：

- 同一家可以配多个 key，按顺序轮询；一次搜索内同一个 key 不重复尝试
- key 失败时按错误类型打标记并带冷却截止时间（reset_at），冷却期内跳过
- 冷却到期自动恢复；成功后清掉该 key 的失败标记
- 状态只落盘 key 的 sha256 摘要，不落明文 key

错误分档（决定冷却多久、影响范围）：
- invalid   : 401/403 —— key 无效，长时间冷却（默认 24h）
- quota     : 402/429 + 额度类文案 —— 额度耗尽，冷却 QUOTA_COOLDOWN
- rate      : 429 —— 限流，冷却 RATE_COOLDOWN（取 Retry-After 优先）
- 其他      : 不标记，只当次失败
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from config import KEY_STATE_FILE, QUOTA_COOLDOWN, RATE_COOLDOWN

INVALID_COOLDOWN = 24 * 3600

# 额度耗尽类文案（各家措辞不一，命中即按 quota 处理）。
# 注意别放过于宽泛的词（如 exceeded / limit），否则会把 "rate limit exceeded" 也判成额度。
_QUOTA_HINTS = (
    "quota",
    "credit",
    "insufficient",
    "out of",
    "余额",
    "额度",
    "欠费",
)

# 限流类文案：429 命中这些就按 rate 处理
_RATE_HINTS = ("rate limit", "too many requests", "限流", "频率")

# key 失效类文案：403 命中这些才判 invalid（避免把反爬/地区封锁当好 key 被封）
_INVALID_HINTS = (
    "invalid api key",
    "invalid_api_key",
    "invalid key",
    "unauthorized",
    "authentication",
    "api key is invalid",
    "key not found",
    "token is invalid",
    "无效的",
    "鉴权失败",
)


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def classify(status: int, message: str) -> str | None:
    """把一次失败归类为 invalid / quota / rate，None 表示不值得打标记。

    注意 403 的坑：Cloudflare / 地区封锁 / 套餐权限 / 反爬都会返回 403，
    那不是 key 失效，一律按 invalid 冷却 24h 会把好 key 白封一天。
    所以 403 只在文案明确指向 key 问题时才判 invalid，否则按 rate 短冷却。
    """
    text = (message or "").lower()
    if status == 401:
        return "invalid"
    if status == 403:
        if any(h in text for h in _INVALID_HINTS):
            return "invalid"
        return "rate"
    if status == 402:
        return "quota"
    if status == 429:
        if any(h in text for h in _RATE_HINTS):
            return "rate"
        return "quota" if any(h in text for h in _QUOTA_HINTS) else "rate"
    return None


@dataclass
class KeyState:
    code: str = ""          # invalid / quota / rate
    message: str = ""
    reset_at: float = 0.0   # epoch 秒；<= now 视为已恢复

    def active(self, now: float | None = None) -> bool:
        return self.reset_at > (now if now is not None else time.time())


@dataclass
class KeyPool:
    """单家的 key 池，跨请求轮转，而不是每次都用第一个 key。"""

    provider: str
    keys: list[str] = field(default_factory=list)
    _cursor: int = field(default=0, init=False, repr=False)

    def __bool__(self) -> bool:
        return bool(self.keys)

    def __len__(self) -> int:
        return len(self.keys)

    # ---- 轮询 ----

    def next_key(self, attempted: set[str] | None = None) -> str | None:
        """取下一个可用 key：跳过本次已试过、冷却期内和其他请求正在使用的 key。

        起点在上次选中 key 的后一位，让多个 key 的额度均衡消耗；
        同一次请求内重试不会重复选同一个 key。
        """
        attempted = attempted or set()
        now = time.time()
        with _LOCK:
            states = _load_state()
            bucket = states.get(self.provider, {})
            total = len(self.keys)
            if not total:
                return None
            start = self._cursor % total
            for offset in range(total):
                index = (start + offset) % total
                key = self.keys[index]
                fp = _fingerprint(key)
                if fp in attempted or fp in _IN_FLIGHT:
                    continue
                state = bucket.get(fp)
                if state and state.active(now):
                    continue
                self._cursor = (index + 1) % total
                _IN_FLIGHT.add(fp)
                return key
        return None

    def release(self, key: str) -> None:
        """请求结束后释放 in-flight 标记；冷却状态由 record_failure 另行记录。"""
        with _LOCK:
            _IN_FLIGHT.discard(_fingerprint(key))

    def available(self) -> int:
        """当前可用（未冷却）的 key 数量。"""
        now = time.time()
        states = _load_state().get(self.provider, {})
        return sum(
            1
            for key in self.keys
            if not (states.get(_fingerprint(key)) or KeyState()).active(now)
        )

    def earliest_reset(self) -> float | None:
        """所有 key 都被冷却时，最早恢复的时间点。"""
        states = _load_state().get(self.provider, {})
        resets = [
            states[_fingerprint(key)].reset_at
            for key in self.keys
            if _fingerprint(key) in states
            and states[_fingerprint(key)].reset_at > time.time()
        ]
        return min(resets) if resets else None

    # ---- 状态记录 ----

    def record_failure(self, key: str, status: int, message: str, retry_after: float | None = None) -> None:
        code = classify(status, message)
        if code is None:
            return
        if code == "invalid":
            cooldown = INVALID_COOLDOWN
        elif code == "quota":
            cooldown = retry_after if retry_after is not None else QUOTA_COOLDOWN
        else:
            cooldown = retry_after if retry_after is not None else RATE_COOLDOWN
        fp = _fingerprint(key)
        with _LOCK:
            states = _load_state()
            states.setdefault(self.provider, {})[fp] = KeyState(
                code=code,
                message=(message or "")[:200],
                reset_at=time.time() + cooldown,
            )
            _save_state(states)

    def record_success(self, key: str) -> None:
        fp = _fingerprint(key)
        with _LOCK:
            states = _load_state()
            bucket = states.get(self.provider)
            if bucket and bucket.pop(fp, None) is not None:
                if not bucket:
                    states.pop(self.provider, None)
                _save_state(states)

    def status_line(self) -> str:
        """给结果尾部用的状态摘要，如 '2/3 可用（1 个冷却中）'。"""
        total = len(self.keys)
        if not total:
            return "无需 key"
        ok = self.available()
        if ok == total:
            return f"{ok}/{total} 可用"
        reset = self.earliest_reset()
        when = ""
        if reset:
            left = max(0, int(reset - time.time()))
            when = f"，最早 {left // 60} 分钟后恢复" if left >= 60 else f"，最早 {left} 秒后恢复"
        return f"{ok}/{total} 可用（{total - ok} 个冷却中{when}）"


# ---- 状态读写 ----

_LOCK = threading.RLock()
_IN_FLIGHT: set[str] = set()
_CACHE: dict[str, dict[str, KeyState]] | None = None
_CACHE_MTIME: float = -1.0


def _file_mtime() -> float:
    try:
        return KEY_STATE_FILE.stat().st_mtime
    except OSError:
        return -1.0


def _load_state() -> dict[str, dict[str, KeyState]]:
    """读取状态。文件被外部改动（mtime 变）或删除时自动重载，无需重启。"""
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        mtime = _file_mtime()
        if _CACHE is not None and mtime == _CACHE_MTIME:
            return _CACHE
        data: dict[str, dict[str, KeyState]] = {}
        try:
            raw = json.loads(KEY_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        now = time.time()
        for provider, bucket in raw.items():
            if not isinstance(bucket, dict):
                continue
            # 顺手清掉已过期的条目，避免状态文件随换 key 无限增长
            fresh = {
                fp: KeyState(
                    code=str(item.get("code", "")),
                    message=str(item.get("message", "")),
                    reset_at=float(item.get("reset_at", 0) or 0),
                )
                for fp, item in bucket.items()
                if isinstance(item, dict)
            }
            fresh = {fp: st for fp, st in fresh.items() if st.reset_at > now}
            if fresh:
                data[provider] = fresh
        _CACHE = data
        _CACHE_MTIME = mtime
        return data


def _save_state(states: dict[str, dict[str, KeyState]]) -> None:
    """原子写入：先写临时文件再 os.replace，避免崩溃留下半截 JSON。"""
    global _CACHE, _CACHE_MTIME
    _CACHE = states
    payload = {
        provider: {
            fp: {"code": st.code, "message": st.message, "reset_at": st.reset_at}
            for fp, st in bucket.items()
        }
        for provider, bucket in states.items()
        if bucket
    }
    try:
        KEY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = KEY_STATE_FILE.with_name(KEY_STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, KEY_STATE_FILE)
        _CACHE_MTIME = _file_mtime()
    except OSError as exc:
        # 状态文件写不了不影响搜索，只是重启后丢冷却信息
        print(f"[aggregate-search] 冷却状态写入失败: {exc}", file=sys.stderr)


def reset_provider(provider: str) -> None:
    """手工清掉某家的冷却状态（调试用）。"""
    with _LOCK:
        states = _load_state()
        if states.pop(provider, None) is not None:
            _save_state(states)
