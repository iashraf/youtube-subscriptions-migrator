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
