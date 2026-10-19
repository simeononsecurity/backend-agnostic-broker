"""The examples/caching/ example loads and caches against the fake backend (ADR 0014)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings

EX = Path(__file__).resolve().parents[1] / "examples" / "caching"
KEY = "example-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
CHAT = {"model": "echo-chat", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}


def test_example_files_present():
    for name in (
        "README.md",
        "registry.yaml",
        "env.exact",
        "env.semantic",
        "curl.sh",
        "openai_sdk.py",
    ):
        assert (EX / name).is_file(), f"missing examples/caching/{name}"


def _settings(**over) -> Settings:
    base = dict(
        auth_enabled=True,
        api_keys=[KEY],
        registry_path=str(EX / "registry.yaml"),
        enabled_backends=["fake"],
        response_cache_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def test_example_exact_cache(tmp_path):
    app = create_app(_settings(cache_dir=str(tmp_path)))
    with TestClient(app) as c:
        assert (
            c.post("/v1/chat/completions", headers=AUTH, json=CHAT).headers["x-broker-cache"]
            == "miss"
        )
        assert (
            c.post("/v1/chat/completions", headers=AUTH, json=CHAT).headers["x-broker-cache"]
            == "hit"
        )


def test_example_semantic_cache(tmp_path):
    # the example's semantic config, with on-demand embedding to keep the test simple
    app = create_app(
        _settings(
            cache_dir=str(tmp_path),
            response_cache_enabled=False,
            semantic_cache_enabled=True,
            semantic_cache_model="echo-embed",
            semantic_cache_threshold=0.95,
            semantic_cache_require_resident=False,
        )
    )
    with TestClient(app) as c:
        r1 = c.post("/v1/chat/completions", headers=AUTH, json=CHAT)
        assert r1.headers["x-broker-cache"] == "miss"
        r2 = c.post("/v1/chat/completions", headers=AUTH, json=CHAT)
        assert r2.headers["x-broker-cache"] == "semantic-hit"


@pytest.mark.parametrize("env_file", ["env.exact", "env.semantic"])
def test_example_env_files_reference_known_settings(env_file):
    # every BROKER_* key in the example env files must be a real Settings field
    valid = set(Settings.model_fields)
    for line in (EX / env_file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0]
        assert key.startswith("BROKER_")
        assert key[len("BROKER_") :].lower() in valid, f"{key} not a Settings field"
