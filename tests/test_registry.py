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
