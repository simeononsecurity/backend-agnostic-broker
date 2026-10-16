"""Shared test fixtures."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from broker.config import Settings


@pytest.fixture
def tmp_models_root(tmp_path: Path) -> Path:
    root = tmp_path / "models"
    root.mkdir()
    return root


@pytest.fixture
def fake_registry_file(tmp_path: Path) -> Path:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        textwrap.dedent(
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
            """
        ).strip()
    )
    return reg


@pytest.fixture
def settings(fake_registry_file: Path, tmp_models_root: Path) -> Settings:
    return Settings(
        auth_enabled=True,
        api_keys=["test-key"],
        registry_path=str(fake_registry_file),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_models_root)],
        allowed_exec_dirs=["/usr/bin", "/usr/local/bin", "/bin"],
        model_idle_timeout_seconds=1,
        idle_sweep_interval_seconds=0.2,
        max_loaded_models=2,
    )


@pytest.fixture
def noauth_settings(fake_registry_file: Path, tmp_models_root: Path) -> Settings:
    return Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(fake_registry_file),
        allowed_model_roots=[str(tmp_models_root)],
        model_idle_timeout_seconds=1,
        idle_sweep_interval_seconds=0.2,
    )
