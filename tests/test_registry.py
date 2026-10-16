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
