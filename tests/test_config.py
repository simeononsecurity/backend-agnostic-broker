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


def test_auth_satisfied_by_keys_file(fake_registry_file, tmp_path):
    kp = tmp_path / "keys.yaml"
    kp.write_text("keys: []\n")
    # No api_keys, but a keys file is present → auth config is valid (ADR 0008).
    s = Settings(
        auth_enabled=True,
        api_keys=[],
        keys_path=str(kp),
        registry_path=str(fake_registry_file),
    )
    s.validate_runtime()  # must not raise


def test_admin_enabled_requires_admin_credentials(fake_registry_file):
    # Admin + auth enabled with neither admin keys nor a keys file is a dead end
    # (nothing could ever authorize /admin) → fail fast.
    s = Settings(
        auth_enabled=True,
        api_keys=["user-key"],
        admin_enabled=True,
        admin_api_keys=[],
        registry_path=str(fake_registry_file),
    )
    with pytest.raises(ConfigError):
        s.validate_runtime()


def test_admin_enabled_satisfied_by_keys_file(fake_registry_file, tmp_path):
    # A keys file can carry an admin-scope virtual key (ADR 0008), so admin may be
    # enabled without BROKER_ADMIN_API_KEYS (ADR 0012 back-compat is optional).
    kp = tmp_path / "keys.yaml"
    kp.write_text("keys: []\n")
    s = Settings(
        auth_enabled=True,
        api_keys=["user-key"],
        admin_enabled=True,
        admin_api_keys=[],
        keys_path=str(kp),
        registry_path=str(fake_registry_file),
    )
    s.validate_runtime()  # must not raise


def test_bad_rate_limit_window_rejected(settings):
    settings.rate_limit_window_seconds = 0
    with pytest.raises(ConfigError):
        settings.validate_runtime()


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


def test_cache_aware_is_a_valid_route_strategy(fake_registry_file):
    s = Settings(route_strategy="cache_aware", registry_path=str(fake_registry_file))
    assert s.route_strategy == "cache_aware"


def test_bad_route_strategy_rejected(fake_registry_file):
    with pytest.raises(PydanticValidationError):
        Settings(route_strategy="nonsense", registry_path=str(fake_registry_file))


def test_cache_aware_defaults_preserve_legacy(settings):
    # Cache-aware settings exist but default strategy stays sticky (off).
    assert settings.route_strategy == "sticky"
    assert settings.cache_aware_beta == 0.5  # ADR 0018: retuned under the queueing model
    assert settings.cache_aware_block_chars == 128
    assert settings.cache_aware_use_metrics is False
    settings.validate_runtime()


@pytest.mark.parametrize(
    "field,value",
    [
        ("cache_aware_beta", -0.1),
        ("cache_aware_block_chars", 0),
        ("cache_aware_max_entries", -1),
        ("cache_aware_ttl_seconds", -1.0),
    ],
)
def test_cache_aware_invalid_values_rejected(settings, field, value):
    setattr(settings, field, value)
    with pytest.raises(ConfigError):
        settings.validate_runtime()


# --- ADR 0017 reliability v2 --------------------------------------------


def test_reliability_defaults_preserve_legacy(settings):
    # Every ADR 0017 knob defaults to v0.5.0 behavior.
    assert settings.max_ejection_percent == 0
    assert settings.cooldown_backoff_multiplier == 1.0
    assert settings.cooldown_max_seconds == 0.0
    assert settings.cooldown_decay_seconds == 0.0
    assert settings.cooldown_half_open_single_trial is False
    assert settings.retry_on_timeout is True
    assert settings.retry_on_connection is True
    assert settings.retry_on_server_error is True
    assert settings.retry_on_backpressure is False
    assert settings.cooldown_on_backpressure is False
    assert settings.retry_respect_retry_after is True
    assert settings.retry_max_attempts_by_class == {}
    assert settings.redis_sticky_max_entries == 0
    settings.validate_runtime()


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_ejection_percent", -1),
        ("max_ejection_percent", 101),
        ("cooldown_backoff_multiplier", 0.5),
        ("cooldown_max_seconds", -1.0),
        ("cooldown_decay_seconds", -1.0),
        ("cooldown_half_open_ttl_seconds", -1.0),
        ("redis_sticky_max_entries", -1),
    ],
)
def test_reliability_invalid_values_rejected(settings, field, value):
    setattr(settings, field, value)
    with pytest.raises(ConfigError):
        settings.validate_runtime()


def test_retry_by_class_unknown_class_rejected(settings):
    settings.retry_max_attempts_by_class = {"nonsense": 2}
    with pytest.raises(ConfigError):
        settings.validate_runtime()


def test_retry_by_class_negative_count_rejected(settings):
    settings.retry_max_attempts_by_class = {"timeout": -1}
    with pytest.raises(ConfigError):
        settings.validate_runtime()


def test_retry_by_class_valid_accepted(settings):
    settings.retry_max_attempts_by_class = {"timeout": 5, "server_error": 2}
    settings.validate_runtime()  # must not raise
