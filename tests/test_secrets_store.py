import sys
from base64 import b64decode, b64encode

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from jev_ultrafast import demo, model, secrets_store


def test_save_rejects_empty_or_control_characters():
    with pytest.raises(ValueError, match="vazia ou tem formato inválido"):
        secrets_store.save_openrouter_key(" \n ")


def encrypt_for_server(key):
    """What the browser does: RSA-OAEP / SHA-256 with the server's public key."""
    public = serialization.load_der_public_key(b64decode(secrets_store.transport_public_key()))
    return b64encode(public.encrypt(key.encode(), secrets_store._OAEP)).decode()


def test_settings_command_validates_then_saves_key_without_returning_it(monkeypatch):
    api_key = "sk-or-v1-settings-test-key"
    calls = []
    monkeypatch.setattr(demo, "SESSION", None)
    monkeypatch.setattr(demo, "validate_openrouter_key", lambda key: calls.append(("validate", key)))
    monkeypatch.setattr(demo, "save_openrouter_key", lambda key: calls.append(("save", key)))
    monkeypatch.setattr(demo, "openrouter_key_status", lambda: {
        "openrouter_key_configured": True,
        "openrouter_key_source": "encrypted",
    })

    result = demo.command("settings", {"encrypted_key": encrypt_for_server(api_key)})

    assert calls == [("validate", api_key), ("save", api_key)]
    assert api_key not in str(result)


def test_settings_command_does_not_save_a_rejected_key(monkeypatch):
    saved = []
    monkeypatch.setattr(demo, "SESSION", None)
    monkeypatch.setattr(demo, "save_openrouter_key", saved.append)

    def reject(_key):
        raise ValueError("A OpenRouter recusou esta chave.")

    monkeypatch.setattr(demo, "validate_openrouter_key", reject)
    with pytest.raises(ValueError, match="recusou"):
        demo.command("settings", {"encrypted_key": encrypt_for_server("sk-or-v1-bad")})
    assert saved == []


def test_settings_command_rejects_plaintext_or_garbage_key(monkeypatch):
    monkeypatch.setattr(demo, "SESSION", None)
    bodies = ({"openrouter_api_key": "sk-or-v1-plain"}, {"encrypted_key": "sk-or-v1-plain"}, {"encrypted_key": "AAAA"})
    for body in bodies:
        with pytest.raises(ValueError, match="Recarregue a página"):
            demo.command("settings", body)


@pytest.mark.parametrize(("status", "error", "match"), [
    (401, ValueError, "recusou"),
    (403, ValueError, "recusou"),
    (500, RuntimeError, "500"),
])
def test_validate_openrouter_key_status(monkeypatch, status, error, match):
    seen = {}

    def get(url, headers):
        seen.update(url=url, auth=headers["Authorization"])
        return httpx.Response(status)

    monkeypatch.setattr(model.CLIENT, "get", get)
    with pytest.raises(error, match=match):
        model.validate_openrouter_key("sk-or-v1-x")
    assert seen == {"url": model.KEY_URL, "auth": "Bearer sk-or-v1-x"}


def test_validate_openrouter_key_accepts_200_and_reports_connection_failure(monkeypatch):
    monkeypatch.setattr(model.CLIENT, "get", lambda url, headers: httpx.Response(200))
    model.validate_openrouter_key("sk-or-v1-x")

    def offline(url, headers):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(model.CLIENT, "get", offline)
    with pytest.raises(RuntimeError, match="falha de conexão"):
        model.validate_openrouter_key("sk-or-v1-x")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI is required")
def test_saved_key_is_encrypted_and_can_be_loaded(tmp_path, monkeypatch):
    key_path = tmp_path / "openrouter-api-key.dpapi"
    monkeypatch.setattr(secrets_store, "KEY_PATH", key_path)
    api_key = "sk-or-v1-local-test-key-never-used-for-provider-calls"

    secrets_store.save_openrouter_key(api_key)

    encrypted = key_path.read_bytes()
    assert api_key.encode() not in encrypted
    assert secrets_store.load_openrouter_key() == api_key


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI is required")
def test_model_prefers_encrypted_key_to_environment(tmp_path, monkeypatch):
    key_path = tmp_path / "openrouter-api-key.dpapi"
    monkeypatch.setattr(secrets_store, "KEY_PATH", key_path)
    api_key = "sk-or-v1-encrypted-precedence-test-key"
    secrets_store.save_openrouter_key(api_key)
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-test-key")

    assert model.openrouter_key() == api_key
    assert model.openrouter_key_status() == {
        "openrouter_key_configured": True,
        "openrouter_key_source": "encrypted",
    }
