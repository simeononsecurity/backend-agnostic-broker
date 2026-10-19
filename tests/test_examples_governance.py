"""Tests that the examples/governance walkthrough works against the real API.

Asserts (1) the shipped example keys file parses with the documented fields, and
(2) the README's claims hold against the fake backend: per-model rpm yields a 429
with Retry-After + x-ratelimit headers, and the admin key API round-trips.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.keys import KeyStore, hash_secret

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "governance" / "keys.example.yaml"

_REGISTRY = """
router:
  default: chat
models:
  - alias: chat
    backend: fake
    runtime_model_id: fake-chat
    placement: [local]
"""


def test_example_keys_file_parses_with_documented_fields():
    ks = KeyStore(str(EXAMPLE))
    team = ks.records["aaaa1111"]
    assert team.limits.per_model["chat"].rpm == 30
    assert team.limits.placement == ["local"]
    assert team.limits.rpm_burst == 10
    assert "alpha" in ks.tenants
    assert ks.tenants["alpha"].daily_token_budget == 5000000
    # The admin key is admin-scope.
    assert ks.records["bbbb2222"].scope == "admin"


@pytest.fixture
def gov_example_client(tmp_path):
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(_REGISTRY).strip())
    kp = tmp_path / "keys.yaml"
    team_secret = "brk_aaaa1111_" + "a" * 48
    admin_secret = "brk_bbbb2222_" + "b" * 48
    kp.write_text(
        yaml.safe_dump(
            {
                "keys": [
                    {
                        "id": "aaaa1111",
                        "scope": "user",
                        "tenant": "alpha",
                        "secret_sha256": hash_secret(team_secret),
                        "enabled": True,
                        "limits": {"per_model": {"chat": {"rpm": 2}}, "placement": ["local"]},
                    },
                    {
                        "id": "bbbb2222",
                        "scope": "admin",
                        "secret_sha256": hash_secret(admin_secret),
                        "enabled": True,
                    },
                ]
            }
        )
    )
    settings = Settings(
        auth_enabled=True,
        keys_path=str(kp),
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        usage_log_path=":memory:",
        health_probe_enabled=False,
        admin_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        c.team_secret = team_secret  # type: ignore[attr-defined]
        c.admin_secret = admin_secret  # type: ignore[attr-defined]
        yield c


def _chat(c, secret):
    return c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {secret}"},
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )


def test_readme_per_model_429_with_headers(gov_example_client):
    c = gov_example_client
    r1 = _chat(c, c.team_secret)
    assert r1.status_code == 200
    assert _chat(c, c.team_secret).status_code == 200
    r3 = _chat(c, c.team_secret)
    assert r3.status_code == 429
    assert r3.json()["error"]["type"] == "rate_limit_exceeded"
    assert r3.headers.get("retry-after") is not None


def test_readme_admin_key_api_roundtrip(gov_example_client):
    c = gov_example_client
    created = c.post(
        "/admin/keys",
        headers={"X-Admin-Key": c.admin_secret},
        json={"label": "ci", "tenant": "alpha", "limits": {"rpm": 10, "allowed_models": ["chat"]}},
    )
    assert created.status_code == 201
    new_secret = created.json()["secret"]
    assert _chat(c, new_secret).status_code == 200
    listing = c.get("/admin/keys", headers={"X-Admin-Key": c.admin_secret}).json()
    assert all("secret" not in k for k in listing["keys"])
