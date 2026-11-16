from copy import deepcopy

import pytest

from youtube_migrator.errors import AmbiguousError, APIError, QuotaError
from youtube_migrator.migration import Conflict, Migrator, plan
from youtube_migrator.models import Entry, Playlist, merge
from youtube_migrator.state import State


class Fake:
    def __init__(self):
        self.remote = {}
        self.subs = set()
        self.creates = 0
        self.adds = 0
        self.interrupt = None
        self.failure = None
        self.sub_failures = {}
        self.subscribed = []
        self.lag = 0

    def playlists(self):
        return list(deepcopy(self.remote).values())

    def playlist(self, pid):
        if pid not in self.remote:
            raise APIError("playlistNotFound", 404)
        p = deepcopy(self.remote[pid])
        if self.lag:
            # Simulate list results that briefly omit the newest write.
            self.lag -= 1
            p.entries = p.entries[:-1]
        return p

    def entries(self, pid):
        return self.playlist(pid).entries

    def create(self, p, marker):
        self.creates += 1
        target = deepcopy(p)
        target.id = "PLtarget"
        target.description += "\n" + marker
        target.entries = []
        self.remote[target.id] = target
        if self.interrupt == "create":
            self.interrupt = None
            raise KeyboardInterrupt()
        return target.id

    def add(self, pid, video_id, position):
        self.adds += 1
        if self.failure:
            exc, self.failure = self.failure, None
            raise exc
        self.remote[pid].entries.insert(position, Entry(str(self.adds), video_id))
        if self.interrupt == "add":
            self.interrupt = None
            raise KeyboardInterrupt()

    def subscriptions(self):
        return self.subs.copy()

    def subscribe(self, channel):
        self.subscribed.append(channel)
        if channel in self.sub_failures:
            raise self.sub_failures.pop(channel)
        self.subs.add(channel)
        if self.interrupt == "subscribe":
            self.interrupt = None
            raise KeyboardInterrupt()


@pytest.fixture
def env(tmp_path):
    p = Playlist(
        "PLsource",
        "Songs",
        "Description",
        "private",
        ["youtube"],
        [Entry("one", "abcdefghijk"), Entry("two", "abcdefghijk"), Entry("three", "12345678901")],
    )
    backup = {
        "manifest": {"generation": "generation", "services": ["youtube"]},
        "subscriptions": ["UCchannel"],
        "playlists": [p.dump()],
    }
    state = State(tmp_path / "state.db")
    client = Fake()
    yield backup, state, client
    state.close()


def engine(env, **kwargs):
    backup, state, client = env
    return Migrator(backup, state, client, settle=(), **kwargs)


def test_import_resume_and_occurrences(env):
    m = engine(env)
    m.run()
    m.run()
    _, state, client = env
    assert client.creates == 1
    assert client.adds == 3
    assert len(client.remote["PLtarget"].entries) == 3
    assert state.get("playlist:PLsource")["destination"] == "PLtarget"
    assert m.verify()["ok"]


@pytest.mark.parametrize("stage", ["create", "add", "subscribe"])
def test_interruption_remote_success_before_journal(env, stage):
    _, _, client = env
    client.interrupt = stage
    with pytest.raises(KeyboardInterrupt):
        engine(env).run()
    engine(env).run()
    assert client.creates == 1
    assert client.adds == 3
    assert engine(env).verify()["ok"]


def test_ambiguous_absent_write_is_never_blindly_retried(env):
    _, state, client = env
    client.failure = AmbiguousError("timeout")
    with pytest.raises(AmbiguousError):
        engine(env).run()
    assert state.get("playlist:PLsource")["pending"] == 0
    with pytest.raises(Conflict, match="uncertain insertion"):
        engine(env).run()
    assert client.adds == 1


def test_quota_pause_and_resume(env):
    _, state, client = env
    client.failure = QuotaError("quotaExceeded", 403)
    with pytest.raises(QuotaError):
        engine(env).run()
    assert "quota exhausted" in state.get("status")
    engine(env).run()
    assert client.creates == 1
    assert engine(env).verify()["ok"]


def test_permanent_failure_recorded_and_not_retried(env):
    _, state, client = env
    client.failure = APIError("videoNotFound", 404)
    engine(env).run()
    assert state.get("playlist:PLsource")["failed"]["0"]["status"] == "failed"
    assert not engine(env).verify()["ok"]
    adds = client.adds
    engine(env).run()
    assert client.adds == adds
    assert [e.video_id for e in client.remote["PLtarget"].entries] == ["abcdefghijk", "12345678901"]


def test_remote_edit_refused(env):
    engine(env).run()
    env[2].remote["PLtarget"].entries.append(Entry("external", "unrelated12"))
    with pytest.raises(Conflict, match="differs"):
        engine(env).run()
    assert not engine(env).verify()["ok"]


def test_mapping_removed_never_recreated(env):
    engine(env).run()
    env[2].remote.clear()
    with pytest.raises(APIError):
        engine(env).run()
    assert env[2].creates == 1


def test_verify_order_title_privacy_and_missing(env):
    engine(env).run()
    p = env[2].remote["PLtarget"]
    p.entries.reverse()
    p.title = "Changed"
    p.privacy = "public"
    report = engine(env).verify()
    assert not report["ok"]
    assert not report["playlists"][0]["sequence_matches"]
    assert not report["playlists"][0]["title_matches"]
    env[2].subs.clear()
    assert engine(env).verify()["subscriptions"][0]["status"] == "missing"


def test_state_binding_survives_reopen(tmp_path):
    path = tmp_path / "state.db"
    state = State(path)
    state.bind("backup", {"channel": "destination"})
    state.close()
    state = State(path)
    with pytest.raises(RuntimeError, match="another export"):
        state.bind("other", {"channel": "destination"})
    state.close()


def test_unavailable_recorded(env):
    env[0]["playlists"][0]["entries"][1]["available"] = False
    engine(env).run()
    report = engine(env).verify()
    assert not report["ok"]
    assert report["playlists"][0]["entries"][1]["status"] == "unavailable"


def test_create_quota_resumes_without_manual_mapping(env):
    client = env[2]
    create = client.create
    first = True

    def quota_once(p, marker):
        nonlocal first
        if first:
            first = False
            raise QuotaError("quotaExceeded", 403)
        return create(p, marker)

    client.create = quota_once
    with pytest.raises(QuotaError):
        engine(env).run()
    assert env[1].get("playlist:PLsource")["status"] == "create_retry"
    engine(env).run()
    assert client.creates == 1
    assert engine(env).verify()["ok"]


def test_destination_unavailable_is_not_success(env):
    engine(env).run()
    env[2].remote["PLtarget"].entries[0].available = False
    report = engine(env).verify()
    assert not report["ok"]
    assert report["playlists"][0]["entries"][0]["status"] == "unavailable"


def test_failed_duplicate_is_not_misattributed(env):
    env[2].failure = APIError("videoNotFound", 404)
    engine(env).run()
    report = engine(env).verify()
    assert [e["status"] for e in report["playlists"][0]["entries"]] == [
        "failed",
        "migrated",
        "migrated",
    ]


def test_false_success_leaves_pending_and_does_not_report_complete(env):
    client = env[2]
    client.add = lambda *args: {"status": "success"}
    with pytest.raises(Conflict, match="not confirmed"):
        engine(env).run()
    assert env[1].get("playlist:PLsource")["pending"] == 0
    assert not engine(env).verify()["ok"]


def test_permission_error_stops_without_marking_permanent_entry_failure(env):
    env[2].failure = APIError("insufficientPermissions", 403)
    with pytest.raises(APIError):
        engine(env).run()
    record = env[1].get("playlist:PLsource")
    assert not record["failed"]
    assert "pending" not in record
    engine(env).run()
    assert engine(env).verify()["ok"]


def test_listing_lag_is_settled_instead_of_pausing(env, monkeypatch):
    monkeypatch.setattr("youtube_migrator.migration.time.sleep", lambda s: None)
    backup, state, client = env
    client.add_original = client.add

    def lagging_add(*args):
        client.add_original(*args)
        client.lag = 2

    client.add = lagging_add
    Migrator(backup, state, client, settle=(1, 2, 4)).run()
    assert state.get("playlist:PLsource")["status"] == "complete"


def test_subscription_duplicate_counts_as_success(env):
    _, state, client = env
    client.sub_failures["UCchannel"] = APIError("subscriptionDuplicate", 400)
    client.subs = set()
    original = client.subscriptions
    client.subscriptions = lambda: original() | ({"UCchannel"} if client.subscribed else set())
    engine(env).run()
    assert state.get("subscription:UCchannel")["status"] == "migrated"
    assert engine(env).verify()["subscriptions"][0]["status"] == "migrated"


def test_subscription_throttle_pauses_and_retries_on_resume(env):
    _, state, client = env
    client.sub_failures["UCchannel"] = APIError("subscriptionLimitExceeded", 400)
    with pytest.raises(Conflict, match="skip-subscription"):
        engine(env).run()
    assert state.get("subscription:UCchannel")["status"] == "blocked"
    engine(env).run()
    assert state.get("subscription:UCchannel")["status"] == "migrated"


def test_skipped_subscription_is_not_retried_and_not_verified(env):
    _, state, client = env
    state.set("subscription:UCchannel", {"status": "skipped", "reason": "manual"})
    engine(env).run()
    assert client.subscribed == []
    report = engine(env).verify()
    assert not report["ok"]
    assert report["subscriptions"][0]["status"] == "skipped"


def test_unknown_channel_is_permanent_failure(env):
    _, state, client = env
    client.sub_failures["UCchannel"] = APIError("publisherNotFound", 404)
    engine(env).run()
    engine(env).run()
    assert client.subscribed == ["UCchannel"]
    assert state.get("subscription:UCchannel")["status"] == "failed"


def test_ambiguous_subscription_confirmed_by_single_listing(env):
    backup, state, client = env
    backup["subscriptions"] = ["UC1", "UC2"]
    client.sub_failures["UC1"] = AmbiguousError("timeout")
    calls = 0
    original = client.subscriptions

    def listing():
        nonlocal calls
        calls += 1
        # The timed-out write did commit remotely.
        return original() | {"UC1"} if "UC1" in client.subscribed else original()

    client.subscriptions = listing
    engine(env).run()
    assert calls == 2  # initial listing plus one confirmation pass, no per-channel reads
    assert state.get("subscription:UC1")["status"] == "migrated"
    assert engine(env).verify()["ok"]


def test_unconfirmed_subscription_pauses_then_resends(env):
    _, state, client = env
    client.subscribe = lambda channel: client.subscribed.append(channel)
    with pytest.raises(Conflict, match="not yet visible"):
        engine(env).run()
    assert state.get("subscription:UCchannel")["status"] == "sent"
    client.subscribe = lambda channel: client.subs.add(channel)
    engine(env).run()
    assert state.get("subscription:UCchannel")["status"] == "migrated"


def test_max_subscriptions_caps_attempts(env):
    backup, state, client = env
    backup["subscriptions"] = ["UC1", "UC2", "UC3"]
    engine(env, max_subscriptions=2).run()
    assert client.subscribed == ["UC1", "UC2"]
    assert not engine(env).verify()["ok"]
    engine(env).run()
    assert client.subscribed == ["UC1", "UC2", "UC3"]
    assert engine(env).verify()["ok"]


def test_continue_on_conflict_still_runs_subscriptions(env):
    backup, state, client = env
    engine(env).run()
    client.remote["PLtarget"].entries.append(Entry("external", "unrelated12"))
    backup["subscriptions"] = ["UCchannel", "UCnew"]
    with pytest.raises(Conflict, match="1 item"):
        engine(env, continue_on_conflict=True).run()
    assert "UCnew" in client.subs
    assert "paused" in state.get("status")


def test_source_public_playlist_becomes_private_without_changing_backup(env):
    env[0]["playlists"][0]["privacy"] = "public"
    engine(env).run()
    assert env[2].remote["PLtarget"].privacy == "private"
    assert env[0]["playlists"][0]["privacy"] == "public"
    assert env[1].get("playlist:PLsource")["privacy"] == "private"
    assert engine(env).verify()["ok"]


def test_unlisted_policy_is_saved_and_reused_on_resume_and_verify(env):
    engine(env, playlist_privacy="unlisted").run()
    assert env[2].remote["PLtarget"].privacy == "unlisted"
    engine(env).run()
    assert env[2].creates == 1
    assert env[1].get("playlist_privacy") == "unlisted"
    assert engine(env).verify()["ok"]
    with pytest.raises(Exception, match="already fixed"):
        engine(env, playlist_privacy="private")


def test_public_policy_is_rejected_before_remote_writes(env):
    with pytest.raises(Exception, match="public creation is forbidden"):
        engine(env, playlist_privacy="public")
    assert env[2].creates == env[2].adds == 0


def test_watch_later_is_a_regular_private_playlist_even_with_unlisted_policy(env):
    raw = env[0]["playlists"][0]
    raw.update(
        {
            "id": "watch-later",
            "title": "Watch Later (Migrated)",
            "services": ["watch_later"],
            "privacy": "public",
        }
    )
    engine(env, playlist_privacy="unlisted").run()
    assert env[2].remote["PLtarget"].title == "Watch Later (Migrated)"
    assert env[2].remote["PLtarget"].privacy == "private"
    assert engine(env).verify()["ok"]


def test_duplicate_positions_resume_after_third_remote_write_without_appending_twice(env):
    from unittest.mock import MagicMock

    raw = env[0]["playlists"][0]
    ordered = ["abcdefghijk", "ABCDEFGHIJK", "abcdefghijk", "12345678901"]
    raw["entries"] = [Entry(f"entry-{i}", vid).__dict__ for i, vid in enumerate(ordered)]
    client = env[2]
    original = client.add

    def interrupted_add(*args):
        original(*args)
        if client.adds == 3:
            raise KeyboardInterrupt()

    client.add = MagicMock(side_effect=interrupted_add)
    with pytest.raises(KeyboardInterrupt):
        engine(env).run()
    engine(env).run()
    assert [e.video_id for e in client.remote["PLtarget"].entries] == ordered
    assert [c.args[2] for c in client.add.call_args_list] == [0, 1, 2, 3]
    assert client.creates == 1
    assert engine(env).verify()["ok"]


def test_no_append_to_an_existing_public_destination(env):
    engine(env).run()
    env[2].remote["PLtarget"].privacy = "public"
    before = env[2].adds
    with pytest.raises(Conflict, match="privacy changed"):
        engine(env).run()
    assert env[2].adds == before
    assert not engine(env).verify()["ok"]


def test_playlist_deduplication_uses_ids_and_leaves_snapshots_untouched():
    first = Playlist("PLsame", "Same title", services=["youtube"], entries=[Entry("e1", "a")])
    second = Playlist("PLsame", "Same title", services=["youtube"], entries=[Entry("e2", "a")])
    other = Playlist("PLdifferent", "Same title", services=["youtube"])
    playlists, warnings = merge([first, second, other])
    assert len(playlists) == 2
    assert playlists[0].entries[0].key == "e1"
    assert first.entries[0].key == "e1" and second.entries[0].key == "e2"
    assert not warnings


def test_offline_plan_estimates_only_official_api_units(env):
    backup = env[0]
    backup["subscriptions"] = [f"UC{i}" for i in range(500)]
    result = plan(backup)
    assert result["estimated_youtube_units"] == 50 * 500 + 20 + 205
    assert result["estimated_days"] == 3
    assert result["playlists"][0]["privacy"] == "private"
