"""The channel-less write probe is explicitly opted into and bounded to one subscription."""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from youtube_migrator import cli
from youtube_migrator.backup import Backup
from youtube_migrator.errors import APIError
from youtube_migrator.state import State
from youtube_migrator.storage import atomic


@pytest.fixture
def check_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config"
    monkeypatch.setattr(cli, "CONFIG", config)
    exports = tmp_path / "exports"
    monkeypatch.setattr(cli, "EXPORTS", exports)
    source = MagicMock()
    source.identity.return_value = None
    source.subscriptions.return_value = {"UCsource1", "UCsource2"}
    source.playlists.return_value = []
    Backup(exports).export(youtube=source)
    atomic(config / "source_youtube_token.json", {"refresh_token": "fictional-source"})
    atomic(config / "destination_youtube_token.json", {"refresh_token": "fictional-destination"})
    existing = set()
    youtube = MagicMock()
    youtube.identity.return_value = None
    youtube.subscriptions.side_effect = lambda: existing.copy()
    youtube.subscribe.side_effect = existing.add
    youtube.caller.units = 0
    monkeypatch.setattr(cli, "yt", lambda account, write=False: youtube)
    return config, existing, youtube


def test_preview_makes_no_writes_or_journal(check_env):
    config, existing, youtube = check_env
    result = CliRunner().invoke(cli.app, ["check-subscription"])
    assert result.exit_code == 0, result.output
    assert "UCsource1" in result.output
    assert "Preview only" in result.output
    assert "fictional-destination" not in result.output
    assert not existing
    youtube.subscribe.assert_not_called()
    assert not cli.DEFAULT_STATE.exists()
    youtube.playlists.assert_not_called()


def test_apply_is_one_confirmed_subscription_not_a_full_import(check_env):
    _, existing, youtube = check_env
    result = CliRunner().invoke(cli.app, ["check-subscription", "--apply"])
    assert result.exit_code == 0, result.output
    assert existing == {"UCsource1"}
    youtube.subscribe.assert_called_once_with("UCsource1")
    youtube.create.assert_not_called()
    youtube.playlists.assert_not_called()
    assert "write confirmed" in result.output
    state = State(cli.DEFAULT_STATE)
    try:
        assert state.get("subscription:UCsource1")["status"] == "migrated"
        assert state.get("subscription:UCsource2") is None
        assert "youtube_credential_fingerprint" in state.get("binding")["destination"]
    finally:
        state.close()


def test_same_oauth_credential_is_refused_before_probe(check_env):
    config, _, youtube = check_env
    atomic(config / "destination_youtube_token.json", {"refresh_token": "fictional-source"})
    result = CliRunner().invoke(cli.app, ["check-subscription", "--apply"])
    assert result.exit_code == 1
    assert "credentials are identical" in result.output
    youtube.subscribe.assert_not_called()


def test_rejected_subscription_is_not_reported_success(check_env):
    _, existing, youtube = check_env
    youtube.subscribe.side_effect = APIError("youtubeSignupRequired", 401)
    result = CliRunner().invoke(cli.app, ["check-subscription", "--apply"])
    assert result.exit_code == 1
    assert "youtubeSignupRequired" in result.output
    assert "write confirmed" not in result.output
    assert not existing
    youtube.subscribe.assert_called_once()


def test_no_missing_subscriptions_never_writes(check_env):
    _, existing, youtube = check_env
    existing.update({"UCsource1", "UCsource2"})
    result = CliRunner().invoke(cli.app, ["check-subscription", "--apply"])
    assert result.exit_code == 0
    assert "already present" in result.output
    youtube.subscribe.assert_not_called()


def test_import_continues_channel_less_probe_journal_without_duplicate(check_env):
    _, existing, youtube = check_env
    runner = CliRunner()
    checked = runner.invoke(cli.app, ["check-subscription", "--apply"])
    assert checked.exit_code == 0, checked.output
    imported = runner.invoke(cli.app, ["import", "--max-subscriptions", "1"])
    assert imported.exit_code == 0, imported.output
    assert existing == {"UCsource1", "UCsource2"}
    assert [call.args[0] for call in youtube.subscribe.call_args_list] == ["UCsource1", "UCsource2"]
    youtube.create.assert_not_called()
    verified = runner.invoke(cli.app, ["verify"])
    assert verified.exit_code == 0, verified.output


def test_channel_less_subscription_batches_pause_and_resume(check_env):
    _, existing, youtube = check_env
    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", "--max-subscriptions", "1"])
    assert result.exit_code == 0, result.output
    assert existing == {"UCsource1"}
    assert "batch limit reached" in result.output
    resumed = runner.invoke(cli.app, ["resume", "--max-subscriptions", "1"])
    assert resumed.exit_code == 0, resumed.output
    assert existing == {"UCsource1", "UCsource2"}
    assert "finished" in resumed.output
    assert youtube.subscribe.call_count == 2


def test_playlist_backup_retains_channel_preflight(check_env):
    from youtube_migrator.errors import UserError

    _, _, youtube = check_env
    backup = {
        "manifest": {"services": ["youtube"]},
        "subscriptions": [],
        "playlists": [{"id": "PLexample"}],
    }
    youtube.identity.side_effect = UserError("This playlist import needs a destination channel")
    with pytest.raises(UserError, match="playlist import needs"):
        cli.destination(backup, True)
    youtube.identity.assert_called_once_with()
    youtube.subscribe.assert_not_called()


def test_subscriptions_only_skips_playlists_and_scope_persists_for_resume_and_verify(check_env):
    from youtube_migrator.models import Entry, Playlist
    from youtube_migrator.storage import read

    _, existing, youtube = check_env
    source = MagicMock()
    source.identity.return_value = None
    source.subscriptions.return_value = {"UCsource1", "UCsource2"}
    source.playlists.side_effect = APIError("channelNotFound", 404)
    backup = Backup(cli.EXPORTS)
    manifest = backup.export(
        youtube=source,
        watch=Playlist(
            "watch-later",
            "Watch Later (Migrated)",
            services=["watch_later"],
            entries=[Entry("one", "abcdefghijk")],
        ),
    )
    runner = CliRunner()
    imported = runner.invoke(
        cli.app, ["import", "--subscriptions-only", "--max-subscriptions", "1"]
    )
    assert imported.exit_code == 0, imported.output
    assert existing == {"UCsource1"}
    assert "1 playlist(s) excluded" in imported.output
    resumed = runner.invoke(cli.app, ["resume", "--max-subscriptions", "1"])
    assert resumed.exit_code == 0, resumed.output
    assert existing == {"UCsource1", "UCsource2"}
    verified = runner.invoke(cli.app, ["verify"])
    assert verified.exit_code == 0, verified.output
    report = read(cli.Path("data/reports/verification.json"))
    assert report["scope"] == "subscriptions"
    assert report["excluded_playlists"] == 1
    assert report["playlists"] == []
    assert report["source_export_completeness"] == "partial"
    assert backup.load()["manifest"]["generation"] == manifest["generation"]
    youtube.create.assert_not_called()
    youtube.add.assert_not_called()


def test_subscription_scope_dry_run_ignores_playlists_without_creating_journal(check_env):
    from youtube_migrator.models import Playlist

    _, _, youtube = check_env
    source = MagicMock()
    source.identity.return_value = None
    source.subscriptions.return_value = {"UCsource1"}
    source.playlists.return_value = []
    Backup(cli.EXPORTS).export(
        youtube=source, watch=Playlist("watch-later", "WL", services=["watch_later"])
    )
    result = CliRunner().invoke(cli.app, ["import", "--subscriptions-only", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert '"excluded_playlists": 1' in result.output
    assert '"playlists": []' in result.output
    youtube.identity.assert_not_called()
    assert not cli.DEFAULT_STATE.exists()


def test_all_data_can_explicitly_restore_playlist_preflight(check_env):
    from youtube_migrator.errors import UserError
    from youtube_migrator.models import Playlist

    _, _, youtube = check_env
    source = MagicMock()
    source.identity.return_value = None
    source.subscriptions.return_value = {"UCsource1"}
    source.playlists.return_value = []
    Backup(cli.EXPORTS).export(
        youtube=source, watch=Playlist("watch-later", "WL", services=["watch_later"])
    )
    runner = CliRunner()
    assert runner.invoke(cli.app, ["import", "--subscriptions-only"]).exit_code == 0
    youtube.identity.side_effect = UserError("Playlist preflight requires an API-visible channel")
    result = runner.invoke(cli.app, ["resume", "--all-data"])
    assert result.exit_code == 1
    assert "Playlist preflight" in result.output
    state = State(cli.DEFAULT_STATE)
    try:
        assert state.get("migration_scope") == "subscriptions"
    finally:
        state.close()
    youtube.create.assert_not_called()


def test_quota_pause_closes_journal_and_resume_keeps_scope(check_env):
    from youtube_migrator.errors import QuotaError

    _, existing, youtube = check_env
    runner = CliRunner()
    youtube.subscribe.side_effect = QuotaError("quotaExceeded", 403)
    paused = runner.invoke(cli.app, ["import", "--subscriptions-only"])
    assert paused.exit_code == 1
    assert "quotaExceeded" in paused.output
    assert State.peek(cli.DEFAULT_STATE, "migration_scope") == "subscriptions"
    assert "quota exhausted" in State.peek(cli.DEFAULT_STATE, "status")
    youtube.subscribe.side_effect = existing.add
    resumed = runner.invoke(cli.app, ["resume"])
    assert resumed.exit_code == 0, resumed.output
    assert existing == {"UCsource1", "UCsource2"}
    assert youtube.subscribe.call_count == 3
