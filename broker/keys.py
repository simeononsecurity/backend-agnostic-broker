"""Virtual API keys: hashed-at-rest key store + CLI (ADR 0008).

A *virtual key* gives a caller an identity (id, label, tenant, scope) and a set
of governance limits (rpm/tpm/concurrency/budgets/allowed_models). The secret is
shown **exactly once** at creation; only a **SHA-256 hash** is ever written to
disk (the Stripe/AWS "irretrievable" model). A non-secret key id is embedded in
the secret for O(1) lookup, and verification is **constant-time**
(`hmac.compare_digest`). Unknown, disabled, expired, and wrong-secret keys all
fail with the *same* generic error so valid keys cannot be enumerated.

Secret format: ``brk_<keyid>_<random>`` where ``keyid`` is 8 hex chars and
``random`` is 48 hex chars from ``secrets``. The stored display ``prefix`` is
``brk_<keyid>`` (non-secret, safe to show in dashboards/logs).

Backward compatibility: flat ``BROKER_API_KEYS`` keep working and resolve to an
**unlimited user** principal, so enabling virtual keys is purely additive.

Keys live in a git-ignored ``keys.yaml`` (``BROKER_KEYS_PATH``); a committed
``keys.example.yaml`` documents the schema with fake values. Manage them with::

    python -m broker.keys create --label team-alpha --tenant alpha --rpm 60 --tpm 100000
    python -m broker.keys list
    python -m broker.keys revoke brk_1a2b3c4d
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

_SECRET_PREFIX = "brk"
# A fixed dummy hash to compare against when no record matches, so verification
# takes the same work whether or not the key id exists (no timing oracle).
_DUMMY_HASH = hashlib.sha256(b"broker-nonexistent-key").hexdigest()
# Upper bound on a presented secret; anything longer can't be a real key and is
# rejected early (cheap guard against pathological inputs).
_MAX_PRESENTED_LEN = 8192


def hash_secret(secret: str) -> str:
    """Return the hex SHA-256 of a key secret (what we store; never the secret).

    Uses ``surrogateescape`` so a secret carrying raw non-ASCII/latin-1 bytes
    hashes deterministically instead of raising.
    """
    return hashlib.sha256(secret.encode("utf-8", "surrogateescape")).hexdigest()


def _ct_equal(a: str, b: str) -> bool:
    """Constant-time equality for two secrets, safe for non-ASCII input.

    ``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str`` (CWE-208
    hardening only covers ASCII str or bytes). Comparing the UTF-8/surrogateescape
    byte encodings avoids that, so a non-ASCII or malformed presented key simply
    fails to match (→ 401) instead of raising an unhandled error (→ 500).
    """
    try:
        return hmac.compare_digest(
            a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape")
        )
    except Exception:  # noqa: BLE001 - any encoding failure means "not a match"
        return False


def generate_secret() -> tuple[str, str]:
    """Return ``(key_id, secret)``. The secret embeds the key id for lookup."""
    key_id = secrets.token_hex(4)  # 8 hex chars, non-secret lookup id
    secret = f"{_SECRET_PREFIX}_{key_id}_{secrets.token_hex(24)}"
    return key_id, secret


def _key_id_from_secret(secret: str) -> str | None:
    """Extract the embedded key id from a presented secret, or None if malformed."""
    parts = secret.split("_")
    if len(parts) != 3 or parts[0] != _SECRET_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1]


@dataclass
class PerModelLimit:
    """Per-model rpm/tpm override for a single key (ADR 0016).

    Applied to the *resolved* model (the ``auto`` router's pick and every cascade
    hop), so a model-scoped cap cannot be bypassed via ``auto`` or an escalation.
    ``None`` on an axis means unlimited for that model on that axis.
    """

    rpm: int | None = None
    tpm: int | None = None
    rpm_burst: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> PerModelLimit:
        d = d or {}
        return cls(rpm=d.get("rpm"), tpm=d.get("tpm"), rpm_burst=d.get("rpm_burst"))

    def to_dict(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in {"rpm": self.rpm, "tpm": self.tpm, "rpm_burst": self.rpm_burst}.items()
            if v is not None
        }


@dataclass
class TenantLimits:
    """Aggregate token/cost budgets shared by every key of a tenant (ADR 0016).

    Enforced *in addition* to per-key budgets (LiteLLM's org>team>user>key model: a
    request must pass every budget that applies). Only budgets live here — rpm/tpm
    stay per-key.
    """

    daily_token_budget: int | None = None
    monthly_token_budget: int | None = None
    daily_cost_usd: float | None = None
    monthly_cost_usd: float | None = None

    @property
    def any_budget(self) -> bool:
        return any(
            v is not None
            for v in (
                self.daily_token_budget,
                self.monthly_token_budget,
                self.daily_cost_usd,
                self.monthly_cost_usd,
            )
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TenantLimits:
        d = d or {}
        return cls(
            daily_token_budget=d.get("daily_token_budget"),
            monthly_token_budget=d.get("monthly_token_budget"),
            daily_cost_usd=d.get("daily_cost_usd"),
            monthly_cost_usd=d.get("monthly_cost_usd"),
        )


@dataclass
class KeyLimits:
    """Per-key governance limits. ``None``/empty means *unlimited* for that axis."""

    rpm: int | None = None
    tpm: int | None = None
    rpm_burst: int | None = None  # GCRA burst capacity (ADR 0016); default = rpm
    max_concurrency: int | None = None
    daily_token_budget: int | None = None
    monthly_token_budget: int | None = None
    daily_cost_usd: float | None = None
    monthly_cost_usd: float | None = None
    allowed_models: list[str] = field(default_factory=list)
    placement: list[str] = field(default_factory=list)  # ADR 0016 per-key placement
    per_model: dict[str, PerModelLimit] = field(default_factory=dict)  # ADR 0016

    @property
    def any_limit(self) -> bool:
        """True if any *metered* (counter-backed) per-key limit is set.

        ``placement`` and ``per_model`` are enforced on their own paths (placement
        machinery / model guard), independently of the global admission fast-path,
        so they deliberately do not toggle this.
        """
        return any(
            v is not None
            for v in (
                self.rpm,
                self.tpm,
                self.max_concurrency,
                self.daily_token_budget,
                self.monthly_token_budget,
                self.daily_cost_usd,
                self.monthly_cost_usd,
            )
        ) or bool(self.allowed_models)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> KeyLimits:
        d = d or {}
        per_model = {
            str(k): PerModelLimit.from_dict(v) for k, v in (d.get("per_model") or {}).items()
        }
        return cls(
            rpm=d.get("rpm"),
            tpm=d.get("tpm"),
            rpm_burst=d.get("rpm_burst"),
            max_concurrency=d.get("max_concurrency"),
            daily_token_budget=d.get("daily_token_budget"),
            monthly_token_budget=d.get("monthly_token_budget"),
            daily_cost_usd=d.get("daily_cost_usd"),
            monthly_cost_usd=d.get("monthly_cost_usd"),
            allowed_models=list(d.get("allowed_models") or []),
            placement=list(d.get("placement") or []),
            per_model=per_model,
        )


@dataclass
class Principal:
    """The authenticated caller identity passed to the governor.

    ``usage_id`` is the stable, non-secret id recorded in the usage log and used
    to key rate/budget counters. ``unlimited`` principals (legacy
    ``BROKER_API_KEYS`` or virtual keys with no limits) bypass governance.
    """

    usage_id: str | None
    scope: str = "user"
    tenant: str | None = None
    label: str | None = None
    limits: KeyLimits = field(default_factory=KeyLimits)
    unlimited: bool = False
    tenant_limits: TenantLimits | None = None  # ADR 0016: aggregate tenant budgets

    @property
    def is_admin(self) -> bool:
        return self.scope == "admin"

    @property
    def tenant_governed(self) -> bool:
        """True if this principal's tenant carries an aggregate budget to enforce."""
        return self.tenant_limits is not None and self.tenant_limits.any_budget


@dataclass
class KeyRecord:
    """A stored virtual-key record. Holds the secret *hash*, never the secret."""

    id: str
    secret_sha256: str
    label: str | None = None
    tenant: str | None = None
    scope: str = "user"
    enabled: bool = True
    expiry: str | None = None  # ISO date "YYYY-MM-DD" (UTC), or None = never
    limits: KeyLimits = field(default_factory=KeyLimits)

    @property
    def prefix(self) -> str:
        return f"{_SECRET_PREFIX}_{self.id}"

    def is_expired(self, today: _dt.date | None = None) -> bool:
        if not self.expiry:
            return False
        today = today or _dt.datetime.now(_dt.UTC).date()
        try:
            exp = _dt.date.fromisoformat(str(self.expiry))
        except ValueError:
            # A malformed expiry is treated as expired (fail closed).
            return True
        return today > exp

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KeyRecord:
        if not d.get("id") or not d.get("secret_sha256"):
            raise ConfigError(
                "each key needs an 'id' and 'secret_sha256'",
                remediation="regenerate with: python -m broker.keys create",
            )
        scope = d.get("scope", "user")
        if scope not in ("user", "admin"):
            raise ConfigError(f"invalid key scope {scope!r}", remediation="use 'user' or 'admin'")
        return cls(
            id=str(d["id"]),
            secret_sha256=str(d["secret_sha256"]),
            label=d.get("label"),
            tenant=d.get("tenant"),
            scope=scope,
            enabled=bool(d.get("enabled", True)),
            expiry=d.get("expiry"),
            limits=KeyLimits.from_dict(d.get("limits")),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "tenant": self.tenant,
            "scope": self.scope,
            "secret_sha256": self.secret_sha256,
            "enabled": self.enabled,
            "expiry": self.expiry,
        }
        lim = {
            k: v
            for k, v in {
                "rpm": self.limits.rpm,
                "tpm": self.limits.tpm,
                "rpm_burst": self.limits.rpm_burst,
                "max_concurrency": self.limits.max_concurrency,
                "daily_token_budget": self.limits.daily_token_budget,
                "monthly_token_budget": self.limits.monthly_token_budget,
                "daily_cost_usd": self.limits.daily_cost_usd,
                "monthly_cost_usd": self.limits.monthly_cost_usd,
                "allowed_models": self.limits.allowed_models or None,
                "placement": self.limits.placement or None,
                "per_model": (
                    {m: pm.to_dict() for m, pm in self.limits.per_model.items()}
                    if self.limits.per_model
                    else None
                ),
            }.items()
            if v is not None
        }
        if lim:
            out["limits"] = lim
        return out

    def principal(self) -> Principal:
        return Principal(
            usage_id=self.prefix,
            scope=self.scope,
            tenant=self.tenant,
            label=self.label,
            limits=self.limits,
            unlimited=not self.limits.any_limit,
        )


class KeyStore:
    """Loads virtual keys and resolves a presented secret to a :class:`Principal`.

    Legacy ``BROKER_API_KEYS`` are accepted as unlimited user keys. All lookups
    are constant-time and return ``None`` (→ a single generic auth error at the
    call site) for any missing/invalid/revoked/expired/disabled key.
    """

    def __init__(self, keys_path: str | None, legacy_api_keys: list[str] | None = None) -> None:
        self.keys_path = keys_path
        self.legacy_api_keys = list(legacy_api_keys or [])
        self.records: dict[str, KeyRecord] = {}
        self.tenants: dict[str, TenantLimits] = {}
        if keys_path and Path(keys_path).exists():
            self.records, self.tenants = self._load(keys_path)

    def reload(self) -> None:
        """Re-read the keys file into memory (after a mutation via the admin API)."""
        if self.keys_path and Path(self.keys_path).exists():
            self.records, self.tenants = self._load(self.keys_path)
        else:
            self.records, self.tenants = {}, {}

    @staticmethod
    def _load(path: str) -> tuple[dict[str, KeyRecord], dict[str, TenantLimits]]:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(
                f"failed to parse keys file {path!r}: {exc}",
                remediation="fix the keys.yaml syntax (see keys.example.yaml)",
            ) from exc
        items = data.get("keys") if isinstance(data, dict) else None
        if items is None:
            raise ConfigError(
                "keys file must be a mapping with a top-level 'keys' list",
                remediation="see keys.example.yaml",
            )
        out: dict[str, KeyRecord] = {}
        for item in items:
            rec = KeyRecord.from_dict(item)
            if rec.id in out:
                raise ConfigError(f"duplicate key id in keys file: {rec.id!r}")
            out[rec.id] = rec
        tenants: dict[str, TenantLimits] = {}
        raw_tenants = data.get("tenants") if isinstance(data, dict) else None
        if raw_tenants:
            if not isinstance(raw_tenants, dict):
                raise ConfigError(
                    "keys file 'tenants' must be a mapping of tenant -> budgets",
                    remediation="see keys.example.yaml",
                )
            tenants = {str(name): TenantLimits.from_dict(v) for name, v in raw_tenants.items()}
        return out, tenants

    @property
    def any_configured(self) -> bool:
        return bool(self.records) or bool(self.legacy_api_keys)

    @property
    def governance_active(self) -> bool:
        """True if any virtual key declares a limit (so counters are needed)."""
        return any(r.limits.any_limit for r in self.records.values()) or any(
            t.any_budget for t in self.tenants.values()
        )

    def _attach_tenant(self, principal: Principal) -> Principal:
        if principal.tenant is not None:
            principal.tenant_limits = self.tenants.get(principal.tenant)
        return principal

    def resolve(self, presented: str | None) -> Principal | None:
        """Return the :class:`Principal` for a presented secret, or ``None``.

        Virtual keys are matched first (by embedded id + constant-time hash
        compare), then legacy flat keys (constant-time). A ``None`` result means
        the caller raises one generic ``AuthError`` — no information about *why*
        leaks to the client.
        """
        if not presented:
            return None
        presented = presented.strip()
        if not presented or len(presented) > _MAX_PRESENTED_LEN:
            return None

        # --- virtual keys ---
        key_id = _key_id_from_secret(presented)
        rec = self.records.get(key_id) if key_id else None
        try:
            presented_hash = hash_secret(presented)
        except Exception:  # noqa: BLE001 - malformed secret can't match a real key
            presented_hash = _DUMMY_HASH
        # Always run a constant-time compare (against a dummy if no record) so the
        # response time does not reveal whether the key id exists. Both operands
        # are hex digests (ASCII), so compare_digest is safe here.
        target_hash = rec.secret_sha256 if rec is not None else _DUMMY_HASH
        match = hmac.compare_digest(presented_hash, target_hash)
        if rec is not None and match and rec.enabled and not rec.is_expired():
            return self._attach_tenant(rec.principal())

        # --- legacy flat keys (unlimited user) ---
        # Byte-wise constant-time compare: a non-ASCII presented key must fail to
        # match (→ generic 401), never raise (which would surface as a 500 on the
        # unauthenticated-reachable auth path).
        legacy_hit = any(_ct_equal(presented, k) for k in self.legacy_api_keys)
        if legacy_hit:
            # Stable, non-reversible usage id so /usage can scope legacy keys too.
            fp = hash_secret(presented)[:12]
            return Principal(usage_id=f"legacy_{fp}", scope="user", unlimited=True)
        return None

    # --- mutation (admin key-management API, ADR 0016) -------------------
    def _require_writable(self) -> str:
        if not self.keys_path:
            raise ConfigError(
                "key management requires a keys file",
                remediation="set BROKER_KEYS_PATH to a writable keys.yaml",
            )
        return self.keys_path

    def create_key(
        self,
        *,
        label: str | None = None,
        tenant: str | None = None,
        scope: str = "user",
        limits: KeyLimits | None = None,
        expiry: str | None = None,
    ) -> tuple[KeyRecord, str]:
        """Mint a new key, persist only its hash, and return ``(record, secret)``.

        The plaintext ``secret`` is returned to the caller **once** and never
        stored; persistence writes the SHA-256 hash only.
        """
        path = self._require_writable()
        data = _read_raw(path)
        existing = {str(k.get("id")) for k in data["keys"]}
        key_id, secret = generate_secret()
        while key_id in existing:  # vanishingly unlikely; still avoid a collision
            key_id, secret = generate_secret()
        rec = KeyRecord(
            id=key_id,
            secret_sha256=hash_secret(secret),
            label=label,
            tenant=tenant,
            scope=scope if scope in ("user", "admin") else "user",
            enabled=True,
            expiry=expiry,
            limits=limits or KeyLimits(),
        )
        data["keys"].append(rec.to_dict())
        _write_raw(path, data)
        self.reload()
        return rec, secret

    def revoke_key(self, key_id: str) -> bool:
        """Disable a key by id (``brk_xxxx`` or ``xxxx``). Returns True if found."""
        path = self._require_writable()
        target = key_id.split("_", 2)[1] if key_id.startswith(f"{_SECRET_PREFIX}_") else key_id
        data = _read_raw(path)
        found = False
        for item in data["keys"]:
            if str(item.get("id")) == target:
                item["enabled"] = False
                found = True
        if found:
            _write_raw(path, data)
            self.reload()
        return found

    def rotate_key(self, key_id: str, *, grace_days: int = 7) -> tuple[KeyRecord, str] | None:
        """Mint a successor inheriting the predecessor's settings; return (rec, secret).

        The old key is kept valid for ``grace_days`` (its ``expiry`` is set to
        today+grace so both keys work during cutover); ``grace_days == 0`` revokes
        it immediately. Returns ``None`` if no such key exists.
        """
        path = self._require_writable()
        target = key_id.split("_", 2)[1] if key_id.startswith(f"{_SECRET_PREFIX}_") else key_id
        data = _read_raw(path)
        old_item = next((it for it in data["keys"] if str(it.get("id")) == target), None)
        if old_item is None:
            return None
        old = KeyRecord.from_dict(old_item)
        new_id, secret = generate_secret()
        new_rec = KeyRecord(
            id=new_id,
            secret_sha256=hash_secret(secret),
            label=old.label,
            tenant=old.tenant,
            scope=old.scope,
            enabled=True,
            expiry=None,
            limits=old.limits,
        )
        if grace_days <= 0:
            old_item["enabled"] = False
        else:
            grace = (_dt.datetime.now(_dt.UTC).date() + _dt.timedelta(days=grace_days)).isoformat()
            old_item["expiry"] = grace
        data["keys"].append(new_rec.to_dict())
        _write_raw(path, data)
        self.reload()
        return new_rec, secret

    def list_records(self) -> list[KeyRecord]:
        return list(self.records.values())


# --------------------------------------------------------------------------- #
# CLI: python -m broker.keys create|list|revoke
# --------------------------------------------------------------------------- #


def _default_path(explicit: str | None) -> str:
    return explicit or os.environ.get("BROKER_KEYS_PATH") or "keys.yaml"


def _read_raw(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"keys": []}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or "keys" not in data:
        data = {"keys": data.get("keys", []) if isinstance(data, dict) else []}
    if data.get("keys") is None:
        data["keys"] = []
    return data


def _write_raw(path: str, data: dict[str, Any]) -> None:
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _parse_per_model(specs: list[str] | None) -> dict[str, PerModelLimit]:
    """Parse repeated ``--per-model model:rpm:tpm`` (blank = unlimited axis)."""
    out: dict[str, PerModelLimit] = {}
    for spec in specs or []:
        parts = spec.split(":")
        if not parts or not parts[0].strip():
            raise ValueError(f"invalid --per-model {spec!r}; use model:rpm:tpm")
        model = parts[0].strip()
        rpm = int(parts[1]) if len(parts) > 1 and parts[1].strip() else None
        tpm = int(parts[2]) if len(parts) > 2 and parts[2].strip() else None
        out[model] = PerModelLimit(rpm=rpm, tpm=tpm)
    return out


def _limits_from_args(args: argparse.Namespace) -> KeyLimits:
    return KeyLimits(
        rpm=args.rpm,
        tpm=args.tpm,
        rpm_burst=getattr(args, "rpm_burst", None),
        max_concurrency=args.max_concurrency,
        daily_token_budget=args.daily_tokens,
        monthly_token_budget=args.monthly_tokens,
        daily_cost_usd=args.daily_cost,
        monthly_cost_usd=args.monthly_cost,
        allowed_models=[m.strip() for m in (args.allowed_models or "").split(",") if m.strip()],
        placement=[
            p.strip() for p in (getattr(args, "placement", None) or "").split(",") if p.strip()
        ],
        per_model=_parse_per_model(getattr(args, "per_model", None)),
    )


def _cmd_create(args: argparse.Namespace) -> int:
    path = _default_path(args.path)
    data = _read_raw(path)
    key_id, secret = generate_secret()
    limits = _limits_from_args(args)
    rec = KeyRecord(
        id=key_id,
        secret_sha256=hash_secret(secret),
        label=args.label,
        tenant=args.tenant,
        scope=args.scope,
        enabled=True,
        expiry=args.expiry,
        limits=limits,
    )
    data["keys"].append(rec.to_dict())
    _write_raw(path, data)
    # The secret is printed EXACTLY ONCE and never stored.
    print(f"Created key {rec.prefix} (scope={rec.scope}, tenant={rec.tenant or '-'})")
    print("Secret (shown once — store it now, it cannot be recovered):")
    print(f"  {secret}")
    print(f"Written to {path} (hash only).")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    path = _default_path(args.path)
    store = KeyStore(path)
    if not store.records:
        print(f"no virtual keys in {path}")
        return 0
    print(f"{'id':<16} {'scope':<6} {'tenant':<12} {'enabled':<8} {'expiry':<12} label")
    for rec in store.records.values():
        print(
            f"{rec.prefix:<16} {rec.scope:<6} {str(rec.tenant or '-'):<12} "
            f"{str(rec.enabled):<8} {str(rec.expiry or '-'):<12} {rec.label or '-'}"
        )
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    path = _default_path(args.path)
    data = _read_raw(path)
    target = args.id
    if target.startswith(f"{_SECRET_PREFIX}_"):
        target = target.split("_", 2)[1]
    found = False
    for item in data["keys"]:
        if str(item.get("id")) == target:
            item["enabled"] = False
            found = True
    if not found:
        print(f"no key with id {args.id!r} in {path}", file=sys.stderr)
        return 1
    _write_raw(path, data)
    print(f"revoked key {args.id} (enabled=false)")
    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    path = _default_path(args.path)
    store = KeyStore(path)
    result = store.rotate_key(args.id, grace_days=args.grace_days)
    if result is None:
        print(f"no key with id {args.id!r} in {path}", file=sys.stderr)
        return 1
    new_rec, secret = result
    old_state = "revoked now" if args.grace_days <= 0 else f"valid for {args.grace_days}d (grace)"
    print(f"Rotated {args.id} -> {new_rec.prefix}; predecessor {old_state}.")
    print("New secret (shown once — store it now, it cannot be recovered):")
    print(f"  {secret}")
    print(f"Written to {path} (hash only).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m broker.keys", description="Manage virtual API keys")
    p.add_argument("--path", help="keys file (default: $BROKER_KEYS_PATH or keys.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a key and print its secret once")
    c.add_argument("--label")
    c.add_argument("--tenant")
    c.add_argument("--scope", choices=["user", "admin"], default="user")
    c.add_argument("--rpm", type=int)
    c.add_argument("--tpm", type=int)
    c.add_argument(
        "--rpm-burst", type=int, dest="rpm_burst", help="GCRA burst capacity (default=rpm)"
    )
    c.add_argument("--max-concurrency", type=int, dest="max_concurrency")
    c.add_argument("--daily-tokens", type=int, dest="daily_tokens")
    c.add_argument("--monthly-tokens", type=int, dest="monthly_tokens")
    c.add_argument("--daily-cost", type=float, dest="daily_cost", help="USD/day cost budget")
    c.add_argument("--monthly-cost", type=float, dest="monthly_cost", help="USD/month cost budget")
    c.add_argument(
        "--allowed-models", dest="allowed_models", help="comma-separated model allow-list"
    )
    c.add_argument("--placement", help="comma-separated required placement labels for this key")
    c.add_argument(
        "--per-model",
        dest="per_model",
        action="append",
        help="per-model limit 'model:rpm:tpm' (repeatable; blank axis = unlimited)",
    )
    c.add_argument("--expiry", help="ISO date YYYY-MM-DD (UTC), or omit for never")
    c.set_defaults(func=_cmd_create)

    lst = sub.add_parser("list", help="list keys (never prints secrets or hashes)")
    lst.set_defaults(func=_cmd_list)

    rv = sub.add_parser("revoke", help="disable a key by id")
    rv.add_argument("id", help="key id (brk_xxxx or xxxx)")
    rv.set_defaults(func=_cmd_revoke)

    rot = sub.add_parser("rotate", help="mint a successor key with a grace overlap")
    rot.add_argument("id", help="key id to rotate (brk_xxxx or xxxx)")
    rot.add_argument(
        "--grace-days",
        type=int,
        dest="grace_days",
        default=7,
        help="days the old key stays valid (0 = revoke immediately); default 7",
    )
    rot.set_defaults(func=_cmd_rotate)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
