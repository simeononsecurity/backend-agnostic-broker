"""Virtual-key storage, verification, and CLI tests (ADR 0008)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from broker import keys as keys_mod
from broker.keys import KeyStore, generate_secret, hash_secret


def _write_keys(path: Path, records: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"keys": records}), encoding="utf-8")


def _record(secret: str, **over) -> dict:
    kid = secret.split("_")[1]
    base = {"id": kid, "scope": "user", "secret_sha256": hash_secret(secret), "enabled": True}
    base.update(over)
    return base


# --- hashing / generation ---------------------------------------------------


def test_generate_secret_format():
    kid, secret = generate_secret()
    assert secret.startswith(f"brk_{kid}_")
    parts = secret.split("_")
    assert len(parts) == 3 and parts[0] == "brk" and parts[1] == kid


def test_hash_is_sha256_hex():
    s = "brk_abcd1234_deadbeef"
    assert hash_secret(s) == hashlib.sha256(s.encode()).hexdigest()
    assert len(hash_secret(s)) == 64


# --- resolution -------------------------------------------------------------


def test_resolve_valid_key(tmp_path):
    secret = "brk_tkey0001_" + "a" * 48
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, [_record(secret, label="alpha", tenant="t1", limits={"rpm": 10})])
    store = KeyStore(str(kp))
    p = store.resolve(secret)
    assert p is not None
    assert p.scope == "user" and p.tenant == "t1" and p.label == "alpha"
    assert p.limits.rpm == 10
    assert p.unlimited is False
    assert p.usage_id == "brk_tkey0001"


def test_resolve_wrong_secret_returns_none(tmp_path):
    secret = "brk_tkey0001_" + "a" * 48
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, [_record(secret)])
    store = KeyStore(str(kp))
    # Right id, wrong secret body → no match, no enumeration.
    assert store.resolve("brk_tkey0001_" + "b" * 48) is None


def test_resolve_unknown_id_returns_none(tmp_path):
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, [_record("brk_tkey0001_" + "a" * 48)])
    store = KeyStore(str(kp))
    assert store.resolve("brk_zzzzzzzz_" + "c" * 48) is None
    assert store.resolve("not-a-broker-key") is None
    assert store.resolve(None) is None


def test_resolve_disabled_returns_none(tmp_path):
    secret = "brk_tkey0002_" + "d" * 48
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, [_record(secret, enabled=False)])
    store = KeyStore(str(kp))
    assert store.resolve(secret) is None


def test_resolve_expired_returns_none(tmp_path):
    secret = "brk_tkey0003_" + "e" * 48
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, [_record(secret, expiry="2000-01-01")])
    store = KeyStore(str(kp))
    assert store.resolve(secret) is None


def test_unlimited_when_no_limits(tmp_path):
    secret = "brk_tkey0004_" + "f" * 48
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, [_record(secret)])  # no limits block
    p = KeyStore(str(kp)).resolve(secret)
    assert p is not None and p.unlimited is True


def test_legacy_key_is_unlimited_user():
    store = KeyStore(None, legacy_api_keys=["flat-key-123"])
    p = store.resolve("flat-key-123")
    assert p is not None
    assert p.unlimited is True and p.scope == "user"
    assert p.usage_id and p.usage_id.startswith("legacy_")
    # a stable, non-reversible id; the secret must not appear in it
    assert "flat-key-123" not in p.usage_id
    assert store.resolve("wrong") is None


def test_admin_scope_round_trip(tmp_path):
    secret = "brk_tkey0005_" + "1" * 48
    kp = tmp_path / "keys.yaml"
    _write_keys(kp, [_record(secret, scope="admin")])
    p = KeyStore(str(kp)).resolve(secret)
    assert p is not None and p.is_admin is True


# --- CLI --------------------------------------------------------------------


def test_cli_create_prints_secret_once_and_stores_hash(tmp_path, capsys):
    kp = tmp_path / "keys.yaml"
    rc = keys_mod.main(
        ["--path", str(kp), "create", "--label", "team", "--rpm", "30", "--tpm", "5000"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Exactly one secret line, of the brk_ form.
    secret_lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("brk_")]
    assert len(secret_lines) == 1
    secret = secret_lines[0]

    data = yaml.safe_load(kp.read_text())
    rec = data["keys"][0]
    # Only the HASH is stored, and it matches the printed secret.
    assert rec["secret_sha256"] == hash_secret(secret)
    assert rec["limits"]["rpm"] == 30 and rec["limits"]["tpm"] == 5000
    # Round-trips through the store.
    assert KeyStore(str(kp)).resolve(secret) is not None


def test_cli_create_no_plaintext_on_disk(tmp_path, capsys):
    kp = tmp_path / "keys.yaml"
    keys_mod.main(["--path", str(kp), "create", "--label", "x"])
    secret = next(
        ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("brk_")
    )
    disk = kp.read_text()
    # The raw secret must NEVER be written to disk — only its hash.
    assert secret not in disk
    assert hash_secret(secret) in disk


def test_cli_list_hides_secrets(tmp_path, capsys):
    kp = tmp_path / "keys.yaml"
    keys_mod.main(["--path", str(kp), "create", "--label", "visible-label"])
    created = capsys.readouterr().out
    secret = next(ln.strip() for ln in created.splitlines() if ln.strip().startswith("brk_"))

    keys_mod.main(["--path", str(kp), "list"])
    listed = capsys.readouterr().out
    assert "visible-label" in listed
    # Never leaks the secret or the stored hash.
    assert secret not in listed
    assert hash_secret(secret) not in listed


def test_cli_revoke_disables(tmp_path, capsys):
    kp = tmp_path / "keys.yaml"
    keys_mod.main(["--path", str(kp), "create"])
    secret = next(
        ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith("brk_")
    )
    assert KeyStore(str(kp)).resolve(secret) is not None

    kid = secret.split("_")[1]
    rc = keys_mod.main(["--path", str(kp), "revoke", kid])
    assert rc == 0
    # After revoke the key no longer authenticates.
    assert KeyStore(str(kp)).resolve(secret) is None
    # Revoking an unknown id is a clean failure.
    assert keys_mod.main(["--path", str(kp), "revoke", "nope9999"]) == 1
