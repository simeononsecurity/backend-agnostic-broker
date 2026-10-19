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
import math
import time
from dataclasses import dataclass
from typing import Any

from .errors import InsufficientQuotaError, RateLimitExceededError
from .keys import KeyLimits, Principal, TenantLimits
from .state import StateStore


@dataclass
class Reservation:
    """Handle returned by :meth:`Governor.admit`, consumed by :meth:`reconcile`."""

    usage_id: str
    limits: KeyLimits
    reserved_concurrency: bool
    now: float
    tenant: str | None = None
    tenant_limits: TenantLimits | None = None


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


def _ceil_pos(seconds: float) -> int:
    """Retry-After seconds: ceil of a positive float, at least 1."""
    return max(1, int(math.ceil(seconds)))


class Governor:
    """Enforces per-key limits on the ADR 0009 ``StateStore``."""

    def __init__(
        self,
        state: StateStore,
        *,
        window_seconds: float = 60.0,
        pricing: dict[str, Any] | None = None,
        algo: str = "sliding_log",
        tpm_algo: str = "fixed",
    ) -> None:
        self.state = state
        self.window = float(window_seconds)
        # model public name -> object with .input_per_million / .output_per_million
        self.pricing = pricing or {}
        # rpm algorithm: "sliding_log" (exact, default) | "gcra" (token bucket).
        self.algo = algo
        # tpm counter: "fixed" (windowed, default) | "sliding" (weighted 2-bucket).
        self.tpm_algo = tpm_algo

    # --- key helpers -----------------------------------------------------
    def _rpm_key(self, uid: str) -> str:
        return f"gov:rpm:{uid}"

    def _conc_key(self, uid: str) -> str:
        return f"gov:conc:{uid}"

    def _tpm_key(self, uid: str, now: float) -> str:
        bucket = int(now // self.window)
        return f"gov:tpm:{uid}:{bucket}"

    def _tpm_sw_keys(self, uid: str, now: float) -> tuple[str, str, float]:
        """Sliding-window-counter keys: (current_bucket, previous_bucket, prev_weight).

        ``prev_weight`` is the fraction of the previous window still inside the
        rolling window — the Cloudflare/Kong weighted-counter estimate.
        """
        bucket = int(now // self.window)
        progress = (now - bucket * self.window) / self.window
        prev_weight = max(0.0, 1.0 - progress)
        return (f"gov:tpmsw:{uid}:{bucket}", f"gov:tpmsw:{uid}:{bucket - 1}", prev_weight)

    async def _tpm_estimate(self, uid: str, now: float) -> float:
        """Current tpm usage estimate under the configured tpm algorithm."""
        if self.tpm_algo == "sliding":
            cur_k, prev_k, w = self._tpm_sw_keys(uid, now)
            cur = await self.state.get_counter(cur_k)
            prev = await self.state.get_counter(prev_k)
            return cur + prev * w
        return float(await self.state.get_counter(self._tpm_key(uid, now)))

    async def _tpm_charge(self, uid: str, total: int, now: float) -> None:
        if self.tpm_algo == "sliding":
            cur_k, _prev_k, _w = self._tpm_sw_keys(uid, now)
            await self.state.incr(cur_k, total, ttl=self.window * 2)
        else:
            await self.state.incr(self._tpm_key(uid, now), total, ttl=self.window * 2)

    def _model_rpm_key(self, uid: str, model: str) -> str:
        return f"gov:mrpm:{uid}:{model}"

    def _model_tpm_key(self, uid: str, model: str, now: float) -> str:
        bucket = int(now // self.window)
        return f"gov:mtpm:{uid}:{model}:{bucket}"

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
        metered = (not principal.unlimited) and lim.any_limit
        tenant_gov = principal.tenant_governed
        if not metered and not tenant_gov:
            return Admission(headers={}, reservation=None)

        uid = self._uid(principal)
        now = time.time()
        now_dt = _utc_now()
        reserved_concurrency = False
        rpm_remaining: int | None = None
        try:
            # 1) concurrency cap
            if metered and lim.max_concurrency is not None:
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

            # 2) requests per minute — sliding-window log (default) or GCRA.
            if metered and lim.rpm is not None:
                if self.algo == "gcra":
                    emission = self.window / lim.rpm
                    burst = lim.rpm_burst or lim.rpm
                    res = await self.state.gcra(
                        self._rpm_key(uid), emission_interval=emission, burst=burst, now=now
                    )
                    if not res.allowed:
                        raise RateLimitExceededError(
                            f"request rate limit of {lim.rpm}/min reached",
                            code="rate_limit_exceeded",
                            remediation="slow down; see Retry-After and x-ratelimit headers",
                            headers=self._rate_headers(
                                lim, lim.rpm, None, retry_after=_ceil_pos(res.retry_after)
                            ),
                        )
                    rpm_remaining = res.remaining
                else:
                    cnt = await self.state.window_count(self._rpm_key(uid), self.window, now=now)
                    if cnt >= lim.rpm:
                        raise RateLimitExceededError(
                            f"request rate limit of {lim.rpm}/min reached",
                            code="rate_limit_exceeded",
                            remediation="slow down; see Retry-After and x-ratelimit headers",
                            headers=self._rate_headers(
                                lim, cnt, None, retry_after=int(self.window)
                            ),
                        )
                    await self.state.window_incr(self._rpm_key(uid), self.window, 1, now=now)

            # 3) tokens per minute — fixed (default) or sliding-window counter.
            if metered and lim.tpm is not None:
                cur_tpm = await self._tpm_estimate(uid, now)
                if cur_tpm + max(0, est_tokens) > lim.tpm:
                    raise RateLimitExceededError(
                        f"token rate limit of {lim.tpm}/min reached",
                        code="rate_limit_exceeded",
                        remediation="slow down or lower max_tokens; see x-ratelimit headers",
                        headers=self._rate_headers(
                            lim, None, int(cur_tpm), retry_after=int(self.window)
                        ),
                    )

            # 4) daily/monthly token + cost budgets (pre-dispatch): per-key then tenant.
            if metered:
                await self._check_budgets(uid, lim, now_dt)
            if tenant_gov:
                assert principal.tenant is not None and principal.tenant_limits is not None
                await self._check_tenant_budgets(principal.tenant, principal.tenant_limits, now_dt)

            headers = await self._headers(principal, now, now_dt, rpm_remaining=rpm_remaining)
            return Admission(
                headers=headers,
                reservation=Reservation(
                    uid,
                    lim,
                    reserved_concurrency,
                    now,
                    tenant=principal.tenant if tenant_gov else None,
                    tenant_limits=principal.tenant_limits if tenant_gov else None,
                ),
            )
        except Exception:
            if reserved_concurrency:
                await self.state.incr(self._conc_key(uid), -1)
            raise

    async def _check_budgets(self, uid: str, lim: KeyLimits, now_dt: _dt.datetime) -> None:
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

    @staticmethod
    def _tenant_uid(tenant: str) -> str:
        return f"t:{tenant}"

    async def _check_tenant_budgets(
        self, tenant: str, tl: TenantLimits, now_dt: _dt.datetime
    ) -> None:
        """Aggregate tenant token/cost budgets, enforced across all keys of a tenant."""
        uid = self._tenant_uid(tenant)
        if tl.daily_token_budget is not None:
            spent = await self.state.get_counter(self._tok_budget_key(uid, "d", now_dt))
            if spent >= tl.daily_token_budget:
                raise InsufficientQuotaError(
                    f"tenant {tenant!r} daily token budget of {tl.daily_token_budget} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the daily reset or raise the tenant budget",
                    headers={"retry-after": str(_seconds_to_midnight(now_dt))},
                )
        if tl.monthly_token_budget is not None:
            spent = await self.state.get_counter(self._tok_budget_key(uid, "m", now_dt))
            if spent >= tl.monthly_token_budget:
                raise InsufficientQuotaError(
                    f"tenant {tenant!r} monthly token budget of {tl.monthly_token_budget} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the monthly reset or raise the tenant budget",
                    headers={"retry-after": str(_seconds_to_month_end(now_dt))},
                )
        if tl.daily_cost_usd is not None:
            spent = await self.state.get_counter(self._cost_budget_key(uid, "d", now_dt))
            if spent >= int(round(tl.daily_cost_usd * 1_000_000)):
                raise InsufficientQuotaError(
                    f"tenant {tenant!r} daily cost budget of ${tl.daily_cost_usd} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the daily reset or raise the tenant cost budget",
                    headers={"retry-after": str(_seconds_to_midnight(now_dt))},
                )
        if tl.monthly_cost_usd is not None:
            spent = await self.state.get_counter(self._cost_budget_key(uid, "m", now_dt))
            if spent >= int(round(tl.monthly_cost_usd * 1_000_000)):
                raise InsufficientQuotaError(
                    f"tenant {tenant!r} monthly cost budget of ${tl.monthly_cost_usd} exhausted",
                    code="insufficient_quota",
                    remediation="wait for the monthly reset or raise the tenant cost budget",
                    headers={"retry-after": str(_seconds_to_month_end(now_dt))},
                )

    # --- per-model limits (ADR 0016) -------------------------------------
    async def check_model(self, principal: Principal, model: str, est_tokens: int = 0) -> None:
        """Enforce the key's per-model rpm/tpm for ``model`` (pre-dispatch).

        Called by the manager at *each* resolved hop (auto pick + every cascade
        hop), so a per-model cap cannot be bypassed via ``auto`` or an escalation.
        A no-op when the key has no per-model entry for this model. rpm is consumed
        here; tpm is a pre-dispatch estimate gate reconciled against actual usage in
        :meth:`reconcile_model`.
        """
        pm = principal.limits.per_model.get(model)
        if pm is None or principal.usage_id is None:
            return
        uid = principal.usage_id
        now = time.time()
        if pm.rpm is not None:
            if self.algo == "gcra":
                emission = self.window / pm.rpm
                burst = pm.rpm_burst or pm.rpm
                res = await self.state.gcra(
                    self._model_rpm_key(uid, model),
                    emission_interval=emission,
                    burst=burst,
                    now=now,
                )
                if not res.allowed:
                    raise RateLimitExceededError(
                        f"request rate limit of {pm.rpm}/min for model {model!r} reached",
                        code="rate_limit_exceeded",
                        remediation="slow down or use another model; see Retry-After",
                        headers={"retry-after": str(_ceil_pos(res.retry_after))},
                    )
            else:
                cnt = await self.state.window_count(
                    self._model_rpm_key(uid, model), self.window, now=now
                )
                if cnt >= pm.rpm:
                    raise RateLimitExceededError(
                        f"request rate limit of {pm.rpm}/min for model {model!r} reached",
                        code="rate_limit_exceeded",
                        remediation="slow down or use another model; see Retry-After",
                        headers={"retry-after": str(int(self.window))},
                    )
                await self.state.window_incr(
                    self._model_rpm_key(uid, model), self.window, 1, now=now
                )
        if pm.tpm is not None:
            cur = await self.state.get_counter(self._model_tpm_key(uid, model, now))
            if cur + max(0, est_tokens) > pm.tpm:
                raise RateLimitExceededError(
                    f"token rate limit of {pm.tpm}/min for model {model!r} reached",
                    code="rate_limit_exceeded",
                    remediation="slow down or lower max_tokens for this model",
                    headers={"retry-after": str(int(self.window))},
                )

    async def reconcile_model(
        self,
        principal: Principal,
        model: str | None,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Charge actual tokens into the per-model tpm counter for ``model``."""
        if model is None or principal.usage_id is None:
            return
        pm = principal.limits.per_model.get(model)
        if pm is None or pm.tpm is None:
            return
        total = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
        if total > 0:
            now = time.time()
            await self.state.incr(
                self._model_tpm_key(principal.usage_id, model, now), total, ttl=self.window * 2
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
            await self._tpm_charge(uid, total, now)
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

        # Tenant aggregate budgets: charge the same actual usage to the tenant.
        tl = reservation.tenant_limits
        if reservation.tenant is not None and tl is not None:
            tuid = self._tenant_uid(reservation.tenant)
            if total > 0 and tl.daily_token_budget is not None:
                await self.state.incr(
                    self._tok_budget_key(tuid, "d", now_dt),
                    total,
                    ttl=_seconds_to_midnight(now_dt) + 60,
                )
            if total > 0 and tl.monthly_token_budget is not None:
                await self.state.incr(
                    self._tok_budget_key(tuid, "m", now_dt),
                    total,
                    ttl=_seconds_to_month_end(now_dt) + 60,
                )
            if cost_micro > 0 and tl.daily_cost_usd is not None:
                await self.state.incr(
                    self._cost_budget_key(tuid, "d", now_dt),
                    cost_micro,
                    ttl=_seconds_to_midnight(now_dt) + 60,
                )
            if cost_micro > 0 and tl.monthly_cost_usd is not None:
                await self.state.incr(
                    self._cost_budget_key(tuid, "m", now_dt),
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
        self,
        principal: Principal,
        now: float,
        now_dt: _dt.datetime,
        *,
        rpm_remaining: int | None = None,
    ) -> dict[str, str]:
        lim = principal.limits
        uid = self._uid(principal)
        h: dict[str, str] = {}
        if lim.rpm is not None:
            if rpm_remaining is not None:
                remaining = rpm_remaining
            elif self.algo == "gcra":
                remaining = lim.rpm
            else:
                cnt = await self.state.window_count(self._rpm_key(uid), self.window, now=now)
                remaining = max(0, lim.rpm - cnt)
            h["x-ratelimit-limit-requests"] = str(lim.rpm)
            h["x-ratelimit-remaining-requests"] = str(max(0, remaining))
            h["x-ratelimit-reset-requests"] = str(int(self.window))
        if lim.tpm is not None:
            used = await self._tpm_estimate(uid, now)
            h["x-ratelimit-limit-tokens"] = str(lim.tpm)
            h["x-ratelimit-remaining-tokens"] = str(max(0, lim.tpm - int(used)))
            h["x-ratelimit-reset-tokens"] = str(int(self.window))
        return h
