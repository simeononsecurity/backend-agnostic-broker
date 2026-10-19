"""Authentication tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def open_client(noauth_settings):
    app = create_app(noauth_settings)
    with TestClient(app) as c:
        yield c


def test_missing_key_rejected(client):
    r = client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_invalid_key_rejected(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_valid_bearer_key(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200


def test_valid_x_api_key(client):
    r = client.get("/v1/models", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200


def test_healthz_bypasses_auth(client):
    assert client.get("/healthz").status_code == 200


def test_auth_disabled_allows_all(open_client):
    assert open_client.get("/v1/models").status_code == 200


def test_non_ascii_key_returns_401_not_500(client):
    # A non-ASCII byte in the key must not raise an unhandled TypeError from
    # hmac.compare_digest (which would be a 500 on an unauthenticated path).
    # Pass raw bytes so the client sends a latin-1 byte on the wire.
    assert client.get("/v1/models", headers={"Authorization": b"Bearer k\xe9y"}).status_code == 401
    assert client.get("/v1/models", headers={"X-API-Key": b"k\xe9y"}).status_code == 401


def test_overlong_key_returns_401(client):
    assert client.get("/v1/models", headers={"X-API-Key": "a" * 20000}).status_code == 401


def test_check_auth_non_ascii_raises_autherror_not_typeerror(settings):
    # Direct regression for the shipped broker/auth.py bug: a non-ASCII presented
    # key must raise AuthError (→ 401), not a bare TypeError (→ 500).
    from broker.auth import check_auth
    from broker.errors import AuthError

    with pytest.raises(AuthError):
        check_auth(settings, "Bearer k\xe9y", None)
