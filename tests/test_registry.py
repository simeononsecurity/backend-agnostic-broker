"""Registry parsing tests."""

from __future__ import annotations

import pytest

from broker.errors import ConfigError
from broker.registry import BackendType, load_registry


def test_load_fake_registry(fake_registry_file):
    reg = load_registry(str(fake_registry_file))
    assert len(reg.models) == 2
    assert reg.models[0].alias == "echo"
    assert reg.models[0].backend == BackendType.FAKE


def test_duplicate_alias_rejected(tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text(
        "models:\n"
        "  - alias: a\n    backend: fake\n    runtime_model_id: x\n"
        "  - alias: a\n    backend: fake\n    runtime_model_id: y\n"
    )
    with pytest.raises(ConfigError):
        load_registry(str(p))


def test_backend_filter(fake_registry_file):
    reg = load_registry(str(fake_registry_file), enabled_backends={"ollama"})
    assert reg.models == []


def test_unknown_backend_rejected(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("models:\n  - alias: a\n    backend: nope\n    runtime_model_id: x\n")
    with pytest.raises(ConfigError):
        load_registry(str(p))


def test_pricing_block_parsed_and_survives_backend_filter(tmp_path):
    p = tmp_path / "priced.yaml"
    p.write_text(
        "models:\n"
        "  - alias: a\n    backend: fake\n    runtime_model_id: x\n"
        "pricing:\n"
        "  a:\n    input_per_million: 0.5\n    output_per_million: 1.5\n"
    )
    reg = load_registry(str(p), enabled_backends={"fake"})
    assert reg.pricing["a"].input_per_million == 0.5
    assert reg.pricing["a"].output_per_million == 1.5


def test_router_weights_cost_normalized_defaults_off_and_parses(tmp_path):
    # ADR 0019 follow-up: the new router.weights.cost_normalized knob defaults to
    # v0.5.0 behavior (off) and round-trips from YAML when set true.
    p = tmp_path / "rw.yaml"
    p.write_text(
        "router:\n  default: a\n  weights: { capability: 3.0, cost: 1.0, cost_normalized: true }\n"
        "models:\n  - alias: a\n    backend: fake\n    runtime_model_id: x\n"
    )
    reg = load_registry(str(p), enabled_backends={"fake"})
    assert reg.router.weights.cost_normalized is True

    p2 = tmp_path / "rw2.yaml"
    p2.write_text("models:\n  - alias: a\n    backend: fake\n    runtime_model_id: x\n")
    reg2 = load_registry(str(p2), enabled_backends={"fake"})
    assert reg2.router.weights.cost_normalized is False  # default = v0.5.0


def test_pricing_rejects_negative(tmp_path):
    p = tmp_path / "negprice.yaml"
    p.write_text(
        "models:\n  - alias: a\n    backend: fake\n    runtime_model_id: x\n"
        "pricing:\n  a:\n    input_per_million: -1\n"
    )
    with pytest.raises(ConfigError):
        load_registry(str(p))


def test_extra_field_rejected(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "models:\n  - alias: a\n    backend: fake\n    runtime_model_id: x\n    bogus: 1\n"
    )
    with pytest.raises(ConfigError):
        load_registry(str(p))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_registry(str(tmp_path / "missing.yaml"))


def test_env_var_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OLLAMA_URL", "http://10.0.0.9:11434")
    p = tmp_path / "env.yaml"
    p.write_text(
        "models:\n"
        "  - alias: a\n    backend: ollama\n    runtime_model_id: m\n"
        "    base_url: ${TEST_OLLAMA_URL}\n"
    )
    reg = load_registry(str(p))
    assert reg.models[0].base_url == "http://10.0.0.9:11434"


def test_env_var_default_used_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_OLLAMA_URL", raising=False)
    p = tmp_path / "env_default.yaml"
    p.write_text(
        "models:\n"
        "  - alias: a\n    backend: ollama\n    runtime_model_id: m\n"
        "    base_url: ${TEST_OLLAMA_URL:-http://127.0.0.1:11434}\n"
    )
    reg = load_registry(str(p))
    assert reg.models[0].base_url == "http://127.0.0.1:11434"


def test_unset_env_var_without_default_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_MISSING_VAR", raising=False)
    p = tmp_path / "env_missing.yaml"
    p.write_text(
        "models:\n"
        "  - alias: a\n    backend: ollama\n    runtime_model_id: m\n"
        "    base_url: ${TEST_MISSING_VAR}\n"
    )
    with pytest.raises(ConfigError):
        load_registry(str(p))


def test_env_ref_in_comment_is_not_expanded(tmp_path, monkeypatch):
    # A ${VAR} mentioned in a YAML comment must NOT trigger expansion/fail-fast —
    # only string values are expanded. Guards registry.example.yaml loading.
    monkeypatch.delenv("UNSET_DOC_VAR", raising=False)
    p = tmp_path / "comment.yaml"
    p.write_text(
        "# Example: point base_url at ${UNSET_DOC_VAR} via .env\n"
        "models:\n"
        "  - alias: a\n    backend: fake\n    runtime_model_id: m\n"
    )
    reg = load_registry(str(p))
    assert reg.models[0].alias == "a"
