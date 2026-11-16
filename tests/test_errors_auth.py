import json
from unittest.mock import MagicMock, patch

import pytest

from youtube_migrator.authentication import READ, WRITE, youtube_credentials
from youtube_migrator.errors import AmbiguousError, APIError, Caller, QuotaError
from youtube_migrator.storage import atomic, read


class HTTPFailure(Exception):
    def __init__(self, status, reason):
        self.resp = MagicMock(status=status)
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


@patch("youtube_migrator.errors.time.sleep")
def test_transient_read_retried(sleep):
    fn = MagicMock(side_effect=[HTTPFailure(503, "backendError"), {"ok": True}])
    assert Caller(0).call(fn, units=1) == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.parametrize(
    "status,reason,kind",
    [
        (403, "quotaExceeded", QuotaError),
        (400, "invalidValue", APIError),
        (401, "authError", APIError),
        (403, "insufficientPermissions", APIError),
        (503, "backendError", AmbiguousError),
        (429, "rateLimitExceeded", AmbiguousError),
    ],
)
def test_writes_never_blindly_retried(status, reason, kind):
    fn = MagicMock(side_effect=HTTPFailure(status, reason))
    with pytest.raises(kind):
        Caller(0).call(fn, write=True)
    assert fn.call_count == 1


@patch("youtube_migrator.authentication.Credentials")
def test_token_refresh_persisted_independently(credentials, tmp_path):
    path = tmp_path / "source_youtube_token.json"
    atomic(path, {"scopes": [READ], "refresh_token": "test-only"})
    credentials.from_authorized_user_info.return_value.expired = True
    credentials.from_authorized_user_info.return_value.to_json.return_value = json.dumps(
        {"scopes": [READ], "refresh_token": "refreshed-test-only"}
    )
    youtube_credentials(tmp_path, "source")
    assert read(path)["refresh_token"] == "refreshed-test-only"
    assert not (tmp_path / "destination_youtube_token.json").exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_and_inadequate_scopes_are_actionable(tmp_path):
    with pytest.raises(RuntimeError, match="Run setup"):
        youtube_credentials(tmp_path, "source")
    atomic(tmp_path / "destination_youtube_token.json", {"scopes": [READ]})
    with pytest.raises(RuntimeError, match="Re-run setup"):
        youtube_credentials(tmp_path, "destination", write=True)


@patch("youtube_migrator.authentication.InstalledAppFlow")
def test_desktop_loopback_flow_and_write_scope(flow, tmp_path):
    credentials = flow.from_client_secrets_file.return_value.run_local_server.return_value
    credentials.to_json.return_value = json.dumps({"scopes": [WRITE]})
    youtube_credentials(tmp_path, "destination", write=True, setup=True)
    assert flow.from_client_secrets_file.call_args.args[1] == [WRITE]
    assert flow.from_client_secrets_file.call_args.kwargs["autogenerate_code_verifier"]
    args = flow.from_client_secrets_file.return_value.run_local_server.call_args.kwargs
    assert args["host"] == "127.0.0.1"
    assert args["bind_addr"] == "0.0.0.0"
    assert not args["open_browser"]


def test_music_http_status_survives_sdk_error_wrapping():
    from ytmusicapi.exceptions import YTMusicServerError

    fn = MagicMock(
        side_effect=YTMusicServerError("Server returned HTTP 503: unavailable.\nprivate")
    )
    with pytest.raises(AmbiguousError) as caught:
        Caller(0).call(fn, write=True)
    assert caught.value.status == 503
    assert "private" not in str(caught.value)
