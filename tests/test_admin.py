"""Admin runtime-control tests (ADR 0012): auth, unload/load, hot-reload."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.errors import ConfigError, ModelBusyError
from broker.keys import hash_secret
from broker.manager import ModelManager
from broker.registry import load_registry

ADMIN = {"X-Admin-Key": "admin-key"}


def _admin_settings(fake_registry_file, tmp_models_root, **over) -> Settings:
    base = dict(
        auth_enabled=True,
        api_keys=["user-key"],
        admin_enabled=True,
        admin_api_keys=["admin-key"],
        admin_drain_timeout_seconds=0.2,
        registry_path=str(fake_registry_file),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_models_root)],
        model_idle_timeout_seconds=60,
        idle_sweep_interval_seconds=5,
        max_loaded_models=2,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def admin_client(fake_registry_file, tmp_models_root):
    app = create_app(_admin_settings(fake_registry_file, tmp_models_root))
    with TestClient(app) as c:
        yield c


def _manager(settings) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    return ModelManager(reg, settings)


# --- auth / gating ---------------------------------------------------------
def test_admin_disabled_blocks(fake_registry_file, tmp_models_root):
    s = _admin_settings(fake_registry_file, tmp_models_root, admin_enabled=False)
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/admin/models", headers=ADMIN)
        assert r.status_code != 200
        assert r.json()["error"]["code"] == "admin_disabled"


def test_admin_requires_admin_key(admin_client):
    # No key at all → 401
    assert admin_client.get("/admin/models").status_code == 401
    # A valid *user* key is not an admin key → 401
    r = admin_client.get("/admin/models", headers={"X-Admin-Key": "user-key"})
    assert r.status_code == 401


def test_admin_models_list(admin_client):
    r = admin_client.get("/admin/models", headers=ADMIN)
    assert r.status_code == 200
    models = {m["alias"]: m for m in r.json()["models"]}
    assert {"echo", "echo2"} <= set(models)
    assert models["echo"]["state"] == "unloaded"  # nothing loaded at startup
    assert models["echo"]["inflight"] == 0


def test_admin_load_then_unload(admin_client):
    # Explicit preload — the only non-request load path.
    r = admin_client.post("/admin/models/echo/load", headers=ADMIN)
    assert r.status_code == 200 and r.json()["loaded"] is True
    listed = {
        m["alias"]: m for m in admin_client.get("/admin/models", headers=ADMIN).json()["models"]
    }
    assert listed["echo"]["loaded"] is True
    r = admin_client.post("/admin/models/echo/unload", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["state"] == "unloaded"


def test_admin_unload_unknown_model_404(admin_client):
    r = admin_client.post("/admin/models/nope/unload", headers=ADMIN)
    assert r.status_code == 404


# --- unload semantics: refuse / drain (manager unit) -----------------------
def test_admin_unload_refuses_inflight(fake_registry_file, tmp_models_root):
    async def run():
        mgr = _manager(_admin_settings(fake_registry_file, tmp_models_root))
        m = mgr.models["echo"]
        m.machine.inflight = 1  # simulate an in-flight request
        with pytest.raises(ModelBusyError):
            await mgr.admin_unload("echo", drain=False)

    asyncio.run(run())


def test_admin_unload_drain_waits_for_inflight(fake_registry_file, tmp_models_root):
    async def run():
        mgr = _manager(_admin_settings(fake_registry_file, tmp_models_root))
        m = mgr.models["echo"]
        m.machine.inflight = 1

        async def clear():
            await asyncio.sleep(0.05)
            m.machine.inflight = 0

        task = asyncio.ensure_future(clear())
        result = await mgr.admin_unload("echo", drain=True)
        await task
        assert result["drained"] is True
        assert m.draining is False  # flag cleared afterwards

    asyncio.run(run())


def test_admin_unload_drain_times_out(fake_registry_file, tmp_models_root):
    async def run():
        mgr = _manager(_admin_settings(fake_registry_file, tmp_models_root))
        m = mgr.models["echo"]
        m.machine.inflight = 1  # never clears → drain must time out
        with pytest.raises(ModelBusyError):
            await mgr.admin_unload("echo", drain=True)
        assert m.draining is False

    asyncio.run(run())


def test_draining_member_skipped_by_selection(fake_registry_file, tmp_models_root):
    async def run():
        mgr = _manager(_admin_settings(fake_registry_file, tmp_models_root))
        m = mgr.models["echo"]
        m.draining = True
        # Single-member "echo" pool with its only member draining → no candidate.
        chosen = await mgr._select_member("echo", [m], set(), route_key=None)
        assert chosen is None

    asyncio.run(run())


# --- registry hot-reload ---------------------------------------------------
def _write_registry(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).strip())


def test_admin_reload_adds_and_removes(fake_registry_file, tmp_models_root):
    async def run():
        settings = _admin_settings(fake_registry_file, tmp_models_root)
        mgr = _manager(settings)
        # Rewrite registry: drop echo2, add echo3.
        _write_registry(
            Path(settings.registry_path),
            """
            models:
              - alias: echo
                backend: fake
                runtime_model_id: fake-echo
              - alias: echo3
                backend: fake
                runtime_model_id: fake-echo-3
            """,
        )
        diff = await mgr.admin_reload_registry()
        assert diff["added"] == ["echo3"]
        assert diff["removed"] == ["echo2"]
        assert "echo3" in mgr.models and "echo2" not in mgr.models
        assert "echo3" in mgr.pools

    asyncio.run(run())


def test_admin_reload_invalid_registry_rolls_back(fake_registry_file, tmp_models_root):
    async def run():
        settings = _admin_settings(fake_registry_file, tmp_models_root)
        mgr = _manager(settings)
        before = set(mgr.models)
        _write_registry(Path(settings.registry_path), "this: is: not: a: valid: registry")
        with pytest.raises(ConfigError):
            await mgr.admin_reload_registry()
        assert set(mgr.models) == before  # unchanged on failure

    asyncio.run(run())


def test_admin_reload_keeps_unchanged_model_instance(fake_registry_file, tmp_models_root):
    async def run():
        settings = _admin_settings(fake_registry_file, tmp_models_root)
        mgr = _manager(settings)
        echo_before = mgr.models["echo"]
        # Change only echo2; echo is untouched.
        _write_registry(
            Path(settings.registry_path),
            """
            models:
              - alias: echo
                backend: fake
                runtime_model_id: fake-echo
                idle_timeout_seconds: 1
                context_limit: 4096
                supports_streaming: true
              - alias: echo2
                backend: fake
                runtime_model_id: fake-echo-2
                idle_timeout_seconds: 1
                context_limit: 2048
            """,
        )
        diff = await mgr.admin_reload_registry()
        assert diff["changed"] == ["echo2"]
        assert diff["unchanged"] == ["echo"]
        assert mgr.models["echo"] is echo_before  # same live instance kept

    asyncio.run(run())


def test_admin_reload_via_http(admin_client):
    r = admin_client.post("/admin/registry/reload", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert "added" in body and "removed" in body and "changed" in body


# --- auth hardening: malformed admin keys fail closed, never 500 -----------
# Integration regression (ADR 0008 + ADR 0012): hmac.compare_digest raises
# TypeError on a non-ASCII str, which on the admin auth path previously surfaced
# as a 500. require_admin now compares byte-wise on every credential source, so a
# malformed key is a clean 401 — on X-Admin-Key, Authorization, and X-API-Key.
@pytest.mark.parametrize("header", ["X-Admin-Key", "Authorization", "X-API-Key"])
def test_admin_non_ascii_key_401_not_500(admin_client, header):
    # Raw bytes so a latin-1 (non-ASCII) byte goes on the wire, as a real client
    # could send. Must be a clean 401, never a 500 from hmac.compare_digest.
    bad = b"Bearer admin-key\xff" if header == "Authorization" else b"admin-key\xff"
    r = admin_client.get("/admin/models", headers={header: bad})
    assert r.status_code == 401


def test_admin_over_long_key_401(admin_client):
    r = admin_client.get("/admin/models", headers={"X-Admin-Key": "x" * 100_000})
    assert r.status_code == 401


# --- admin authorization via an ADR 0008 admin-scope virtual key -----------
_ADMIN_VKEY = "brk_vadmin01_" + "c" * 48
_USER_VKEY = "brk_vuser001_" + "d" * 48


@pytest.fixture
def vkey_admin_client(fake_registry_file, tmp_models_root, tmp_path):
    """Admin enabled with NO BROKER_ADMIN_API_KEYS — authorization must come
    solely from an ADR 0008 virtual key whose scope is ``admin``."""
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        yaml.safe_dump(
            {
                "keys": [
                    {
                        "id": "vadmin01",
                        "scope": "admin",
                        "secret_sha256": hash_secret(_ADMIN_VKEY),
                        "enabled": True,
                    },
                    {
                        "id": "vuser001",
                        "scope": "user",
                        "secret_sha256": hash_secret(_USER_VKEY),
                        "enabled": True,
                    },
                ]
            }
        )
    )
    s = _admin_settings(
        fake_registry_file,
        tmp_models_root,
        admin_api_keys=[],
        keys_path=str(keys),
    )
    app = create_app(s)
    with TestClient(app) as c:
        yield c


def test_admin_scope_virtual_key_authorizes(vkey_admin_client):
    # An admin-scope virtual key is accepted even with no BROKER_ADMIN_API_KEYS.
    r = vkey_admin_client.get("/admin/models", headers={"X-Admin-Key": _ADMIN_VKEY})
    assert r.status_code == 200
    # Presented as a Bearer token too.
    r2 = vkey_admin_client.get(
        "/admin/models", headers={"Authorization": f"Bearer {_ADMIN_VKEY}"}
    )
    assert r2.status_code == 200


def test_user_scope_virtual_key_denied_on_admin(vkey_admin_client):
    r = vkey_admin_client.get("/admin/models", headers={"X-Admin-Key": _USER_VKEY})
    assert r.status_code == 401
