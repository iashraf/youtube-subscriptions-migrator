"""Independent source/destination OAuth for the official YouTube Data API."""

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .errors import InputError, UserError
from .storage import atomic, read

READ = "https://www.googleapis.com/auth/youtube.readonly"
WRITE = "https://www.googleapis.com/auth/youtube.force-ssl"


def youtube_credentials(
    config: Path,
    account: str,
    write: bool = False,
    setup: bool = False,
    force_reauth: bool = False,
) -> Credentials:
    if force_reauth and not setup:
        raise InputError("Re-authentication must be requested through setup")
    scope = WRITE if write else READ
    path = config / f"{account}_youtube_token.json"
    creds = None
    if path.exists() and not force_reauth:
        info = read(path)
        if scope not in info.get("scopes", []):
            if not setup:
                raise UserError(f"Re-run setup --account {account} for required scope")
        else:
            creds = Credentials.from_authorized_user_info(info)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        atomic(path, json.loads(creds.to_json()))
    if not creds or not creds.valid:
        if not setup:
            raise UserError(f"Run setup --account {account}")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config / "client_secret.json"), [scope], autogenerate_code_verifier=True
        )
        creds = flow.run_local_server(
            host="127.0.0.1",
            bind_addr="0.0.0.0",
            port=int(os.getenv("OAUTH_PORT", "8080")),
            open_browser=False,
            authorization_prompt_message="Open this URL on the Docker host: {url}",
            prompt="select_account consent",
            access_type="offline",
            timeout_seconds=300,
        )
        atomic(path, json.loads(creds.to_json()))
    return creds
