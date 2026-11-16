from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from youtube_migrator import cli
from youtube_migrator.backup import Backup
from youtube_migrator.models import Entry, Playlist
from youtube_migrator.storage import atomic, read


def test_atomic_snapshot_and_failure_keeps_old_generation(tmp_path):
    youtube = MagicMock()
    youtube.identity.return_value = "UCsource"
    youtube.subscriptions.return_value = {"UC1", "UC2"}
    p = Playlist("PL1", "日本語", services=["youtube"], metadata={"youtube": {}})
    youtube.playlists.return_value = [p]
    youtube.entries.return_value = [Entry("entry", "abcdefghijk")]
    backup = Backup(tmp_path)
    manifest = backup.export(youtube=youtube)
    assert backup.load()["playlists"][0]["title"] == "日本語"
    assert manifest["subscriptions"] == 2
    youtube.entries.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError):
        backup.export(youtube=youtube)
    assert read(tmp_path / "manifest.json")["generation"] == manifest["generation"]


def test_dry_run_never_authenticates_or_creates_state(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    Backup(exports).export(
        watch=Playlist(
            "watch-later",
            "Watch Later (Migrated)",
            services=["watch_later"],
            entries=[Entry("1", "abcdefghijk")],
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "EXPORTS", exports)
    youtube = MagicMock(side_effect=AssertionError("must not authenticate"))
    monkeypatch.setattr(cli, "yt", youtube)
    result = CliRunner().invoke(cli.app, ["import", "--dry-run"])
    assert result.exit_code == 0, result.output
    # Create 50 + initial reads 2 + insert 50 + one-page read-back 1.
    assert '"estimated_youtube_units": 103' in result.output
    assert not (tmp_path / "data/state/migration.sqlite3").exists()
    youtube.assert_not_called()


def test_schema_rejected(tmp_path):
    atomic(tmp_path / "manifest.json", {"schema_version": 2})
    with pytest.raises(ValueError, match="schema"):
        Backup(tmp_path).load()


def test_cli_help():
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in [
        "setup",
        "export",
        "import",
        "resume",
        "status",
        "verify",
        "watch-later",
        "skip-subscription",
    ]:
        assert command in result.output


def test_backup_integrity_rejects_edited_entries(tmp_path):
    backup = Backup(tmp_path)
    manifest = backup.export(watch=Playlist("watch-later", "WL", services=["watch_later"]))
    atomic(tmp_path / manifest["generation"] / "backup.json", {"playlists": []})
    with pytest.raises(ValueError, match="integrity"):
        backup.load()


def test_channel_less_source_keeps_subscriptions_and_marks_playlist_coverage_partial(tmp_path):
    from youtube_migrator.errors import APIError
    from youtube_migrator.migration import Migrator
    from youtube_migrator.state import State

    youtube = MagicMock()
    youtube.identity.return_value = None
    youtube.subscriptions.return_value = {"UC1", "UC2"}
    youtube.playlists.side_effect = APIError("channelNotFound", 404)
    backup = Backup(tmp_path / "exports")
    manifest = backup.export(youtube=youtube)
    data = backup.load()
    assert data["subscriptions"] == ["UC1", "UC2"]
    assert data["playlists"] == []
    assert manifest["completeness"] == "partial"
    assert manifest["source"]["youtube_channel"] is None
    assert "channelNotFound" in " ".join(manifest["warnings"])
    state = State(tmp_path / "state.db")
    try:
        # Even if every exported subscription is present, inaccessible source collections
        # must prevent a full-verification success.
        report = Migrator({**data, "playlists": []}, state, youtube).verify()
        assert all(s["status"] == "already_present" for s in report["subscriptions"])
        assert not report["ok"]
        assert report["source_export_completeness"] == "partial"
    finally:
        state.close()


def test_no_channel_does_not_hide_unrelated_playlist_errors(tmp_path):
    from youtube_migrator.errors import APIError

    youtube = MagicMock()
    youtube.identity.return_value = None
    youtube.subscriptions.return_value = {"UC1"}
    youtube.playlists.side_effect = APIError("insufficientPermissions", 403)
    with pytest.raises(APIError):
        Backup(tmp_path).export(youtube=youtube)
    assert not (tmp_path / "manifest.json").exists()


def test_source_setup_accepts_channel_less_read_access(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    credentials = MagicMock()
    monkeypatch.setattr(cli, "youtube_credentials", credentials)
    youtube = MagicMock()
    youtube.identity.return_value = None
    youtube.subscriptions.return_value = {"UCone", "UCtwo"}
    monkeypatch.setattr(cli, "YouTube", lambda creds: youtube)
    result = CliRunner().invoke(cli.app, ["setup", "--account", "source", "--reauth"])
    assert result.exit_code == 0, result.output
    assert "2 subscriptions" in result.output
    assert "aliases are unverified" in result.output
    assert credentials.call_args.kwargs["force_reauth"] is True
    youtube.identity.assert_called_once_with(allow_missing=True)


def test_explicit_playlist_ids_export_order_and_duplicates_when_discovery_is_blocked(tmp_path):
    from youtube_migrator.errors import APIError

    youtube = MagicMock()
    youtube.identity.return_value = None
    youtube.subscriptions.return_value = {"UC1"}
    youtube.playlists.side_effect = APIError("channelNotFound", 404)
    videos = ["abcdefghijk", "ABCDEFGHIJK", "abcdefghijk"]
    youtube.playlist.return_value = Playlist(
        "PLshared",
        "Shared videos",
        privacy="public",
        services=["youtube"],
        entries=[Entry(str(i), vid) for i, vid in enumerate(videos)],
    )
    backup = Backup(tmp_path)
    manifest = backup.export(youtube=youtube, playlist_ids=["PLshared", "PLshared"])
    data = backup.load()
    assert manifest["completeness"] == "partial"
    assert len(data["playlists"]) == 1
    assert [e["video_id"] for e in data["playlists"][0]["entries"]] == videos
    youtube.playlist.assert_called_once_with("PLshared")


def test_subscription_only_export_skips_playlist_discovery(tmp_path):
    youtube = MagicMock()
    youtube.identity.return_value = None
    youtube.subscriptions.return_value = {"UC1"}
    manifest = Backup(tmp_path).export(youtube=youtube, subscriptions_only=True)
    assert manifest["completeness"] != "partial"
    assert manifest["coverage"]["youtube_owned_playlists"] == "not_requested"
    youtube.playlists.assert_not_called()


def test_public_privacy_option_fails_before_authentication(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    factory = MagicMock()
    monkeypatch.setattr(cli, "yt", factory)
    result = CliRunner().invoke(cli.app, ["import", "--playlist-privacy", "public"])
    assert result.exit_code == 1
    assert "public creation is forbidden" in result.output
    factory.assert_not_called()


def test_public_existing_playlist_cannot_be_adopted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    exports = tmp_path / "exports"
    youtube = MagicMock()
    youtube.identity.return_value = "UCsource"
    youtube.subscriptions.return_value = set()
    youtube.playlists.return_value = [
        Playlist(
            "PLsource", "Source", privacy="public", services=["youtube"], metadata={"youtube": {}}
        )
    ]
    youtube.entries.return_value = []
    Backup(exports).export(youtube=youtube)
    monkeypatch.setattr(cli, "EXPORTS", exports)
    target = MagicMock()
    target.identity.return_value = "UCdest"
    target.playlist.return_value = Playlist("PLdest", "Destination", privacy="public")
    monkeypatch.setattr(cli, "yt", lambda *args, **kwargs: target)
    result = CliRunner().invoke(cli.app, ["map-playlist", "PLsource", "PLdest"])
    assert result.exit_code == 1
    assert "non-public privacy" in result.output
    target.add.assert_not_called()


def test_unsupported_playlist_types_are_rejected():
    with pytest.raises(ValueError, match="Unsupported playlist type"):
        Playlist.load(Playlist("PLinvalid", "Invalid", services=["unsupported"]).dump())


def test_dry_run_uses_saved_privacy_without_changing_existing_state(tmp_path, monkeypatch):
    from youtube_migrator.state import State

    monkeypatch.chdir(tmp_path)
    exports = tmp_path / "exports"
    monkeypatch.setattr(cli, "EXPORTS", exports)
    youtube = MagicMock()
    youtube.identity.return_value = "UCsource"
    youtube.subscriptions.return_value = set()
    youtube.playlists.return_value = [
        Playlist("PLsource", "Videos", services=["youtube"], metadata={"youtube": {}})
    ]
    youtube.entries.return_value = []
    Backup(exports).export(youtube=youtube)
    with State(cli.DEFAULT_STATE) as state:
        state.set("playlist_privacy", "unlisted")
        before = state.summary()
    factory = MagicMock(side_effect=AssertionError("dry-run must not authenticate"))
    monkeypatch.setattr(cli, "yt", factory)
    result = CliRunner().invoke(cli.app, ["import", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert '"privacy": "unlisted"' in result.output
    with State(cli.DEFAULT_STATE) as state:
        assert state.summary() == before
    factory.assert_not_called()
