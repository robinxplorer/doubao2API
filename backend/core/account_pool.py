"""
doubao2api AccountPool — 豆包账号池管理

与 qwen2API 的核心区别：
- 豆包账号用 sessionid（Cookie 认证），而非 email/token
- 无需激活流程，sessionid 有效即可使用
- Account 字段围绕 sessionid 设计
"""

import asyncio
import logging
import random
import time
from typing import Optional
from backend.core.database import AsyncJsonDB
from backend.core.config import settings

log = logging.getLogger("doubao2api.accounts")


def _jitter_seconds() -> float:
    low = max(0, settings.REQUEST_JITTER_MIN_MS)
    high = max(low, settings.REQUEST_JITTER_MAX_MS)
    return random.uniform(low, high) / 1000.0


class Account:
    """豆包账号，以 sessionid 为核心标识。"""

    def __init__(
        self,
        sessionid: str = "",
        name: str = "",
        status_code: str = "",
        last_error: str = "",
        **kwargs,
    ):
        self.sessionid = sessionid
        self.name = name or (sessionid[:8] + "..." if sessionid else "unknown")
        self.valid = bool(sessionid)  # 有 sessionid 即视为有效
        self.last_used = 0.0
        self.inflight = 0
        self.rate_limited_until = 0.0
        self.status_code = status_code or ("valid" if self.valid else "invalid")
        self.last_error = last_error or ""
        self.last_request_started = float(kwargs.get("last_request_started", 0.0) or 0.0)
        self.last_request_finished = float(kwargs.get("last_request_finished", 0.0) or 0.0)
        self.consecutive_failures = int(kwargs.get("consecutive_failures", 0) or 0)
        self.rate_limit_strikes = int(kwargs.get("rate_limit_strikes", 0) or 0)

    def is_rate_limited(self) -> bool:
        return self.rate_limited_until > time.time()

    def is_available(self) -> bool:
        return self.valid and not self.is_rate_limited()

    def next_available_at(self) -> float:
        min_interval = max(0, settings.ACCOUNT_MIN_INTERVAL_MS) / 1000.0
        return max(self.rate_limited_until, self.last_request_started + min_interval)

    def get_status_code(self) -> str:
        if self.is_rate_limited():
            return "rate_limited"
        if self.valid:
            return "valid"
        if self.status_code == "banned":
            return "banned"
        if self.status_code == "auth_error":
            return "auth_error"
        return self.status_code or "invalid"

    def get_status_text(self) -> str:
        status_map = {
            "valid": "正常",
            "rate_limited": "限流",
            "banned": "封禁",
            "auth_error": "鉴权失败",
            "invalid": "失效",
            "session_expired": "Session 过期",
            "unknown": "未知",
        }
        return status_map.get(self.get_status_code(), "未知")

    def to_dict(self) -> dict:
        return {
            "sessionid": self.sessionid,
            "name": self.name,
            "status_code": self.status_code,
            "last_error": self.last_error,
            "last_request_started": self.last_request_started,
            "last_request_finished": self.last_request_finished,
            "consecutive_failures": self.consecutive_failures,
            "rate_limit_strikes": self.rate_limit_strikes,
        }


class AccountPool:
    """豆包账号池，管理 sessionid 的获取、释放、限流等。"""

    def __init__(self, db: AsyncJsonDB, max_inflight: int = settings.MAX_INFLIGHT_PER_ACCOUNT):
        self.db = db
        self.max_inflight = max_inflight
        self.accounts: list[Account] = []
        self._lock = asyncio.Lock()
        self._waiters: list[asyncio.Event] = []
        self._sticky_sessionid: Optional[str] = None

    async def load(self):
        data = await self.db.load()
        self.accounts = [Account(**d) for d in data] if isinstance(data, list) else []
        log.info(f"Loaded {len(self.accounts)} upstream account(s)")

    async def save(self):
        await self.db.save([a.to_dict() for a in self.accounts])

    async def add(self, account: Account):
        async with self._lock:
            # 用 sessionid 去重
            self.accounts = [a for a in self.accounts if a.sessionid != account.sessionid]
            self.accounts.append(account)
        await self.save()

    async def remove(self, sessionid: str):
        async with self._lock:
            self.accounts = [a for a in self.accounts if a.sessionid != sessionid]
        await self.save()

    def set_max_inflight(self, value: int):
        self.max_inflight = max(1, int(value))

    async def acquire(self, exclude: set = None) -> Optional[Account]:
        """获取一个可用账号，优先选择 inflight 最少、最早就绪的。"""
        async with self._lock:
            now = time.time()
            available = [
                a for a in self.accounts
                if a.is_available() and (not exclude or a.sessionid not in exclude)
            ]
            if not available:
                return None

            ready = [
                a for a in available
                if a.inflight < self.max_inflight and a.next_available_at() <= now
            ]
            if not ready:
                return None

            ready.sort(key=lambda a: (a.inflight, a.last_request_started or 0.0, a.last_used or 0.0))
            best = ready[0]
            best.inflight += 1
            best.last_used = now
            best.last_request_started = now + _jitter_seconds()
            self._sticky_sessionid = best.sessionid if len(ready) == 1 else None
            return best

    async def acquire_wait(self, timeout: float = 60, exclude: set = None) -> Optional[Account]:
        """等待获取可用账号，超时返回 None。"""
        deadline = time.time() + timeout
        while True:
            acc = await self.acquire(exclude)
            if acc:
                return acc

            async with self._lock:
                candidates = [
                    a for a in self.accounts
                    if a.valid and (not exclude or a.sessionid not in exclude)
                ]
                if not candidates:
                    return None
                next_ready_at = min(
                    (a.next_available_at() for a in candidates),
                    default=time.time(),
                )

            remaining = deadline - time.time()
            if remaining <= 0:
                return None

            evt = asyncio.Event()
            self._waiters.append(evt)
            wait_timeout = min(remaining, max(0.05, next_ready_at - time.time() + 0.05))
            try:
                await asyncio.wait_for(evt.wait(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                if evt in self._waiters:
                    self._waiters.remove(evt)

    def release(self, acc: Account):
        """释放账号，减少 inflight 计数。"""
        acc.inflight = max(0, acc.inflight - 1)
        acc.last_request_finished = time.time()
        if self._waiters:
            evt = self._waiters.pop(0)
            evt.set()

    def mark_invalid(self, acc: Account, reason: str = "invalid", error_message: str = ""):
        """标记账号为不可用。"""
        acc.valid = False
        acc.status_code = reason or "invalid"
        acc.last_error = error_message or acc.last_error
        acc.consecutive_failures += 1
        if self._sticky_sessionid == acc.sessionid:
            self._sticky_sessionid = None
        log.warning(f"[账号] {acc.name} 已标记为不可用，状态={acc.status_code}")

    def mark_success(self, acc: Account):
        """标记账号请求成功，重置失败计数。"""
        acc.consecutive_failures = 0
        acc.rate_limit_strikes = 0
        if acc.status_code == "rate_limited":
            acc.status_code = "valid"
        acc.valid = True

    def mark_rate_limited(self, acc: Account, cooldown: int | None = None, error_message: str = ""):
        """标记账号被限流，设置冷却时间。"""
        acc.rate_limit_strikes += 1
        base = cooldown if cooldown is not None else settings.RATE_LIMIT_BASE_COOLDOWN
        dynamic = min(
            settings.RATE_LIMIT_MAX_COOLDOWN,
            int(base * (2 ** max(0, acc.rate_limit_strikes - 1))),
        )
        dynamic += int(_jitter_seconds())
        acc.rate_limited_until = time.time() + dynamic
        acc.status_code = "rate_limited"
        acc.last_error = error_message or acc.last_error
        if self._sticky_sessionid == acc.sessionid:
            self._sticky_sessionid = None
        log.warning(f"[账号] {acc.name} 已限流冷却 {dynamic} 秒")

    def status(self) -> dict:
        """返回账号池状态概览。"""
        available = [a for a in self.accounts if a.is_available()]
        rate_limited = [a for a in self.accounts if a.get_status_code() == "rate_limited"]
        invalid = [a for a in self.accounts if a.get_status_code() not in ("valid", "rate_limited")]
        banned = [a for a in self.accounts if a.get_status_code() == "banned"]
        session_expired = [a for a in self.accounts if a.get_status_code() == "session_expired"]
        in_use = sum(a.inflight for a in self.accounts)
        return {
            "total": len(self.accounts),
            "valid": len(available),
            "rate_limited": len(rate_limited),
            "invalid": len(invalid),
            "banned": len(banned),
            "session_expired": len(session_expired),
            "in_use": in_use,
            "max_inflight": self.max_inflight,
            "waiting": len(self._waiters),
            "account_min_interval_ms": settings.ACCOUNT_MIN_INTERVAL_MS,
        }
