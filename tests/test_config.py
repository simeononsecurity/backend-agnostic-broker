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
