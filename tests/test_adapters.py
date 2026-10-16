"""Adapter contract conformance + per-backend config validation."""

from __future__ import annotations

import inspect

import pytest

from broker.adapters import ADAPTERS, make_adapter
from broker.adapters.base import BackendAdapter
from broker.config import Settings
from broker.errors import ConfigError, SecurityError
from broker.registry import BackendType, GPURequirements, ModelEntry, ReadinessProbe

CONTRACT = [
    "validate_config",
    "describe_model",
    "start",
    "wait_until_ready",
    "health",
    "chat",
    "stream_chat",
    "cancel",
    "unload",
    "stop",
    "collect_status",
]


def test_every_adapter_implements_contract():
    for backend, cls in ADAPTERS.items():
        assert issubclass(cls, BackendAdapter)
        # Not abstract — can be instantiated conceptually.
        assert not inspect.isabstract(cls), backend
        for method in CONTRACT:
            assert hasattr(cls, method), f"{backend} missing {method}"


async def test_fake_adapter_full_lifecycle(settings):
    entry = ModelEntry(alias="f", backend=BackendType.FAKE, runtime_model_id="x")
    a = make_adapter(entry, settings)
    a.validate_config()
    assert a.describe_model()["id"] == "f"
    await a.start()
    await a.wait_until_ready(5)
    h = await a.health()
    assert h.healthy
    resp = await a.chat({"messages": [{"role": "user", "content": "hi"}]}, "r")
    assert resp["choices"][0]["message"]["content"].startswith("echo:")
    chunks = [c async for c in a.stream_chat({"messages": []}, "r2")]
    assert chunks[-1] == b"data: [DONE]\n\n"
    status = await a.collect_status()
    assert status["backend"] == "fake"
    await a.unload()
    await a.stop()


def test_ollama_requires_base_url(settings):
    entry = ModelEntry(alias="o", backend=BackendType.OLLAMA, runtime_model_id="llama3")
    a = make_adapter(entry, settings)
    with pytest.raises(ConfigError):
        a.validate_config()


def test_ollama_rejects_startup_command(settings):
    entry = ModelEntry(
        alias="o",
        backend=BackendType.OLLAMA,
        runtime_model_id="llama3",
        base_url="http://127.0.0.1:11434",
        startup_command=["/usr/bin/ollama", "serve"],
    )
    a = make_adapter(entry, settings)
    with pytest.raises(ConfigError):
        a.validate_config()


def test_httpbackend_validate_config_builds_safe_argv(tmp_path):
    # A broker-managed runtime with a valid exe + model path validates OK,
    # and an escaping model path is rejected.
    models = tmp_path / "models"
    models.mkdir()
    model_file = models / "m.gguf"
    model_file.write_text("x")
    exe = tmp_path / "bin" / "runtime"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    s = Settings(
        auth_enabled=False,
        registry_path=str(tmp_path / "r.yaml"),  # not read here
        allowed_model_roots=[str(models)],
        allowed_exec_dirs=[str(exe.parent)],
    )
    entry = ModelEntry(
        alias="llm",
        backend=BackendType.LLAMACPP,
        runtime_model_id="m",
        model_path=str(model_file),
        base_url="http://127.0.0.1:8081",
        startup_command=[str(exe), "--model", "{model_path}", "--ctx", "{context_limit}"],
        readiness=ReadinessProbe(kind="http", path="/health"),
        gpu=GPURequirements(),
    )
    a = make_adapter(entry, s)
    a.validate_config()  # no raise
    argv = a._build_startup_argv()
    assert argv[0] == str(exe.resolve())
    assert str(model_file.resolve()) in argv
    assert "{model_path}" not in argv

    # Now point at a model outside the root -> SecurityError.
    entry2 = entry.model_copy(update={"model_path": "/etc/passwd"})
    a2 = make_adapter(entry2, s)
    with pytest.raises(SecurityError):
        a2.validate_config()
