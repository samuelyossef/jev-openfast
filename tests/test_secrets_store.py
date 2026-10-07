import sys

import pytest

from jev_ultrafast import demo, model, secrets_store


def test_save_rejects_empty_or_control_characters():
    with pytest.raises(ValueError, match="vazia ou tem formato inválido"):
        secrets_store.save_openrouter_key(" \n ")


def test_settings_command_saves_key_without_returning_it(monkeypatch):
    api_key = "sk-or-v1-settings-test-key"
    saved = []
    monkeypatch.setattr(demo, "SESSION", None)
    monkeypatch.setattr(demo, "save_openrouter_key", saved.append)
    monkeypatch.setattr(demo, "openrouter_key_status", lambda: {
        "openrouter_key_configured": True,
        "openrouter_key_source": "encrypted",
    })

    result = demo.command("settings", {"openrouter_api_key": api_key})

    assert saved == [api_key]
    assert "openrouter_api_key" not in result
    assert api_key not in str(result)


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
