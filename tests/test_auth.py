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
