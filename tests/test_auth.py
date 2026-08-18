from __future__ import annotations

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
