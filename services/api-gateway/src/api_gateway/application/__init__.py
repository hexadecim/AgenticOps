"""Gateway use cases. The BFF owns little logic of its own: throttling sign-in, and nothing else."""

from __future__ import annotations

import time

from opspilot_common.resilience import CircuitState, ResilienceSettings

from ..ports import BreakerStateReader, IdentityPort, Json, RateLimiter


class TooManyAttemptsError(Exception):
    pass


class SignIn:
    """Throttles both sign-in steps per client address before reaching identity-service."""

    def __init__(self, identity: IdentityPort, limiter: RateLimiter, limit_per_minute: int) -> None:
        self._identity, self._limiter, self._limit = identity, limiter, limit_per_minute

    async def password_step(self, client: str, email: str, password: str) -> Json:
        await self._throttle(f"sign-in:{client}")
        return await self._identity.sign_in(email, password)

    async def mfa_step(self, client: str, challenge_token: str, code: str, new_password: str | None) -> Json:
        await self._throttle(f"sign-in-mfa:{client}")
        return await self._identity.sign_in_mfa(challenge_token, code, new_password)

    async def _throttle(self, key: str) -> None:
        if not await self._limiter.allow(key, self._limit, 60):
            raise TooManyAttemptsError()


class SystemHealth:
    """Every known dependency with its live circuit state and configured thresholds."""

    def __init__(self, reader: BreakerStateReader, settings: ResilienceSettings) -> None:
        self._reader, self._settings = reader, settings

    async def circuits(self) -> list[Json]:
        snapshots = await self._reader.snapshots()
        names = sorted(set(snapshots) | set(self._settings.dependencies))
        now, rows = time.time(), []
        for name in names:
            cfg, snap = self._settings.for_dependency(name), snapshots.get(name)
            state, retry_after = "unused", None
            if snap is not None:
                state = snap.state.value
                if snap.state is CircuitState.OPEN:
                    remaining = cfg.breaker.reset_timeout - (now - snap.opened_at)
                    state, retry_after = ("half_open", None) if remaining <= 0 else ("open", round(remaining))
            rows.append(
                {
                    "dependency": name,
                    "state": state,
                    "failures": snap.failures if snap else 0,
                    "retry_after_seconds": retry_after,
                    "fail_max": cfg.breaker.fail_max,
                    "reset_timeout_seconds": cfg.breaker.reset_timeout,
                    "timeout_seconds": cfg.timeout,
                    "retry_attempts": cfg.retry.attempts,
                    "bulkhead": cfg.bulkhead,
                }
            )
        return rows
