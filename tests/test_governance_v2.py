"""ADR 0016 governance v2 tests: GCRA, sliding-window tpm, per-model limits,
tenant budgets, per-key placement, rotation, and the admin key API.

Unit tests run against BOTH state backends via the ``store`` fixture (in-process
always; Redis via ``fakeredis`` when the optional ``[redis]`` extra is present,
else skipped). HTTP tests drive the full request path through a ``TestClient``.
"""

from __future__ import annotations

import datetime as dt
import os
import stat
import textwrap
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from broker import keys as broker_keys
from broker.app import create_app
from broker.config import Settings
from broker.errors import (
    ConfigError,
    ConflictError,
    InsufficientQuotaError,
    RateLimitExceededError,
)
from broker.governance import Governor
from broker.keys import (
    KeyLimits,
    KeyRecord,
    KeyStore,
    PerModelLimit,
    Principal,
    TenantLimits,
    hash_secret,
)
from broker.state import InProcessStateStore, RedisStateStore

try:  # optional: only present in a venv with the [redis] extra + fakeredis
    import fakeredis.aioredis as _fakeredis_aio

    _HAVE_FAKEREDIS = True
except Exception:  # noqa: BLE001
    _HAVE_FAKEREDIS = False


@pytest.fixture(params=["in_process", "redis"])
async def store(request):
    if request.param == "redis":
        if not _HAVE_FAKEREDIS:
            pytest.skip("redis/fakeredis not installed (optional [redis] extra)")
        s = RedisStateStore("redis://fake", prefix="gov2", client=_fakeredis_aio.FakeRedis())
    else:
        s = InProcessStateStore()
    yield s
    await s.close()


def _principal(**limits) -> Principal:
    return Principal(usage_id="brk_unit0001", scope="user", limits=KeyLimits(**limits))


# --- GCRA primitive (StateStore.gcra) --------------------------------------


async def test_gcra_burst_then_reject(store):
    now = 10_000.0
    out = [
        (await store.gcra("b", emission_interval=1.0, burst=3, now=now)).allowed for _ in range(5)
    ]
    assert out == [True, True, True, False, False]


async def test_gcra_refill_after_wait(store):
    now = 20_000.0
    for _ in range(3):
        await store.gcra("r", emission_interval=1.0, burst=3, now=now)
    assert not (await store.gcra("r", emission_interval=1.0, burst=3, now=now)).allowed
    # Two emission intervals later, two units have refilled.
    assert (await store.gcra("r", emission_interval=1.0, burst=3, now=now + 2.0)).allowed
    assert (await store.gcra("r", emission_interval=1.0, burst=3, now=now + 2.0)).allowed
    assert not (await store.gcra("r", emission_interval=1.0, burst=3, now=now + 2.0)).allowed


async def test_gcra_retry_after_and_remaining(store):
    now = 30_000.0
    a = await store.gcra("rr", emission_interval=1.0, burst=2, now=now)
    assert a.allowed and a.remaining == 1
    await store.gcra("rr", emission_interval=1.0, burst=2, now=now)
    denied = await store.gcra("rr", emission_interval=1.0, burst=2, now=now)
    assert not denied.allowed
    assert 0 < denied.retry_after <= 1.0


async def test_gcra_cost_multi_unit(store):
    now = 40_000.0
    # A single cost=5 request drains a burst-5 bucket.
    a = await store.gcra("c", emission_interval=1.0, burst=5, cost=5, now=now)
    assert a.allowed and a.remaining == 0
    assert not (await store.gcra("c", emission_interval=1.0, burst=5, cost=1, now=now)).allowed


# --- Governor rpm with GCRA -------------------------------------------------


async def test_governor_rpm_gcra_burst_equals_rpm(store):
    gov = Governor(store, window_seconds=60, algo="gcra")
    p = _principal(rpm=3)
    for _ in range(3):
        await gov.admit(p)
    with pytest.raises(RateLimitExceededError) as ei:
        await gov.admit(p)
    assert ei.value.http_status == 429
    assert ei.value.headers.get("retry-after")


async def test_governor_rpm_gcra_custom_burst(store):
    gov = Governor(store, window_seconds=60, algo="gcra")
    p = _principal(rpm=60, rpm_burst=2)  # emission 1s, burst 2
    await gov.admit(p)
    await gov.admit(p)
    with pytest.raises(RateLimitExceededError):
        await gov.admit(p)


async def test_governor_rpm_gcra_headers(store):
    gov = Governor(store, window_seconds=60, algo="gcra")
    p = _principal(rpm=5)
    a = await gov.admit(p)
    assert a.headers["x-ratelimit-limit-requests"] == "5"
    assert int(a.headers["x-ratelimit-remaining-requests"]) == 4


# --- sliding-window tpm -----------------------------------------------------


async def test_tpm_sliding_estimate_gate(store):
    gov = Governor(store, window_seconds=60, tpm_algo="sliding")
    p = _principal(tpm=1000)
    a = await gov.admit(p, est_tokens=10)
    await gov.reconcile(p, a.reservation, prompt_tokens=500, completion_tokens=0)
    # 500 charged into the current bucket; a 400-token estimate still fits.
    await gov.admit(p, est_tokens=400)
    with pytest.raises(RateLimitExceededError):
        await gov.admit(p, est_tokens=600)


async def test_tpm_sliding_window_weighting(store):
    gov = Governor(store, window_seconds=60, tpm_algo="sliding")
    uid = "brk_sw0001"
    now = 60.0  # bucket boundary: bucket=1, previous bucket=0, progress=0
    _cur_k, prev_k, _w = gov._tpm_sw_keys(uid, now)
    await store.incr(prev_k, 1000)
    # At the start of the current window the previous bucket is fully weighted.
    assert await gov._tpm_estimate(uid, now) == pytest.approx(1000.0)
    # Halfway through, the previous bucket is weighted 0.5.
    assert await gov._tpm_estimate(uid, now + 30) == pytest.approx(500.0)


# --- per-model limits -------------------------------------------------------


async def test_per_model_rpm(store):
    gov = Governor(store, window_seconds=60)
    p = _principal(per_model={"echo": PerModelLimit(rpm=2)})
    await gov.check_model(p, "echo")
    await gov.check_model(p, "echo")
    with pytest.raises(RateLimitExceededError) as ei:
        await gov.check_model(p, "echo")
    assert "echo" in ei.value.message
    # A model with no per-model entry is unaffected.
    await gov.check_model(p, "other")


async def test_per_model_tpm_estimate_and_reconcile(store):
    gov = Governor(store, window_seconds=60)
    p = _principal(per_model={"echo": PerModelLimit(tpm=100)})
    await gov.check_model(p, "echo", est_tokens=10)
    await gov.reconcile_model(p, "echo", prompt_tokens=60, completion_tokens=30)  # 90
    with pytest.raises(RateLimitExceededError):
        await gov.check_model(p, "echo", est_tokens=20)  # 90 + 20 > 100


async def test_per_model_gcra(store):
    gov = Governor(store, window_seconds=60, algo="gcra")
    p = _principal(per_model={"echo": PerModelLimit(rpm=2)})
    await gov.check_model(p, "echo")
    await gov.check_model(p, "echo")
    with pytest.raises(RateLimitExceededError):
        await gov.check_model(p, "echo")


# --- tenant (hierarchical) budgets ------------------------------------------


async def test_tenant_budget_shared_across_two_keys(store):
    gov = Governor(store, window_seconds=60)
    tl = TenantLimits(daily_token_budget=1000)
    p1 = Principal(usage_id="brk_t1", tenant="acme", tenant_limits=tl, unlimited=True)
    p2 = Principal(usage_id="brk_t2", tenant="acme", tenant_limits=tl, unlimited=True)
    a1 = await gov.admit(p1)
    await gov.reconcile(p1, a1.reservation, prompt_tokens=600, completion_tokens=300)  # 900
    # key2 (same tenant) still admits (900 < 1000), then pushes the tenant to 1000.
    a2 = await gov.admit(p2)
    await gov.reconcile(p2, a2.reservation, prompt_tokens=100, completion_tokens=0)  # 1000
    with pytest.raises(InsufficientQuotaError) as ei:
        await gov.admit(p1)
    assert "tenant" in ei.value.message


async def test_tenant_cost_budget(store):
    from broker.registry import ModelPricing

    gov = Governor(
        store,
        window_seconds=60,
        pricing={"m": ModelPricing(input_per_million=1_000_000, output_per_million=1_000_000)},
    )
    tl = TenantLimits(daily_cost_usd=1.0)  # 1e6 micro-USD
    p = Principal(usage_id="brk_tc", tenant="acme", tenant_limits=tl, unlimited=True)
    a = await gov.admit(p)
    # 1 input + 0 output token @ $1/token = $1 -> exhausts the $1 tenant budget.
    await gov.reconcile(p, a.reservation, prompt_tokens=1, completion_tokens=0, model="m")
    with pytest.raises(InsufficientQuotaError):
        await gov.admit(p)


async def test_tenant_and_per_key_budget_both_enforced(store):
    gov = Governor(store, window_seconds=60)
    tl = TenantLimits(daily_token_budget=10_000)
    # Per-key budget (100) is tighter than the tenant budget (10_000).
    p = Principal(
        usage_id="brk_tk",
        tenant="acme",
        tenant_limits=tl,
        limits=KeyLimits(daily_token_budget=100),
    )
    a = await gov.admit(p)
    await gov.reconcile(p, a.reservation, prompt_tokens=100, completion_tokens=0)
    with pytest.raises(InsufficientQuotaError) as ei:
        await gov.admit(p)
    assert "tenant" not in ei.value.message  # per-key budget trips first


# --- keys: tenants loading, placement, rotation ------------------------------


def test_tenants_block_loads_and_attaches(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    kp.write_text(
        yaml.safe_dump(
            {
                "keys": [
                    {"id": "k1", "secret_sha256": hash_secret("brk_k1_sekret"), "tenant": "acme"}
                ],
                "tenants": {"acme": {"daily_token_budget": 1000, "monthly_cost_usd": 50.0}},
            }
        )
    )
    ks = KeyStore(str(kp))
    assert "acme" in ks.tenants
    p = ks.resolve("brk_k1_sekret")
    assert p is not None
    assert p.tenant_limits is not None
    assert p.tenant_limits.daily_token_budget == 1000
    assert p.tenant_governed is True


def test_keylimits_placement_and_per_model_roundtrip():
    lim = KeyLimits(placement=["local"], per_model={"echo": PerModelLimit(rpm=5, tpm=100)})
    rec = KeyRecord(id="x", secret_sha256="0" * 64, limits=lim)
    d = rec.to_dict()
    assert d["limits"]["placement"] == ["local"]
    assert d["limits"]["per_model"]["echo"] == {"rpm": 5, "tpm": 100}
    rec2 = KeyRecord.from_dict(d)
    assert rec2.limits.placement == ["local"]
    assert rec2.limits.per_model["echo"].rpm == 5


def test_placement_not_metered_but_present():
    # placement alone must not toggle the metered fast-path (any_limit).
    lim = KeyLimits(placement=["local"])
    assert lim.any_limit is False


def test_rotate_key_grace_overlap(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    rec, secret = ks.create_key(label="team", tenant="acme", limits=KeyLimits(rpm=5))
    result = ks.rotate_key(rec.id, grace_days=7)
    assert result is not None
    new_rec, new_secret = result
    assert new_secret != secret
    ks2 = KeyStore(str(kp))
    old = ks2.records[rec.id]
    assert old.enabled and old.expiry is not None  # kept valid during grace
    assert ks2.resolve(secret) is not None  # old still works
    assert ks2.resolve(new_secret) is not None  # new works
    assert ks2.records[new_rec.id].limits.rpm == 5  # successor inherits limits
    assert ks2.records[new_rec.id].tenant == "acme"


def test_rotate_key_immediate_revoke(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    rec, secret = ks.create_key(limits=KeyLimits(rpm=5))
    result = ks.rotate_key(rec.id, grace_days=0)
    assert result is not None
    _new_rec, new_secret = result
    ks2 = KeyStore(str(kp))
    assert ks2.resolve(secret) is None  # old revoked immediately
    assert ks2.resolve(new_secret) is not None


def test_create_key_stores_hash_only(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    _rec, secret = ks.create_key(limits=KeyLimits(rpm=5))
    raw = kp.read_text()
    assert secret not in raw  # plaintext never written
    assert hash_secret(secret) in raw  # only the hash


# --- config validation ------------------------------------------------------


def test_invalid_rate_limit_algo_rejected():
    with pytest.raises(ConfigError):
        Settings(auth_enabled=False, rate_limit_algo="bogus").validate_runtime()


def test_invalid_tpm_algo_rejected():
    with pytest.raises(ConfigError):
        Settings(auth_enabled=False, rate_limit_tpm_algo="bogus").validate_runtime()


# --- HTTP integration -------------------------------------------------------

_ECHO_REGISTRY = """
router:
  default: echo
models:
  - alias: echo
    backend: fake
    runtime_model_id: fake-echo
    supports_streaming: true
  - alias: echo2
    backend: fake
    runtime_model_id: fake-echo-2
"""

_PLACEMENT_REGISTRY = """
router:
  default: local-small
models:
  - alias: local-small
    backend: fake
    runtime_model_id: ls
    placement: [local, trusted]
  - alias: cloud-big
    backend: fake
    runtime_model_id: cb
    placement: ["region:us"]
"""


def _mk_secret(key_id: str) -> str:
    return f"brk_{key_id}_{'a' * 48}"


def _write_keys(path: Path, keys: list[dict], tenants: dict | None = None) -> None:
    doc: dict = {"keys": keys}
    if tenants:
        doc["tenants"] = tenants
    path.write_text(yaml.safe_dump(doc))


def _app(tmp_path: Path, registry_body: str, keys: list[dict], tenants=None, **over):
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(registry_body).strip())
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, keys, tenants)
    kwargs = dict(
        auth_enabled=True,
        keys_path=str(kp),
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        usage_log_path=":memory:",
        health_probe_enabled=False,
    )
    kwargs.update(over)
    return create_app(Settings(**kwargs))


def _chat(client, secret, model="echo", headers=None):
    h = {"Authorization": f"Bearer {secret}"}
    if headers:
        h.update(headers)
    return client.post(
        "/v1/chat/completions",
        headers=h,
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_http_per_model_rpm_429(tmp_path: Path):
    secret = _mk_secret("pmkey001")
    keys = [
        {
            "id": "pmkey001",
            "secret_sha256": hash_secret(secret),
            "enabled": True,
            "limits": {"per_model": {"echo": {"rpm": 1}}},
        }
    ]
    app = _app(tmp_path, _ECHO_REGISTRY, keys)
    with TestClient(app) as c:
        assert _chat(c, secret, model="echo").status_code == 200
        r = _chat(c, secret, model="echo")
        assert r.status_code == 429
        assert r.json()["error"]["type"] == "rate_limit_exceeded"
        # A different model has its own (unset) budget.
        assert _chat(c, secret, model="echo2").status_code == 200


def test_http_per_model_applies_to_auto(tmp_path: Path):
    # Single-model registry: auto always resolves to echo, so the per-model cap on
    # echo applies to auto's pick (cannot be bypassed via auto).
    secret = _mk_secret("pmauto01")
    reg = """
    router:
      default: echo
    models:
      - alias: echo
        backend: fake
        runtime_model_id: fake-echo
    """
    keys = [
        {
            "id": "pmauto01",
            "secret_sha256": hash_secret(secret),
            "enabled": True,
            "limits": {"per_model": {"echo": {"rpm": 1}}},
        }
    ]
    app = _app(tmp_path, reg, keys, router_enabled=True)
    with TestClient(app) as c:
        assert _chat(c, secret, model="auto").status_code == 200
        assert _chat(c, secret, model="auto").status_code == 429


def test_http_per_key_placement_denied(tmp_path: Path):
    secret = _mk_secret("plkey001")
    keys = [
        {
            "id": "plkey001",
            "secret_sha256": hash_secret(secret),
            "enabled": True,
            "limits": {"placement": ["region:us"]},
        }
    ]
    app = _app(tmp_path, _PLACEMENT_REGISTRY, keys)
    with TestClient(app) as c:
        # Key requires region:us; local-small lacks it -> fail-closed 403.
        r = _chat(c, secret, model="local-small")
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "placement_denied"


def test_http_per_key_placement_allowed(tmp_path: Path):
    secret = _mk_secret("plkey002")
    keys = [
        {
            "id": "plkey002",
            "secret_sha256": hash_secret(secret),
            "enabled": True,
            "limits": {"placement": ["local"]},
        }
    ]
    app = _app(tmp_path, _PLACEMENT_REGISTRY, keys)
    with TestClient(app) as c:
        assert _chat(c, secret, model="local-small").status_code == 200


def test_http_per_key_placement_intersects_header(tmp_path: Path):
    secret = _mk_secret("plkey003")
    keys = [
        {
            "id": "plkey003",
            "secret_sha256": hash_secret(secret),
            "enabled": True,
            "limits": {"placement": ["local"]},
        }
    ]
    app = _app(tmp_path, _PLACEMENT_REGISTRY, keys)
    with TestClient(app) as c:
        # key=local ∪ header=trusted -> local-small carries both -> allow.
        assert (
            _chat(
                c, secret, model="local-small", headers={"x-broker-placement": "trusted"}
            ).status_code
            == 200
        )
        # key=local ∪ header=region:us -> local-small lacks region:us -> 403.
        r = _chat(c, secret, model="local-small", headers={"x-broker-placement": "region:us"})
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "placement_denied"


def test_http_gcra_algo_rate_limits(tmp_path: Path):
    secret = _mk_secret("gcrakey1")
    keys = [
        {
            "id": "gcrakey1",
            "secret_sha256": hash_secret(secret),
            "enabled": True,
            "limits": {"rpm": 2},
        }
    ]
    app = _app(tmp_path, _ECHO_REGISTRY, keys, rate_limit_algo="gcra")
    with TestClient(app) as c:
        assert _chat(c, secret).status_code == 200
        assert _chat(c, secret).status_code == 200
        r = _chat(c, secret)
        assert r.status_code == 429
        assert r.headers.get("retry-after") is not None


# --- admin key-management API -----------------------------------------------

_ADMIN_SECRET = _mk_secret("adminky1")


def _admin_app(tmp_path: Path, extra_keys=None):
    keys = [{"id": "adminky1", "secret_sha256": hash_secret(_ADMIN_SECRET), "scope": "admin"}]
    keys.extend(extra_keys or [])
    return _app(tmp_path, _ECHO_REGISTRY, keys, admin_enabled=True)


def test_admin_keys_list_no_secrets(tmp_path: Path):
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/admin/keys", headers={"X-Admin-Key": _ADMIN_SECRET})
        assert r.status_code == 200
        body = r.json()
        assert any(k["id"] == "brk_adminky1" for k in body["keys"])
        for k in body["keys"]:
            assert "secret" not in k and "secret_sha256" not in k


def test_admin_keys_create_returns_secret_once_and_usable(tmp_path: Path):
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": "new-team", "limits": {"rpm": 5, "allowed_models": ["echo"]}},
        )
        assert r.status_code == 201
        secret = r.json()["secret"]
        assert secret.startswith("brk_")
        # The freshly minted key authenticates a real request.
        assert _chat(c, secret, model="echo").status_code == 200
        # Hash only on disk.
        raw = (tmp_path / "keys.yaml").read_text()
        assert secret not in raw and hash_secret(secret) in raw


def test_admin_keys_create_requires_admin(tmp_path: Path):
    user_secret = _mk_secret("userky01")
    extra = [{"id": "userky01", "secret_sha256": hash_secret(user_secret), "scope": "user"}]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {user_secret}"},
            json={"label": "x"},
        )
        assert r.status_code == 401


def test_admin_keys_rotate(tmp_path: Path):
    target = _mk_secret("rotme001")
    extra = [
        {
            "id": "rotme001",
            "secret_sha256": hash_secret(target),
            "scope": "user",
            "limits": {"rpm": 9},
        }
    ]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys/brk_rotme001/rotate",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"grace_days": 7},
        )
        assert r.status_code == 201
        new_secret = r.json()["secret"]
        # Both old (grace) and new keys authenticate.
        assert _chat(c, target).status_code in (200, 429)
        assert _chat(c, new_secret).status_code in (200, 429)


def test_admin_keys_revoke(tmp_path: Path):
    target = _mk_secret("revme001")
    extra = [{"id": "revme001", "secret_sha256": hash_secret(target), "scope": "user"}]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        assert _chat(c, target).status_code == 200
        r = c.delete("/admin/keys/brk_revme001", headers={"X-Admin-Key": _ADMIN_SECRET})
        assert r.status_code == 200
        # After revoke the key no longer authenticates.
        assert _chat(c, target).status_code == 401


def test_admin_keys_rotate_unknown_404(tmp_path: Path):
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/admin/keys/brk_nope/rotate", headers={"X-Admin-Key": _ADMIN_SECRET})
        assert r.status_code == 404


def test_http_non_ascii_key_still_401(tmp_path: Path):
    secret = _mk_secret("okkey001")
    keys = [{"id": "okkey001", "secret_sha256": hash_secret(secret), "limits": {"rpm": 5}}]
    app = _app(tmp_path, _ECHO_REGISTRY, keys)
    with TestClient(app) as c:
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": b"Bearer brk_okkey001_\xe9\xe9\xe9"},
            json=body,
        )
        assert r.status_code == 401
        r2 = c.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer brk_okkey001_{'a' * 20000}"},
            json=body,
        )
        assert r2.status_code == 401


# --- ADR 0016 follow-up: admin key API hardening ----------------------------
# One test per defect row from the follow-up card, plus concurrency and a
# corrupted-reload test. Admin routes are driven through the full request path.


@pytest.mark.parametrize("grace", ['"abc"', "1e999", str(10**30), "[1]", "-5", "99999999"])
def test_admin_rotate_grace_days_invalid_400(tmp_path: Path, grace):
    # "abc"/[1] (wrong type), 1e999 (inf float), 10**30 & 99999999 (out of range),
    # -5 (negative): every one must be a clean 400, never a 500.
    target = _mk_secret("gdkey001")
    extra = [{"id": "gdkey001", "secret_sha256": hash_secret(target), "scope": "user"}]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys/brk_gdkey001/rotate",
            headers={"X-Admin-Key": _ADMIN_SECRET, "Content-Type": "application/json"},
            content=f'{{"grace_days": {grace}}}',
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_grace_days"


def test_admin_rotate_grace_days_valid_bound_ok(tmp_path: Path):
    target = _mk_secret("gdkey002")
    extra = [{"id": "gdkey002", "secret_sha256": hash_secret(target), "scope": "user"}]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys/brk_gdkey002/rotate",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"grace_days": 365},  # the documented upper bound
        )
        assert r.status_code == 201


def test_admin_rotate_never_extends_expiry(tmp_path: Path):
    # A key expiring tomorrow, rotated with grace_days=30, must NOT be extended:
    # old expiry = min(existing, today+grace) = tomorrow.
    tomorrow = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)).isoformat()
    target = _mk_secret("expkey01")
    extra = [
        {
            "id": "expkey01",
            "secret_sha256": hash_secret(target),
            "scope": "user",
            "expiry": tomorrow,
        }
    ]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys/brk_expkey01/rotate",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"grace_days": 30},
        )
        assert r.status_code == 201
        listed = c.get("/admin/keys", headers={"X-Admin-Key": _ADMIN_SECRET}).json()["keys"]
        old = next(k for k in listed if k["id"] == "brk_expkey01")
        assert old["expiry"] == tomorrow  # clamped to existing, not today+30


def test_admin_rotate_revoked_key_409(tmp_path: Path):
    target = _mk_secret("revrot01")
    extra = [
        {
            "id": "revrot01",
            "secret_sha256": hash_secret(target),
            "scope": "user",
            "enabled": False,
        }
    ]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys/brk_revrot01/rotate",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"grace_days": 1},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "key_not_active"
        # No working successor must have been minted.
        assert "secret" not in r.json()


def test_admin_rotate_expired_key_409(tmp_path: Path):
    target = _mk_secret("exprot01")
    extra = [
        {
            "id": "exprot01",
            "secret_sha256": hash_secret(target),
            "scope": "user",
            "expiry": "2001-01-01",
        }
    ]
    app = _admin_app(tmp_path, extra_keys=extra)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys/brk_exprot01/rotate",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"grace_days": 1},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "key_not_active"


@pytest.mark.parametrize("expiry", ["not-a-date", "2001-01-01", "2026-13-40", 20260101])
def test_admin_create_bad_expiry_400(tmp_path: Path, expiry):
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": "x", "expiry": expiry},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] in ("invalid_expiry", "expiry_in_past")


def test_admin_create_future_expiry_ok(tmp_path: Path):
    future = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=5)).isoformat()
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": "ok", "expiry": future},
        )
        assert r.status_code == 201
        assert r.json()["expiry"] == future


def test_admin_create_iso_datetime_normalized(tmp_path: Path):
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=10)
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": "ok", "expiry": future.isoformat()},
        )
        assert r.status_code == 201
        assert r.json()["expiry"] == future.date().isoformat()  # reduced to the UTC date


def test_admin_create_bad_tenant_400(tmp_path: Path):
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": "x", "tenant": {"x": 1}},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_tenant"
        # Out-of-charset tenant is also rejected.
        r2 = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"tenant": "bad tenant!"},
        )
        assert r2.status_code == 400


def test_admin_create_bad_label_400(tmp_path: Path):
    app = _admin_app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": 123},  # non-string
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_label"
        r2 = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": "a" * 200},  # over the length cap
        )
        assert r2.status_code == 400


def test_admin_create_keys_file_mode_0600_and_hash_only(tmp_path: Path):
    app = _admin_app(tmp_path)  # pre-writes keys.yaml (default perms)
    kp = tmp_path / "keys.yaml"
    with TestClient(app) as c:
        r = c.post("/admin/keys", headers={"X-Admin-Key": _ADMIN_SECRET}, json={"label": "m"})
        assert r.status_code == 201
        assert stat.S_IMODE(os.stat(kp).st_mode) == 0o600  # tightened to owner-only
        secret = r.json()["secret"]
        raw = kp.read_text()
        assert secret not in raw and hash_secret(secret) in raw


def test_write_preserves_stricter_existing_mode(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    ks.create_key(label="a")  # new file -> 0600
    assert stat.S_IMODE(os.stat(kp).st_mode) == 0o600
    os.chmod(kp, 0o400)  # operator hardens to read-only
    ks.create_key(label="b")  # must preserve 0400, never loosen to 0600
    assert stat.S_IMODE(os.stat(kp).st_mode) == 0o400
    assert len(KeyStore(str(kp)).records) == 2  # update landed despite 0400


def test_admin_keys_request_too_large_413(tmp_path: Path):
    keys = [{"id": "adminky1", "secret_sha256": hash_secret(_ADMIN_SECRET), "scope": "admin"}]
    app = _app(tmp_path, _ECHO_REGISTRY, keys, admin_enabled=True, max_request_bytes=500)
    with TestClient(app) as c:
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": _ADMIN_SECRET},
            json={"label": "x" * 2000},  # body far exceeds the 500-byte cap
        )
        assert r.status_code == 413
        assert r.json()["error"]["type"] == "request_too_large"


def test_concurrent_creates_no_lost_updates(tmp_path: Path):
    # N concurrent creators must all land and the file must stay parseable — the
    # flock + atomic-replace guards against torn files and lost updates.
    kp = tmp_path / "keys.yaml"
    KeyStore(str(kp)).create_key(label="seed")  # ensure the file exists
    n = 12
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            KeyStore(str(kp)).create_key(label=f"k{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    final = KeyStore(str(kp))
    assert len(final.records) == n + 1  # seed + n, none lost
    data = yaml.safe_load(kp.read_text())  # always parseable
    assert len(data["keys"]) == n + 1


def test_corrupted_keys_file_reload_keeps_last_good(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    _rec, secret = ks.create_key(label="live", limits=KeyLimits(rpm=5))
    assert ks.resolve(secret) is not None
    kp.write_text("keys: [")  # corrupt: unterminated flow sequence
    ks.reload()  # must not raise and must keep the previous key set
    assert ks.resolve(secret) is not None
    assert len(ks.records) == 1


def test_rotate_key_revoked_raises_conflict(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    rec, _secret = ks.create_key(label="x")
    ks.revoke_key(rec.id)
    with pytest.raises(ConflictError):
        ks.rotate_key(rec.id, grace_days=1)


# --- CLI parity (rotate behaves the same as the admin API) ------------------


def test_cli_rotate_revoked_key_errors(tmp_path: Path, capsys):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    rec, _secret = ks.create_key(label="c")
    ks.revoke_key(rec.id)
    rc = broker_keys.main(["--path", str(kp), "rotate", rec.prefix, "--grace-days", "1"])
    assert rc == 1
    assert "inactive" in capsys.readouterr().err.lower()


def test_cli_rotate_grace_days_out_of_range_errors(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    rec, _secret = ks.create_key(label="c")
    rc = broker_keys.main(["--path", str(kp), "rotate", rec.prefix, "--grace-days", "99999999"])
    assert rc == 1


def test_cli_create_bad_expiry_errors(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    rc = broker_keys.main(["--path", str(kp), "create", "--label", "c", "--expiry", "not-a-date"])
    assert rc == 1
    assert not kp.exists() or not KeyStore(str(kp)).records  # nothing stored


def test_cli_rotate_active_key_ok_and_clamps_expiry(tmp_path: Path):
    kp = tmp_path / "keys.yaml"
    ks = KeyStore(str(kp))
    tomorrow = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)).isoformat()
    rec, _secret = ks.create_key(label="c", expiry=tomorrow)
    rc = broker_keys.main(["--path", str(kp), "rotate", rec.prefix, "--grace-days", "30"])
    assert rc == 0
    ks2 = KeyStore(str(kp))
    assert ks2.records[rec.id].expiry == tomorrow  # not extended past its own expiry
    assert len(ks2.records) == 2  # predecessor + successor
