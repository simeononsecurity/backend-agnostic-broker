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
class KeyLimits:
    """Per-key governance limits. ``None``/empty means *unlimited* for that axis."""

    rpm: int | None = None
    tpm: int | None = None
    max_concurrency: int | None = None
    daily_token_budget: int | None = None
    monthly_token_budget: int | None = None
    daily_cost_usd: float | None = None
    monthly_cost_usd: float | None = None
    allowed_models: list[str] = field(default_factory=list)

    @property
    def any_limit(self) -> bool:
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
        return cls(
            rpm=d.get("rpm"),
            tpm=d.get("tpm"),
            max_concurrency=d.get("max_concurrency"),
            daily_token_budget=d.get("daily_token_budget"),
            monthly_token_budget=d.get("monthly_token_budget"),
            daily_cost_usd=d.get("daily_cost_usd"),
            monthly_cost_usd=d.get("monthly_cost_usd"),
            allowed_models=list(d.get("allowed_models") or []),
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

    @property
    def is_admin(self) -> bool:
        return self.scope == "admin"


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
                "max_concurrency": self.limits.max_concurrency,
                "daily_token_budget": self.limits.daily_token_budget,
                "monthly_token_budget": self.limits.monthly_token_budget,
                "daily_cost_usd": self.limits.daily_cost_usd,
                "monthly_cost_usd": self.limits.monthly_cost_usd,
                "allowed_models": self.limits.allowed_models or None,
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
        if keys_path and Path(keys_path).exists():
            self.records = self._load(keys_path)

    @staticmethod
    def _load(path: str) -> dict[str, KeyRecord]:
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
        return out

    @property
    def any_configured(self) -> bool:
        return bool(self.records) or bool(self.legacy_api_keys)

    @property
    def governance_active(self) -> bool:
        """True if any virtual key declares a limit (so counters are needed)."""
        return any(r.limits.any_limit for r in self.records.values())

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
            return rec.principal()

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


def _cmd_create(args: argparse.Namespace) -> int:
    path = _default_path(args.path)
    data = _read_raw(path)
    key_id, secret = generate_secret()
    limits = KeyLimits(
        rpm=args.rpm,
        tpm=args.tpm,
        max_concurrency=args.max_concurrency,
        daily_token_budget=args.daily_tokens,
        monthly_token_budget=args.monthly_tokens,
        daily_cost_usd=args.daily_cost,
        monthly_cost_usd=args.monthly_cost,
        allowed_models=[m.strip() for m in (args.allowed_models or "").split(",") if m.strip()],
    )
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
    c.add_argument("--max-concurrency", type=int, dest="max_concurrency")
    c.add_argument("--daily-tokens", type=int, dest="daily_tokens")
    c.add_argument("--monthly-tokens", type=int, dest="monthly_tokens")
    c.add_argument("--daily-cost", type=float, dest="daily_cost", help="USD/day cost budget")
    c.add_argument("--monthly-cost", type=float, dest="monthly_cost", help="USD/month cost budget")
    c.add_argument(
        "--allowed-models", dest="allowed_models", help="comma-separated model allow-list"
    )
    c.add_argument("--expiry", help="ISO date YYYY-MM-DD (UTC), or omit for never")
    c.set_defaults(func=_cmd_create)

    lst = sub.add_parser("list", help="list keys (never prints secrets or hashes)")
    lst.set_defaults(func=_cmd_list)

    rv = sub.add_parser("revoke", help="disable a key by id")
    rv.add_argument("id", help="key id (brk_xxxx or xxxx)")
    rv.set_defaults(func=_cmd_revoke)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
