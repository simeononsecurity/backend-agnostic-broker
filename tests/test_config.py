"""Config validation (fail-fast) tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from broker.config import Settings
from broker.errors import ConfigError


def test_auth_requires_keys(fake_registry_file):
    s = Settings(auth_enabled=True, api_keys=[], registry_path=str(fake_registry_file))
    with pytest.raises(ConfigError):
        s.validate_runtime()


def test_valid_config_passes(settings):
    settings.validate_runtime()  # should not raise


def test_missing_registry_fails(tmp_path):
    s = Settings(
        auth_enabled=False,
        registry_path=str(tmp_path / "nope.yaml"),
    )
    with pytest.raises(ConfigError):
        s.validate_runtime()


def test_bad_log_format_rejected(fake_registry_file):
    with pytest.raises(PydanticValidationError):
        Settings(log_format="xml", registry_path=str(fake_registry_file))


def test_bad_policy_rejected(fake_registry_file):
    with pytest.raises(PydanticValidationError):
        Settings(load_concurrency_policy="nope", registry_path=str(fake_registry_file))


def test_retry_backoff_bounds_validated(settings):
    settings.retry_backoff_base_ms = 5000
    settings.retry_backoff_max_ms = 1000  # max < base
    with pytest.raises(ConfigError):
        settings.validate_runtime()


def test_negative_retry_attempts_rejected(settings):
    settings.retry_max_attempts = -1
    with pytest.raises(ConfigError):
        settings.validate_runtime()


def test_negative_cooldown_failures_rejected(settings):
    settings.cooldown_failures = -1
    with pytest.raises(ConfigError):
        settings.validate_runtime()


def test_empty_redis_url_rejected(settings):
    settings.redis_url = ""  # set-but-empty is a misconfiguration
    with pytest.raises(ConfigError):
        settings.validate_runtime()


def test_retry_cooldown_defaults_preserve_legacy(settings):
    # Defaults must reproduce v0.4.0: no retries, no backoff, no cooldowns.
    assert settings.retry_max_attempts == 0
    assert settings.retry_backoff_base_ms == 0
    assert settings.cooldown_failures == 0
    assert settings.redis_url is None
    settings.validate_runtime()  # valid as-is
