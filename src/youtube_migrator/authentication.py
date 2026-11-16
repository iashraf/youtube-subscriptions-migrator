"""Independent desktop-loopback YouTube OAuth and Music device OAuth."""

import json
import os
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from ytmusicapi import OAuthCredentials, YTMusic, setup_oauth

from .errors import UserError
from .storage import atomic, read

READ = "https://www.googleapis.com/auth/youtube.readonly"
WRITE = "https://www.googleapis.com/auth/youtube.force-ssl"


def youtube_credentials(
    config: Path, account: str, write: bool = False, setup: bool = False
) -> Credentials:
    scope = WRITE if write else READ
    path = config / f"{account}_youtube_token.json"
    creds = None
    if path.exists():
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
            raise UserError(f"Run setup --account {account} --service youtube")
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


def music_client(config: Path, account: str, setup: bool = False) -> YTMusic:
    path = config / f"{account}_music_auth.json"
    info = read(config / "music_client_secret.json")
    info = info.get("installed", info)
    if setup:
        old_mask = os.umask(0o077)
        try:
            setup_oauth(
                info["client_id"],
                info["client_secret"],
                str(path),
                open_browser=False,
                session=TimeoutSession(),
            )
            path.chmod(0o600)
        finally:
            os.umask(old_mask)
    if not path.exists():
        raise UserError(f"Run setup --account {account} --service music")
    return YTMusic(
        str(path),
        requests_session=TimeoutSession(),
        oauth_credentials=OAuthCredentials(
            client_id=info["client_id"],
            client_secret=info["client_secret"],
            session=TimeoutSession(),
        ),
    )


class TimeoutSession(requests.Session):
    """Bound network waits; transport retries are owned by the operation journal."""

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 60)
        return super().request(method, url, **kwargs)
