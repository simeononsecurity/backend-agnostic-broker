"""Per-key governance: rate limits, budgets, concurrency, scoping (ADR 0008).

The :class:`Governor` is the enforcement stage that runs between authentication
and dispatch. Given an authenticated :class:`~broker.keys.Principal` it:

* reserves a concurrency slot and checks the per-minute **request** rate (rpm,
  sliding-window log) and **token** rate (tpm, windowed counter) *before*
  dispatch, using a cheap pre-dispatch token estimate;
* checks daily/monthly **token** and **cost** budgets before dispatch;
* **reconciles** actual token usage and cost into the tpm/budget counters after
  the response, and releases the concurrency slot;
* emits OpenAI-compatible ``x-ratelimit-*`` headers (and ``Retry-After`` on 429).

All counters live on the ADR 0009 :class:`~broker.state.StateStore`, so the same
code path works in-process and across Redis-backed replicas. Over-limit raises an
OpenAI-style error: ``429 rate_limit_exceeded`` for rpm/tpm/concurrency and
``429 insufficient_quota`` for budgets. Unlimited principals (legacy keys or keys
with no declared limits) short-circuit to a no-op, preserving v0.4.0 behavior.

Cost is tracked in integer **micro-USD** (1e-6 USD) so Redis/in-process integer
counters stay exact; prices come from the registry ``pricing`` block
(USD per million input/output tokens).
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass
from typing import Any

from .errors import InsufficientQuotaError, RateLimitExceededError
from .keys import KeyLimits, Principal
from .state import StateStore


@dataclass
class Reservation:
    """Handle returned by :meth:`Governor.admit`, consumed by :meth:`reconcile`."""

    usage_id: str
    limits: KeyLimits
    reserved_concurrency: bool
    now: float


@dataclass
class Admission:
    """Result of an admission check: response headers + an optional reservation."""

    headers: dict[str, str]
    reservation: Reservation | None


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _seconds_to_midnight(now_dt: _dt.datetime) -> int:
    tomorrow = (now_dt + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now_dt).total_seconds()))


def _seconds_to_month_end(now_dt: _dt.datetime) -> int:
    if now_dt.month == 12:
        nxt = now_dt.replace(
            year=now_dt.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        nxt = now_dt.replace(
            month=now_dt.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return max(1, int((nxt - now_dt).total_seconds()))


class Governor:
    """Enforces per-key limits on the ADR 0009 ``StateStore``."""

    def __init__(
        self,
        state: StateStore,
        *,
        window_seconds: float = 60.0,
        pricing: dict[str, Any] | None = None,
    ) -> None:
        self.state = state
        self.window = float(window_seconds)
        # model public name -> object with .input_per_million / .output_per_million
        self.pricing = pricing or {}

    # --- key helpers -----------------------------------------------------
    def _rpm_key(self, uid: str) -> str:
        return f"gov:rpm:{uid}"

    def _conc_key(self, uid: str) -> str:
        return f"gov:conc:{uid}"

    def _tpm_key(self, uid: str, now: float) -> str:
        bucket = int(now // self.window)
        return f"gov:tpm:{uid}:{bucket}"

    def _tok_budget_key(self, uid: str, period: str, now_dt: _dt.datetime) -> str:
        stamp = now_dt.strftime("%Y%m%d") if period == "d" else now_dt.strftime("%Y%m")
        return f"gov:tok:{period}:{uid}:{stamp}"

    def _cost_budget_key(self, uid: str, period: str, now_dt: _dt.datetime) -> str:
        stamp = now_dt.strftime("%Y%m%d") if period == "d" else now_dt.strftime("%Y%m")
        return f"gov:cost:{period}:{uid}:{stamp}"

    # --- pricing ---------------------------------------------------------
    def cost_micro_usd(self, model: str | None, prompt_tokens: int, completion_tokens: int) -> int:
        """Cost of a request in integer micro-USD, or 0 if the model is unpriced."""
        if not model:
            return 0
        p = self.pricing.get(model)
        if p is None:
            return 0
        inp = float(getattr(p, "input_per_million", 0.0) or 0.0)
        out = float(getattr(p, "output_per_million", 0.0) or 0.0)
        # micro_usd = usd * 1e6; usd = tokens/1e6 * per_million → the 1e6 cancels.
        return int(round(prompt_tokens * inp + completion_tokens * out))

    # --- admission -------------------------------------------------------
    @staticmethod
    def _uid(principal: Principal) -> str:
        """Counter id for a LIMITED principal.

        Only reached after the unlimited early-return, and every limited principal
        is a virtual key with a stable id. Fail closed if that invariant is ever
        broken rather than metering an anonymous ``None`` bucket shared by callers.
        """
        if principal.usage_id is None:
            raise RuntimeError("limited principal has no usage_id; refusing to meter it")
        return principal.usage_id

    async def admit(self, principal: Principal, est_tokens: int = 0) -> Admission:
        """Reserve capacity and check rpm/tpm/budgets BEFORE dispatch.

        Raises :class:`RateLimitExceededError` (429 ``rate_limit_exceeded``) or
        :class:`InsufficientQuotaError` (429 ``insufficient_quota``) if a limit is
        hit. On success returns headers to attach to the response plus a
        :class:`Reservation` to pass to :meth:`reconcile`.
        """
        lim = principal.limits
        if principal.unlimited or not lim.any_limit:
            return Admission(headers={}, reservation=None)

        uid = self._uid(principal)
        now = time.time()
        now_dt = _utc_now()
        reserved_concurrency = False
        try:
            # 1) concurrency cap
            if lim.max_concurrency is not None:
                cur = await self.state.incr(self._conc_key(uid), 1)
                reserved_concurrency = True
                if cur > lim.max_concurrency:
                    await self.state.incr(self._conc_key(uid), -1)
                    reserved_concurrency = False
                    raise RateLimitExceededError(
                        f"concurrent request limit of {lim.max_concurrency} reached",
                        code="rate_limit_exceeded",
                        remediation="reduce in-flight requests or raise the key's max_concurrency",
                        headers={"retry-after": "1"},
                    )

            # 2) requests per minute (sliding-window log)
            if lim.rpm is not None:
                cnt = await self.state.window_count(self._rpm_key(uid), self.window, now=now)
                if cnt >= lim.rpm:
                    raise RateLimitExceededError(
                        f"request rate limit of {lim.rpm}/min reached",
                        code="rate_limit_exceeded",
                        remediation="slow down; see Retry-After and x-ratelimit headers",
                        headers=self._rate_headers(lim, cnt, None, retry_after=int(self.window)),
                    )
                await self.state.window_incr(self._rpm_key(uid), self.window, 1, now=now)

            # 3) tokens per minute (windowed counter) — pre-dispatch estimate gate
            if lim.tpm is not None:
                cur_tpm = await self.state.get_counter(self._tpm_key(uid, now))
                if cur_tpm + max(0, est_tokens) > lim.tpm:
                    raise RateLimitExceededError(
                        f"token rate limit of {lim.tpm}/min reached",
                        code="rate_limit_exceeded",
                        remediation="slow down or lower max_tokens; see x-ratelimit headers",
                        headers=self._rate_headers(
                            lim, None, cur_tpm, retry_after=int(self.window)
                        ),
                    )

            # 4) daily/monthly token + cost budgets (pre-dispatch)
            await self._check_budgets(principal, now_dt)

            headers = await self._headers(principal, now, now_dt)
            return Admission(
                headers=headers,
                reservation=Reservation(uid, lim, reserved_concurrency, now),
            )
        except Exception:
            if reserved_concurrency:
                await self.state.incr(self._conc_key(uid), -1)
            raise

    async def _check_budgets(self, principal: Principal, now_dt: _dt.datetime) -> None:
        lim = principal.limits
        uid = self._uid(principal)
        if lim.daily_token_budget is not None:
            spent = await self.state.get_counter(self._tok_budget_key(uid, "d", now_dt))
            if spent >= lim.daily_token_budget:
                raise InsufficientQuotaError(
                    f"daily token budget of {lim.daily_token_budget} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the daily reset or raise the budget",
                    headers={"retry-after": str(_seconds_to_midnight(now_dt))},
                )
        if lim.monthly_token_budget is not None:
            spent = await self.state.get_counter(self._tok_budget_key(uid, "m", now_dt))
            if spent >= lim.monthly_token_budget:
                raise InsufficientQuotaError(
                    f"monthly token budget of {lim.monthly_token_budget} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the monthly reset or raise the budget",
                    headers={"retry-after": str(_seconds_to_month_end(now_dt))},
                )
        if lim.daily_cost_usd is not None:
            spent = await self.state.get_counter(self._cost_budget_key(uid, "d", now_dt))
            if spent >= int(round(lim.daily_cost_usd * 1_000_000)):
                raise InsufficientQuotaError(
                    f"daily cost budget of ${lim.daily_cost_usd} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the daily reset or raise the cost budget",
                    headers={"retry-after": str(_seconds_to_midnight(now_dt))},
                )
        if lim.monthly_cost_usd is not None:
            spent = await self.state.get_counter(self._cost_budget_key(uid, "m", now_dt))
            if spent >= int(round(lim.monthly_cost_usd * 1_000_000)):
                raise InsufficientQuotaError(
                    f"monthly cost budget of ${lim.monthly_cost_usd} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the monthly reset or raise the cost budget",
                    headers={"retry-after": str(_seconds_to_month_end(now_dt))},
                )

    # --- reconcile -------------------------------------------------------
    async def reconcile(
        self,
        principal: Principal,
        reservation: Reservation | None,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
    ) -> None:
        """Charge ACTUAL usage into tpm/budget counters and release concurrency.

        Safe to call in a ``finally`` for both success and failure paths; a no-op
        for unlimited principals (``reservation is None``).
        """
        if reservation is None:
            return
        lim = reservation.limits
        uid = reservation.usage_id
        now = time.time()
        now_dt = _utc_now()
        total = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))

        # Release the concurrency slot first (always).
        if reservation.reserved_concurrency and lim.max_concurrency is not None:
            await self.state.incr(self._conc_key(uid), -1)

        if total > 0 and lim.tpm is not None:
            await self.state.incr(self._tpm_key(uid, now), total, ttl=self.window * 2)
        if total > 0 and lim.daily_token_budget is not None:
            await self.state.incr(
                self._tok_budget_key(uid, "d", now_dt), total, ttl=_seconds_to_midnight(now_dt) + 60
            )
        if total > 0 and lim.monthly_token_budget is not None:
            await self.state.incr(
                self._tok_budget_key(uid, "m", now_dt),
                total,
                ttl=_seconds_to_month_end(now_dt) + 60,
            )

        cost_micro = self.cost_micro_usd(model, prompt_tokens, completion_tokens)
        if cost_micro > 0 and lim.daily_cost_usd is not None:
            await self.state.incr(
                self._cost_budget_key(uid, "d", now_dt),
                cost_micro,
                ttl=_seconds_to_midnight(now_dt) + 60,
            )
        if cost_micro > 0 and lim.monthly_cost_usd is not None:
            await self.state.incr(
                self._cost_budget_key(uid, "m", now_dt),
                cost_micro,
                ttl=_seconds_to_month_end(now_dt) + 60,
            )

    # --- headers ---------------------------------------------------------
    def _rate_headers(
        self,
        lim: KeyLimits,
        rpm_count: int | None,
        tpm_count: int | None,
        *,
        retry_after: int | None = None,
    ) -> dict[str, str]:
        h: dict[str, str] = {}
        if retry_after is not None:
            h["retry-after"] = str(retry_after)
        if lim.rpm is not None:
            used = rpm_count if rpm_count is not None else 0
            h["x-ratelimit-limit-requests"] = str(lim.rpm)
            h["x-ratelimit-remaining-requests"] = str(max(0, lim.rpm - used))
            h["x-ratelimit-reset-requests"] = str(int(self.window))
        if lim.tpm is not None:
            used = tpm_count if tpm_count is not None else 0
            h["x-ratelimit-limit-tokens"] = str(lim.tpm)
            h["x-ratelimit-remaining-tokens"] = str(max(0, lim.tpm - used))
            h["x-ratelimit-reset-tokens"] = str(int(self.window))
        return h

    async def _headers(
        self, principal: Principal, now: float, now_dt: _dt.datetime
    ) -> dict[str, str]:
        lim = principal.limits
        uid = self._uid(principal)
        rpm_count = None
        tpm_count = None
        if lim.rpm is not None:
            rpm_count = await self.state.window_count(self._rpm_key(uid), self.window, now=now)
        if lim.tpm is not None:
            tpm_count = await self.state.get_counter(self._tpm_key(uid, now))
        return self._rate_headers(lim, rpm_count, tpm_count)
