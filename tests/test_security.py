"""Security regression tests: SSRF, path traversal, shell injection, flags."""

from __future__ import annotations

import pytest

from broker.errors import SecurityError, ValidationError
from broker.security import (
    build_argv,
    reject_request_flags,
    validate_executable,
    validate_model_path,
    validate_upstream_url,
)


# ---- path traversal --------------------------------------------------------
def test_model_path_traversal_blocked(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    (root / "ok.gguf").write_text("x")
    # A traversal attempt that escapes the root must be rejected.
    evil = str(root / ".." / ".." / "etc" / "passwd")
    with pytest.raises(SecurityError):
        validate_model_path(evil, [str(root)])


def test_model_path_within_root_ok(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    f = root / "ok.gguf"
    f.write_text("x")
    assert validate_model_path(str(f), [str(root)]) == str(f.resolve())


def test_model_path_missing_file(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    with pytest.raises(ValidationError):
        validate_model_path(str(root / "nope.gguf"), [str(root)])


def test_symlink_escape_blocked(tmp_path):
    root = tmp_path / "models"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("secret")
    link = root / "link"
    link.symlink_to(outside)
    # Resolves outside the root => rejected.
    with pytest.raises(SecurityError):
        validate_model_path(str(link), [str(root)])


# ---- executable allow-list -------------------------------------------------
def test_executable_not_absolute():
    with pytest.raises(SecurityError):
        validate_executable("llama-server", ["/usr/local/bin"])


def test_executable_not_in_allowlist(tmp_path):
    exe = tmp_path / "evil.sh"
    exe.write_text("#!/bin/sh\necho hi\n")
    exe.chmod(0o755)
    with pytest.raises(SecurityError):
        validate_executable(str(exe), ["/usr/local/bin"])


def test_executable_allowed(tmp_path):
    exe = tmp_path / "ok"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert validate_executable(str(exe), [str(tmp_path)]) == str(exe.resolve())


# ---- shell-injection / flag rejection -------------------------------------
def test_reject_flag_tokens():
    with pytest.raises(SecurityError):
        reject_request_flags("--n-gpu-layers 999", "model")


def test_reject_shell_metacharacters():
    for bad in ["a; rm -rf /", "a && b", "a | b", "a`b`", "a$(b)", "a>b"]:
        with pytest.raises(SecurityError):
            reject_request_flags(bad, "model")


def test_allow_plain_alias():
    reject_request_flags("llama3-ollama", "model")  # no raise


def test_build_argv_is_list_never_shell():
    argv = build_argv(
        ["{exe}", "--model", "{model_path}"],
        {"exe": "/usr/local/bin/llama-server", "model_path": "/models/x.gguf"},
    )
    assert argv == ["/usr/local/bin/llama-server", "--model", "/models/x.gguf"]
    assert isinstance(argv, list)


def test_build_argv_unknown_placeholder():
    with pytest.raises(SecurityError):
        build_argv(["{exe}", "{unknown}"], {"exe": "/bin/true"})


def test_build_argv_rejects_injected_metachars():
    with pytest.raises(SecurityError):
        build_argv(["{exe}"], {"exe": "/bin/true; rm -rf /"})


# ---- SSRF ------------------------------------------------------------------
def test_ssrf_url_not_in_allowlist():
    with pytest.raises(SecurityError):
        validate_upstream_url("http://evil.example.com/x", ["http://127.0.0.1:11434"])


def test_ssrf_metadata_ip_blocked():
    # Even if base matches, link-local metadata must be rejected.
    with pytest.raises(SecurityError):
        validate_upstream_url(
            "http://169.254.169.254/latest/meta-data",
            ["http://169.254.169.254"],
            allow_loopback=False,
        )


def test_ssrf_loopback_allowed():
    url = validate_upstream_url("http://127.0.0.1:11434/api/tags", ["http://127.0.0.1:11434"])
    assert url.startswith("http://127.0.0.1:11434")


def test_ssrf_scheme_rejected():
    with pytest.raises(SecurityError):
        validate_upstream_url("file:///etc/passwd", ["file:///etc"])
