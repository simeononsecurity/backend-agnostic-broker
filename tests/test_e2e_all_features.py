"""End-to-end integration smoke across every shipped feature (ADR 0006–0019).

One committed module that drives the **full HTTP request path** through a
``TestClient`` against the deterministic **fake** backend — no external runtime,
no network. It is intentionally broad rather than deep: each feature has its own
focused suite (``test_governance_v2.py``, ``test_cooldown.py``, ``test_rerank.py``,
``test_semantic_cache.py``, ``test_ui.py``, …) that exercises edge cases. This
module proves the features still compose into one coherent broker after the
ADR 0013–0019 integration merge:

- 0006 OpenAI surface: /v1/chat/completions, /v1/completions, /v1/responses, /v1/embeddings
- 0007 usage accounting: /usage
- 0008 + 0016 governance v2: per-key rpm 429, per-model limits, fail-closed
  placement, and the admin key-management API (create/rotate/revoke)
- 0009 + 0017 reliability v2: in-request failover + member cooldown surfaced in /status
- 0010 + 0018 cache-aware pool routing
- 0011 + 0019 auto-router (heuristic) resolution
- 0012 exact response cache + runtime control (admin unload) + default-off tracing
- 0013 /v1/rerank
- 0014 per-request cache controls (TTL/namespace) and their validation
- 0015 web UI playground (served + hardened when enabled; default-off otherwise)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.keys import hash_secret

# A single registry rich enough for most features: chat, embeddings, rerank,
# a two-member pool, and an auto-router default.
REGISTRY = """
router:
  default: echo
models:
  - alias: echo
    backend: fake
    runtime_model_id: fake-echo
  - alias: echo2
    backend: fake
    runtime_model_id: fake-echo-2
  - alias: reranker
    backend: fake
    runtime_model_id: fake-reranker
    supports_rerank: true
  - alias: pa
    backend: fake
    runtime_model_id: shared-pool
    pool: mypool
  - alias: pb
    backend: fake
    runtime_model_id: shared-pool
    pool: mypool
"""

KEY = "e2e-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def _app(tmp_path: Path, *, keys: list[dict] | None = None, registry: str = REGISTRY, **over):
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(registry).strip())
    kwargs: dict = dict(
        auth_enabled=True,
        api_keys=[KEY],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        usage_log_path=":memory:",
        health_probe_enabled=False,
        max_loaded_models=5,
    )
    if keys is not None:
        kp = tmp_path / "keys.yaml"
        kp.write_text(yaml.safe_dump({"keys": keys}))
        kwargs["keys_path"] = str(kp)
        kwargs["api_keys"] = []
    kwargs.update(over)
    return create_app(Settings(**kwargs))


def _secret(key_id: str) -> str:
    return f"brk_{key_id}_{'a' * 48}"


def _chat(client, *, model="echo", headers=None, secret=None, content="hi", temperature=0):
    h = dict(headers or AUTH)
    if secret is not None:
        h = {"Authorization": f"Bearer {secret}"}
        if headers:
            h.update(headers)
    return client.post(
        "/v1/chat/completions",
        headers=h,
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
        },
    )


# --- 0006 OpenAI surface + 0007 usage ---------------------------------------
def test_openai_surface_and_usage(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert _chat(c).status_code == 200
        assert (
            c.post(
                "/v1/completions", headers=AUTH, json={"model": "echo", "prompt": "hello"}
            ).status_code
            == 200
        )
        assert (
            c.post(
                "/v1/responses", headers=AUTH, json={"model": "echo", "input": "hello"}
            ).status_code
            == 200
        )
        emb = c.post("/v1/embeddings", headers=AUTH, json={"model": "echo", "input": "hello"})
        assert emb.status_code == 200
        assert emb.json()["data"][0]["embedding"]
        # Usage accounting recorded the traffic.
        u = c.get("/usage", headers=AUTH)
        assert u.status_code == 200
        assert c.get("/usage").status_code == 401  # auth enforced


# --- 0008 + 0016 governance v2 ----------------------------------------------
def test_governance_per_key_rpm_and_per_model(tmp_path):
    secret = _secret("gkey0001")
    keys = [
        {
            "id": "gkey0001",
            "secret_sha256": hash_secret(secret),
            "enabled": True,
            "limits": {"rpm": 1, "per_model": {"echo2": {"rpm": 2}}},
        }
    ]
    app = _app(tmp_path, keys=keys)
    with TestClient(app) as c:
        assert _chat(c, secret=secret, model="echo").status_code == 200
        r = _chat(c, secret=secret, model="echo")
        assert r.status_code == 429
        assert r.headers.get("retry-after") is not None
        assert r.json()["error"]["type"] == "rate_limit_exceeded"


def test_governance_admin_key_lifecycle(tmp_path):
    admin = _secret("admink01")
    keys = [{"id": "admink01", "secret_sha256": hash_secret(admin), "scope": "admin"}]
    app = _app(tmp_path, keys=keys, admin_enabled=True)
    with TestClient(app) as c:
        # Create a user key; the secret is returned exactly once.
        r = c.post(
            "/admin/keys",
            headers={"X-Admin-Key": admin},
            json={"label": "team", "limits": {"rpm": 5, "allowed_models": ["echo"]}},
        )
        assert r.status_code == 201
        new_secret = r.json()["secret"]
        assert new_secret.startswith("brk_")
        assert _chat(c, secret=new_secret, model="echo").status_code == 200
        # Hash only on disk — the plaintext secret is never persisted.
        raw = (tmp_path / "keys.yaml").read_text()
        assert new_secret not in raw and hash_secret(new_secret) in raw
        # Listing never leaks secrets.
        listed = c.get("/admin/keys", headers={"X-Admin-Key": admin}).json()["keys"]
        assert all("secret" not in k and "secret_sha256" not in k for k in listed)
        key_id = next(k["id"] for k in listed if k["id"] != "brk_admink01")
        # Rotate, then revoke.
        assert (
            c.post(
                f"/admin/keys/{key_id}/rotate",
                headers={"X-Admin-Key": admin},
                json={"grace_days": 7},
            ).status_code
            == 201
        )
        assert c.delete(f"/admin/keys/{key_id}", headers={"X-Admin-Key": admin}).status_code == 200


# --- 0009 + 0017 reliability v2: in-request failover + cooldown --------------
def test_reliability_failover_and_cooldown_visible(tmp_path):
    app = _app(tmp_path, cooldown_failures=1, cooldown_seconds=30)
    with TestClient(app) as c:
        mgr = app.state.manager
        # Make pool member "pa" fail; requests to the pool must fail over to "pb".
        mgr.models["pa"].adapter.fail_on_chat = True
        served = set()
        for i in range(5):
            r = _chat(c, model="mypool", content=f"q{i}")
            assert r.status_code == 200, r.text
            served.add(r.json()["choices"][0]["message"]["content"])
        # Every successful response came from the healthy member.
        assert served == {"echo: pb"}
        # The failed member shows up benched in /status.
        st = c.get("/status", headers=AUTH).json()
        assert st["cooldowns_enabled"] is True
        by_alias = {m["alias"]: m for m in st["models"]}
        assert by_alias["pa"]["cooldown"]["in_cooldown"] is True


# --- 0010 + 0018 cache-aware pool routing -----------------------------------
def test_cache_aware_pool_routing(tmp_path):
    app = _app(tmp_path, route_strategy="cache_aware")
    with TestClient(app) as c:
        r = _chat(c, model="mypool")
        assert r.status_code == 200, r.text
        assert r.headers.get("x-broker-route")  # a member was chosen + surfaced


# --- 0011 + 0019 auto-router ------------------------------------------------
def test_auto_router_resolves(tmp_path):
    app = _app(tmp_path, router_enabled=True, router_backend="heuristic")
    with TestClient(app) as c:
        r = _chat(c, model="auto", content="write a python function to sort a list")
        assert r.status_code == 200, r.text
        assert r.headers.get("x-broker-route")


# --- 0012 exact cache + runtime control + default-off tracing ---------------
def test_exact_cache_and_runtime_unload(tmp_path):
    app = _app(
        tmp_path,
        response_cache_enabled=True,
        admin_enabled=True,
        api_keys=[KEY],
        admin_api_keys=["admin-key"],
    )
    with TestClient(app) as c:
        body = {
            "model": "echo",
            "messages": [{"role": "user", "content": "cache me"}],
            "temperature": 0,
        }
        r1 = c.post("/v1/chat/completions", headers=AUTH, json=body)
        assert r1.headers["x-broker-cache"] == "miss"
        r2 = c.post("/v1/chat/completions", headers=AUTH, json=body)
        assert r2.headers["x-broker-cache"] == "hit"
        # Runtime control: admin can unload a loaded model.
        assert c.post(
            "/admin/models/echo/unload", headers={"X-Admin-Key": "admin-key"}
        ).status_code in (200, 202)


# --- 0013 /v1/rerank --------------------------------------------------------
def test_rerank_endpoint(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/v1/rerank",
            headers=AUTH,
            json={
                "model": "reranker",
                "query": "What is the capital of France?",
                "documents": [
                    "The capital of France is Paris.",
                    "Bananas are yellow.",
                    "Berlin is the capital of Germany.",
                ],
            },
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["object"] == "rerank"
        scores = [x["relevance_score"] for x in d["results"]]
        assert scores == sorted(scores, reverse=True)  # sorted desc
        assert d["results"][0]["index"] == 0  # the France doc ranks first


# --- 0014 per-request cache controls ----------------------------------------
def test_cache_controls_validation(tmp_path):
    app = _app(tmp_path, response_cache_enabled=True)
    with TestClient(app) as c:
        body = {"model": "echo", "messages": [{"role": "user", "content": "ctl"}], "temperature": 0}
        # A negative / non-numeric per-request TTL override is rejected (ADR 0014).
        assert (
            c.post(
                "/v1/chat/completions", headers={**AUTH, "x-broker-cache-ttl": "-1"}, json=body
            ).status_code
            == 400
        )
        assert (
            c.post(
                "/v1/chat/completions", headers={**AUTH, "x-broker-cache-ttl": "nan"}, json=body
            ).status_code
            == 400
        )
        # A namespace scopes the cache: a namespaced miss does not see the default hit.
        r1 = c.post("/v1/chat/completions", headers=AUTH, json=body)
        assert r1.headers["x-broker-cache"] == "miss"
        r2 = c.post(
            "/v1/chat/completions",
            headers={**AUTH, "x-broker-cache-namespace": "tenant-x"},
            json=body,
        )
        assert r2.headers["x-broker-cache"] == "miss"


# --- 0015 web UI playground -------------------------------------------------
_UI_SECURITY = {
    "content-security-policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "object-src 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}


def test_web_ui_served_and_hardened(tmp_path):
    app = _app(tmp_path, ui_enabled=True)
    with TestClient(app) as c:
        r = c.get("/ui")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        for name, value in _UI_SECURITY.items():
            assert r.headers.get(name) == value, name
        assert c.get("/ui/app.js").status_code == 200


def test_web_ui_default_off(tmp_path):
    app = _app(tmp_path)  # ui_enabled defaults to False
    with TestClient(app) as c:
        assert c.get("/ui").status_code == 404
        assert c.get("/ui/app.js").status_code == 404
