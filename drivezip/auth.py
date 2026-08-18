"""Credential resolution for the Drive backend.

Four ways in, tried in this order:

1. an access token passed explicitly or via `DRIVEZIP_ACCESS_TOKEN`
2. a service-account key file (`--service-account` or `GOOGLE_APPLICATION_CREDENTIALS`)
3. a cached OAuth user token, refreshed when stale
4. an interactive OAuth consent flow using a client-secrets file

Google libraries are imported lazily so the rest of the package — and the test
suite — works with nothing but the standard library installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import AuthError

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CONFIG_DIR = Path(os.environ.get("DRIVEZIP_CONFIG_DIR", Path.home() / ".config" / "drivezip"))
TOKEN_PATH = CONFIG_DIR / "token.json"
CLIENT_SECRETS_PATH = CONFIG_DIR / "client_secret.json"


class Credentials:
    """What `DriveRangeSource` needs: either a bearer token or a refreshable object."""

    def __init__(self, *, token: str | None = None, google_credentials: Any = None) -> None:
        self.token = token
        self.google_credentials = google_credentials


def resolve(
    *,
    access_token: str | None = None,
    service_account: str | os.PathLike[str] | None = None,
    client_secrets: str | os.PathLike[str] | None = None,
    allow_interactive: bool = True,
) -> Credentials:
    """Return usable credentials, or raise `AuthError` explaining what is missing."""
    token = access_token or os.environ.get("DRIVEZIP_ACCESS_TOKEN")
    if token:
        return Credentials(token=token)

    key_file = service_account or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key_file:
        return Credentials(google_credentials=_service_account_credentials(key_file))

    cached = _cached_user_credentials()
    if cached is not None:
        return Credentials(google_credentials=cached)

    secrets = Path(client_secrets) if client_secrets else CLIENT_SECRETS_PATH
    if secrets.is_file():
        if not allow_interactive:
            raise AuthError(
                f"no cached token at {TOKEN_PATH} and interactive login is disabled; "
                "run `drivezip login` once from a terminal"
            )
        return Credentials(google_credentials=_interactive_login(secrets))

    raise AuthError(
        "no Drive credentials found. Provide one of:\n"
        "  * DRIVEZIP_ACCESS_TOKEN=<token> for a one-off run\n"
        "  * --service-account key.json (or GOOGLE_APPLICATION_CREDENTIALS)\n"
        f"  * an OAuth client-secrets file at {CLIENT_SECRETS_PATH}, then `drivezip login`"
    )


def _service_account_credentials(key_file: str | os.PathLike[str]) -> Any:
    path = Path(key_file)
    if not path.is_file():
        raise AuthError(f"service-account key file not found: {path}")
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise AuthError(
            "google-auth is not installed; install the extra with `pip install drivezip[google]`"
        ) from exc

    try:
        return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise AuthError(f"{path} is not a valid service-account key: {exc}") from exc


def _cached_user_credentials() -> Any | None:
    if not TOKEN_PATH.is_file():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials as UserCredentials
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise AuthError(
            "google-auth is not installed; install the extra with `pip install drivezip[google]`"
        ) from exc

    credentials = UserCredentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _store_token(credentials)
    return credentials


def _interactive_login(secrets: Path) -> Any:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise AuthError(
            "google-auth-oauthlib is not installed; "
            "install the extra with `pip install drivezip[google]`"
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    credentials = flow.run_local_server(port=0)
    _store_token(credentials)
    return credentials


def _store_token(credentials: Any) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    TOKEN_PATH.chmod(0o600)


def login(client_secrets: str | os.PathLike[str] | None = None) -> Path:
    """Run the consent flow and cache the resulting token. Returns the token path."""
    secrets = Path(client_secrets) if client_secrets else CLIENT_SECRETS_PATH
    if not secrets.is_file():
        raise AuthError(
            f"OAuth client-secrets file not found at {secrets}.\n"
            "Create a Desktop-app OAuth client in Google Cloud Console, download the "
            f"JSON, and save it there (or pass --client-secrets)."
        )
    _interactive_login(secrets)
    return TOKEN_PATH
