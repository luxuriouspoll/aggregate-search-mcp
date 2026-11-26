"""多 key 轮询池 + 限额状态持久化。

设计参考 Agent2API 的 next_key / record_failure / record_success：

- 同一家可以配多个 key，按顺序轮询；一次搜索内同一个 key 不重复尝试
- 并发请求优先分到空闲的 key；key 不够分时共用，而不是让后来的请求直接失败
- key 失败时按错误类型打标记并带冷却截止时间（reset_at），冷却期内跳过
- 冷却到期自动恢复；先前已发出的成功响应不清除其他请求设下的冷却
- 到达本地用量上限（DAILY_LIMITS / MONTHLY_LIMITS）的 key 同样跳过，发送前原子预占
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
import math
import threading
import time
from dataclasses import dataclass, field

from . import config, store, usage

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
class _PoolStatus:
    total: int
    cooling: int = 0
    capped: int = 0
    usable: int = 0
    reset: float | None = None
    reason: str = ""
    unreliable: int = 0
    note: str = ""


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
        取到 key 前先完成预占；出错或取消不退还，避免少记上游可能已收到的调用。
        """
        attempted = attempted or set()
        with _LOCK:
            keys = self.keys
            total = len(keys)
            if not total:
                return None
            start = self._cursor % total
            candidates = []
            for offset in range(total):
                index = (start + offset) % total
                fp = _fingerprint(keys[index])
                if fp not in attempted:
                    candidates.append((_IN_FLIGHT.get(fp, 0), offset, index, fp))
            # 保持「占用数优先、同占用轮转」；失败候选在同一事务里跳过。
            candidates.sort()
            chosen = store.reserve_key(
                self.provider, [candidate[3] for candidate in candidates],
                lambda: usage.limits_for(self.provider, reservation=True),
            )
            if chosen is None:
                return None
            index = next(index for _, _, index, fp in candidates if chosen == fp)
            self._cursor = (index + 1) % total
            _IN_FLIGHT[chosen] = _IN_FLIGHT.get(chosen, 0) + 1
            return keys[index]

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

    def _status(self, *, show_usage: bool = False) -> _PoolStatus:
        keys = self.keys
        status = _PoolStatus(total=len(keys))
        if not keys:
            return status
        limits = usage.limits_for(self.provider)
        state = store.read_provider_state(self.provider, counters=show_usage or any(limits))
        today = month = 0
        for key in keys:
            fp = _fingerprint(key)
            reset = state.cooldowns.get(fp)
            if reset is not None:
                status.cooling += 1
                status.reset = reset if status.reset is None else min(status.reset, reset)
            counts = state.used(fp)
            reason = usage.limit_reason(counts, limits)
            if reason == usage.ACCOUNTING_UNAVAILABLE:
                status.unreliable += 1
            elif reason:
                status.capped += 1
                if not status.reason:
                    status.reason = reason
            elif reset is None:
                status.usable += 1
            today += counts[0] or 0
            month += counts[1] or 0
        daily, monthly = limits
        if show_usage and (today or month or daily or monthly) and not status.unreliable:
            parts = [f"今日已用 {today}" + (f"/{daily * status.total}" if daily else "")]
            if monthly:
                parts.append(f"本月已用 {month}/{monthly * status.total}")
            status.note = "；" + "，".join(parts)
        return status

    def available(self) -> int:
        """当前未冷却的 key 数量（不看用量上限）。"""
        status = self._status()
        return status.total - status.cooling

    def usable(self) -> int:
        """当前可用的 key 数量。实际发请求前以事务内预占结果为准。"""
        return self._status().usable

    def capped(self) -> int:
        """已到本地用量上限的 key 数量。"""
        return self._status().capped

    def capped_reason(self) -> str:
        """到的是日上限还是月上限，如「今日已用 90/90」；没到量返回空串。"""
        return self._status().reason

    def earliest_reset(self) -> float | None:
        """有冷却的 key 中最早恢复的时间点。"""
        return self._status().reset

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
        settings = config.get_settings()
        if code == "invalid":
            # 状态码是猜的就别下重手：猜错了也只是歇一会儿，而不是把好 key 封一天
            cooldown = min(INVALID_COOLDOWN, settings.QUOTA_COOLDOWN) if soft else INVALID_COOLDOWN
        else:
            default = settings.QUOTA_COOLDOWN if code == "quota" else settings.RATE_COOLDOWN
            cooldown = retry_after if retry_after is not None and math.isfinite(retry_after) else default
        message = message or ""
        if key:
            message = message.replace(key, "***")
        store.upsert_key_state(self.provider, _fingerprint(key), code, message, time.time() + cooldown)

    def record_success(self, key: str) -> None:
        """保持调用接口；预占时没有冷却，后来出现的标记不能被旧成功清掉。"""

    def status_line(self) -> str:
        """给结果尾部用的状态摘要，如 '2/3 可用（1 个冷却中）；今日已用 12/90'。"""
        status = self._status(show_usage=True)
        if not status.total:
            return "无需 key"
        line = f"{status.usable}/{status.total} 可用"
        notes = []
        if status.cooling:
            left = max(0, int(status.reset - time.time())) if status.reset is not None else 0
            when = f"，最早 {left // 60} 分钟后恢复" if left >= 60 else f"，最早 {left} 秒后恢复"
            notes.append(f"{status.cooling} 个冷却中{when}")
        if status.capped:
            notes.append(f"{status.capped} 个已到本地用量上限")
        if status.unreliable:
            notes.append(f"{status.unreliable} 个{usage.ACCOUNTING_UNAVAILABLE}")
        if notes:
            line += f"（{'；'.join(notes)}）"
        return line + status.note


_LOCK = threading.RLock()
# key 指纹 -> 正在使用它的请求数。只用来把并发请求分散到不同 key，不是互斥锁。
_IN_FLIGHT: dict[str, int] = {}
