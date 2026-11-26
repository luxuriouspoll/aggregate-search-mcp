"""多 key 轮询池 + 限额状态持久化。

设计参考 Agent2API 的 next_key / record_failure / record_success：

- 同一家可以配多个 key，按顺序轮询；一次搜索内同一个 key 不重复尝试
- 并发请求优先分到空闲的 key；key 不够分时共用，而不是让后来的请求直接失败
- key 失败时按错误类型打标记并带冷却截止时间（reset_at），冷却期内跳过
- 冷却到期自动恢复；成功后清掉该 key 的失败标记
- 到达本地用量上限（DAILY_LIMITS / MONTHLY_LIMITS）的 key 同样跳过
- 状态只落盘 key 的 sha256 摘要，不落明文 key

错误分档（决定冷却多久、影响范围）：
- invalid   : 401，或 403 且文案明确指向 key —— key 无效，长时间冷却（默认 24h）
- quota     : 402/429 + 额度类文案 —— 额度耗尽，冷却 QUOTA_COOLDOWN
- rate      : 429 —— 限流，冷却 RATE_COOLDOWN（取 Retry-After 优先）
- 其他      : 不标记，只当次失败

状态码如果是从报错文案里猜出来的（soft），最多冷却 QUOTA_COOLDOWN，不会按 invalid 封一整天。
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field

from . import usage
from . import store
from .config import QUOTA_COOLDOWN, RATE_COOLDOWN

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
        """取下一个可用 key：跳过本次已试过、冷却期内和已到用量上限的 key。

        起点在上次选中 key 的后一位，让多个 key 的额度均衡消耗；
        同一次请求内重试不会重复选同一个 key。
        并发时优先挑没人在用的 key；都在用就共用负载最轻的那个——
        只配了一个 key 的厂商也得能同时服务多个请求。
        取到 key 即记一次用量（按发出的请求数计）。
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
            best: tuple[int, int, int] | None = None  # (并发占用数, 轮转偏移, 下标)
            for offset in range(total):
                index = (start + offset) % total
                fp = _fingerprint(self.keys[index])
                if fp in attempted:
                    continue
                state = bucket.get(fp)
                if state and state.active(now):
                    continue
                if usage.limit_reached(self.provider, fp):
                    continue
                candidate = (_IN_FLIGHT.get(fp, 0), offset, index)
                if best is None or candidate < best:
                    best = candidate
            if best is None:
                return None
            index = best[2]
            key = self.keys[index]
            fp = _fingerprint(key)
            self._cursor = (index + 1) % total
            _IN_FLIGHT[fp] = _IN_FLIGHT.get(fp, 0) + 1
            usage.count_call(self.provider, fp)
            return key

    def release(self, key: str) -> None:
        """请求结束后释放 in-flight 占用；冷却状态由 record_failure 另行记录。"""
        fp = _fingerprint(key)
        with _LOCK:
            left = _IN_FLIGHT.get(fp, 0) - 1
            if left > 0:
                _IN_FLIGHT[fp] = left
            else:
                _IN_FLIGHT.pop(fp, None)

    def index_of(self, key: str) -> int:
        """key 在本家池子里的序号（1 起），只用于记录“这次用的是第几个 key”。"""
        try:
            return self.keys.index(key) + 1
        except ValueError:
            return 0

    def available(self) -> int:
        """当前未冷却的 key 数量（不看用量上限）。"""
        now = time.time()
        states = _load_state().get(self.provider, {})
        return sum(
            1
            for key in self.keys
            if not (states.get(_fingerprint(key)) or KeyState()).active(now)
        )

    def usable(self) -> int:
        """现在真能拿来发请求的 key 数量：未冷却，且没到本地用量上限。"""
        now = time.time()
        states = _load_state().get(self.provider, {})
        count = 0
        for key in self.keys:
            fp = _fingerprint(key)
            if (states.get(fp) or KeyState()).active(now):
                continue
            if usage.limit_reached(self.provider, fp):
                continue
            count += 1
        return count

    def capped(self) -> int:
        """已到本地用量上限的 key 数量。"""
        return sum(
            1 for key in self.keys if usage.limit_reached(self.provider, _fingerprint(key))
        )

    def capped_reason(self) -> str:
        """到的是日上限还是月上限，如「今日已用 90/90」；没到量返回空串。"""
        for key in self.keys:
            reason = usage.limit_reached(self.provider, _fingerprint(key))
            if reason:
                return reason
        return ""

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

    def record_failure(
        self,
        key: str,
        status: int,
        message: str,
        retry_after: float | None = None,
        soft: bool = False,
    ) -> None:
        code = classify(status, message)
        if code is None:
            return
        if code == "invalid":
            # 状态码是猜的就别下重手：猜错了也只是歇一会儿，而不是把好 key 封一天
            cooldown = min(INVALID_COOLDOWN, QUOTA_COOLDOWN) if soft else INVALID_COOLDOWN
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
        """给结果尾部用的状态摘要，如 '2/3 可用（1 个冷却中）；今日已用 12/90'。"""
        total = len(self.keys)
        if not total:
            return "无需 key"
        ok = self.usable()
        if ok == total:
            line = f"{ok}/{total} 可用"
        else:
            cooling = total - self.available()
            capped = self.capped()
            notes = []
            if cooling:
                reset = self.earliest_reset()
                when = ""
                if reset:
                    left = max(0, int(reset - time.time()))
                    when = f"，最早 {left // 60} 分钟后恢复" if left >= 60 else f"，最早 {left} 秒后恢复"
                notes.append(f"{cooling} 个冷却中{when}")
            if capped:
                notes.append(f"{capped} 个已到本地用量上限")
            line = f"{ok}/{total} 可用（{'；'.join(notes)}）"
        return line + self._usage_note()

    def _usage_note(self) -> str:
        daily, monthly = usage.limits_for(self.provider)
        today = month = 0
        for key in self.keys:
            used_today, used_month = usage.used(self.provider, _fingerprint(key))
            today += used_today
            month += used_month
        if not (today or month or daily or monthly):
            return ""
        count = len(self.keys)
        parts = [f"今日已用 {today}" + (f"/{daily * count}" if daily else "")]
        if monthly:
            parts.append(f"本月已用 {month}/{monthly * count}")
        return "；" + "，".join(parts)


# ---- 状态读写 ----

_LOCK = threading.RLock()
# key 指纹 -> 正在使用它的请求数。只用来把并发请求分散到不同 key，不是互斥锁。
_IN_FLIGHT: dict[str, int] = {}
_CACHE: dict[str, dict[str, KeyState]] | None = None
_CACHE_MTIME: float = -1.0


def _file_mtime() -> float:
    try:
        return store.DB_FILE.stat().st_mtime
    except OSError:
        return -1.0


def _load_state() -> dict[str, dict[str, KeyState]]:
    """读取冷却状态（SQLite）。带一层内存缓存，避免每次取 key 都查库。"""
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        mtime = _file_mtime()
        if _CACHE is not None and mtime == _CACHE_MTIME:
            return _CACHE
        data: dict[str, dict[str, KeyState]] = {}
        for provider, bucket in store.load_key_state().items():
            fresh = {}
            for fp, item in bucket.items():
                try:
                    reset_at = float(item.get("reset_at", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if reset_at > time.time():
                    fresh[str(fp)] = KeyState(
                        code=str(item.get("code", "")),
                        message=str(item.get("message", "")),
                        reset_at=reset_at,
                    )
            if fresh:
                data[provider] = fresh
        _CACHE = data
        _CACHE_MTIME = mtime
        return data


def _save_state(states: dict[str, dict[str, KeyState]]) -> None:
    """写入冷却状态（SQLite 事务，多进程并发安全）。"""
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
    store.save_key_state(payload)
    _CACHE_MTIME = _file_mtime()

