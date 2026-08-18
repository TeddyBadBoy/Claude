from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest

from drivezip import auth
from drivezip.errors import AuthError


def test_explicit_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIVEZIP_ACCESS_TOKEN", raising=False)
    credentials = auth.resolve(access_token="abc123")
    assert credentials.token == "abc123"
    assert credentials.google_credentials is None


def test_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIVEZIP_ACCESS_TOKEN", "from-env")
    assert auth.resolve().token == "from-env"


def test_missing_credentials_explain_the_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DRIVEZIP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(auth, "CLIENT_SECRETS_PATH", tmp_path / "client_secret.json")

    with pytest.raises(AuthError) as excinfo:
        auth.resolve()

    message = str(excinfo.value)
    assert "DRIVEZIP_ACCESS_TOKEN" in message
    assert "--service-account" in message


def test_missing_service_account_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DRIVEZIP_ACCESS_TOKEN", raising=False)
    with pytest.raises(AuthError, match="not found"):
        auth.resolve(service_account=tmp_path / "absent.json")


def test_login_without_client_secrets(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="client-secrets"):
        auth.login(tmp_path / "absent.json")


def test_inline_service_account_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIVEZIP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("DRIVEZIP_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    monkeypatch.setattr(auth, "_service_account_credentials_from_info", lambda raw: ("built", raw))

    credentials = auth.resolve()
    assert credentials.google_credentials == ("built", '{"type": "service_account"}')


def test_inline_key_accepts_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeServiceAccount:
        class Credentials:
            @staticmethod
            def from_service_account_info(info: dict[str, object], *, scopes: list[str]) -> str:
                captured["info"] = info
                captured["scopes"] = scopes
                return "credentials"

    module = types.ModuleType("google.oauth2")
    module.service_account = FakeServiceAccount  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.oauth2", module)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", FakeServiceAccount)  # type: ignore[arg-type]

    key = {"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n"}
    encoded = base64.b64encode(json.dumps(key).encode()).decode()

    assert auth._service_account_credentials_from_info(encoded) == "credentials"
    assert captured["info"] == key
    assert captured["scopes"] == auth.SCOPES


@pytest.mark.parametrize("value", ["not json at all", "!!!!", "eyJub3QiOiA="])
def test_inline_key_rejects_garbage(value: str) -> None:
    with pytest.raises(AuthError, match="DRIVEZIP_SERVICE_ACCOUNT_JSON"):
        auth._service_account_credentials_from_info(value)
