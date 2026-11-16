from copy import deepcopy

import pytest

from youtube_migrator.errors import AmbiguousError, APIError, QuotaError
from youtube_migrator.migration import Conflict, Migrator, plan, route
from youtube_migrator.models import Entry, Playlist, merge
from youtube_migrator.state import State, same_binding


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


def test_shared_ids_merge_metadata_without_title_deduplication():
    yt = Playlist("PLsame", "Title", services=["youtube"], entries=[Entry("1", "a")])
    music = Playlist(
        "PLsame",
        "Title",
        services=["music"],
        entries=[Entry("2", "a")],
        metadata={"music": {"tracks": [{"videoId": "a"}]}},
    )
    other = Playlist("PLdifferent", "Title", services=["youtube"])
    playlists, warnings = merge([music, yt, other])
    assert len(playlists) == 2
    assert playlists[0].services == ["music", "youtube"]
    assert playlists[0].entries[0].key == "1"
    assert not warnings


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


def test_music_only_import_duplicate_tracks(env):
    backup, state, client = env
    backup["subscriptions"] = []
    backup["playlists"][0]["services"] = ["music"]
    migrator = Migrator(backup, state, music=client, settle=())
    migrator.run()
    migrator.run()
    assert client.adds == 3
    assert migrator.verify()["ok"]


def test_shared_playlist_created_once_and_verified_through_both(env):
    backup, state, client = env
    p = backup["playlists"][0]
    p["services"] = ["youtube", "music"]
    p["metadata"]["music"] = {"tracks": [{"videoId": e["video_id"]} for e in p["entries"]]}
    migrator = Migrator(backup, state, client, client, settle=())
    migrator.run()
    assert client.creates == 1
    assert state.get("playlist:PLsource")["via"] == "music"
    report = migrator.verify()["playlists"][0]
    assert report["music_sequence_matches"] and report["youtube_sequence_matches"]
    p["metadata"]["music"]["tracks"].pop()
    assert not migrator.verify()["ok"]


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


def test_dedup_does_not_mutate_service_snapshots():
    yt = Playlist("PLsame", "Title", services=["youtube"])
    music = Playlist("PLsame", "Title", services=["music"])
    result, _ = merge([yt, music])
    assert result[0].services == ["music", "youtube"]
    assert yt.services == ["youtube"]
    assert music.services == ["music"]


def shared(p, music_ids=None):
    p = Playlist.load(p.dump() if isinstance(p, Playlist) else p)
    p.services = ["music", "youtube"]
    ids = music_ids if music_ids is not None else [e.video_id for e in p.entries]
    p.metadata["music"] = {"tracks": [{"videoId": v} for v in ids]}
    return p


def test_routing_prefers_music_only_when_views_match(env):
    p = Playlist.load(env[0]["playlists"][0])
    assert route(p) == "youtube"
    assert route(shared(p)) == "music"
    assert route(shared(p, ["abcdefghijk"])) == "youtube"
    assert route(shared(p), music=False) == "youtube"
    wl = Playlist("watch-later", "WL", services=["watch_later"])
    assert route(wl) == "youtube"
    assert route(Playlist("PLm", "M", services=["music"]), music=False) is None


def test_shared_playlist_written_via_music_uses_no_youtube_client(env):
    backup, state, music = env
    backup["playlists"][0] = shared(backup["playlists"][0]).dump()
    youtube = Fake()
    youtube.subs = {"UCchannel"}
    migrator = Migrator(backup, state, youtube, music, settle=())
    migrator.run()
    assert music.creates == 1 and music.adds == 3
    assert youtube.creates == youtube.adds == 0
    assert state.get("playlist:PLsource")["via"] == "music"


def test_divergent_shared_playlist_stays_on_youtube(env):
    backup, state, youtube = env
    backup["playlists"][0] = shared(backup["playlists"][0], ["abcdefghijk"]).dump()
    music = Fake()
    Migrator(backup, state, youtube, music, settle=()).run()
    assert youtube.adds == 3 and music.adds == 0


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


def test_subscribe_via_music_confirms_through_youtube(env):
    backup, state, youtube = env
    backup["playlists"] = []
    music = Fake()
    music.subs = youtube.subs  # same Google account seen through both services
    Migrator(backup, state, youtube, music, settle=(), subscribe_via="music").run()
    assert music.subscribed == ["UCchannel"] and youtube.subscribed == []
    assert state.get("subscription:UCchannel")["status"] == "migrated"


def test_continue_on_conflict_still_runs_subscriptions(env):
    backup, state, client = env
    engine(env).run()
    client.remote["PLtarget"].entries.append(Entry("external", "unrelated12"))
    backup["subscriptions"] = ["UCchannel", "UCnew"]
    with pytest.raises(Conflict, match="1 item"):
        engine(env, continue_on_conflict=True).run()
    assert "UCnew" in client.subs
    assert "paused" in state.get("status")


def test_music_binding_survives_reauthentication():
    old = {
        "backup": "g",
        "destination": {"music_channel_handle": "@new", "music_credential_fingerprint": "a"},
    }
    new = {
        "backup": "g",
        "destination": {"music_channel_handle": "@new", "music_credential_fingerprint": "b"},
    }
    assert same_binding(old, new)
    other = {
        "backup": "g",
        "destination": {"music_channel_handle": "@x", "music_credential_fingerprint": "a"},
    }
    assert not same_binding(old, other)
    no_handle = {"backup": "g", "destination": {"music_credential_fingerprint": "a"}}
    assert not same_binding(
        no_handle, {"backup": "g", "destination": {"music_credential_fingerprint": "b"}}
    )


def test_plan_routes_and_estimates_days(env):
    backup = env[0]
    backup["manifest"]["services"] = ["youtube", "music"]
    backup["subscriptions"] = [f"UC{i}" for i in range(500)]
    backup["playlists"] = [shared(backup["playlists"][0]).dump()]
    result = plan(backup)
    assert result["playlists"][0]["via"] == "music"
    assert result["estimated_youtube_units"] == 50 * 500 + 2 * 10
    assert result["estimated_days"] == 3
    assert plan(backup, subscribe_via="music")["estimated_youtube_units"] == 20
