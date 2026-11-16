"""Safe sequential importer and independent remote verification.

Remote playlist contents are an occurrence-preserving sequence, never a set.
Only a journalled single pending append may explain an extra remote entry.
"""

import hashlib
import math
import time

from .errors import AmbiguousError, APIError, QuotaError, UserError
from .models import Playlist

AUTH_REASONS = {"insufficientPermissions", "authError"}
# Channel-specific outcomes that retrying cannot fix. Anything else unexpected (for example
# subscriptionLimitExceeded or subscriptionForbidden) pauses so throttling never becomes a
# silent permanent failure; skip-subscription records a deliberate skip.
PERMANENT_SUBSCRIPTION = {"publisherNotFound", "channelNotFound"}
# Waits (seconds) before re-reading remote state that may not yet show a confirmed write.
SETTLE = (1, 2, 4, 8)
SENT = {"sent", "pending"}


class Conflict(RuntimeError):
    pass


def ids(entries):
    return [e.video_id for e in entries]


def is_auth(exc: APIError) -> bool:
    return exc.status == 401 or exc.reason in AUTH_REASONS


def music_view_matches(p: Playlist) -> bool:
    tracks = p.metadata.get("music", {}).get("tracks")
    return tracks is not None and [t.get("videoId") for t in tracks] == ids(p.entries)


def route(p: Playlist, music: bool = True, youtube: bool = True) -> str | None:
    """Prefer ytmusicapi, which uses no Data API quota, when its view is the canonical sequence.

    Shared playlists whose Music view differs from YouTube stay on the official API so the
    written sequence can be confirmed by reading it back. Watch Later always uses YouTube.
    """
    if music and "watch_later" not in p.services:
        if "youtube" not in p.services or music_view_matches(p):
            return "music"
    if youtube and ("youtube" in p.services or "watch_later" in p.services):
        return "youtube"
    return None


class Migrator:
    def __init__(
        self,
        backup,
        state,
        youtube=None,
        music=None,
        progress=None,
        *,
        subscribe_via="youtube",
        max_subscriptions=None,
        continue_on_conflict=False,
        settle=SETTLE,
    ):
        self.backup, self.state = backup, state
        self.youtube, self.music = youtube, music
        self.progress = progress or (lambda message: None)
        self.subscribe_via = subscribe_via
        self.max_subscriptions = max_subscriptions
        self.continue_on_conflict = continue_on_conflict
        self.settle = settle

    def via(self, p):
        record = self.state.get("playlist:" + p.id) or {}
        return record.get("via") or route(p, self.music is not None, self.youtube is not None)

    def client(self, p, via=None):
        via = via or self.via(p)
        client = {"music": self.music, "youtube": self.youtube}.get(via)
        if client is None:
            raise UserError(f"Missing destination {via or 'service'} credentials for {p.id}")
        return client

    def settled(self, read, expected):
        """Re-read with short waits; a confirmed write can take a moment to appear in lists."""
        actual = read()
        for delay in self.settle:
            if actual == expected:
                break
            time.sleep(delay)
            actual = read()
        return actual

    def marker(self, pid):
        seed = self.backup["manifest"]["generation"] + ":" + pid
        return "[youtube-migrator:" + hashlib.sha256(seed.encode()).hexdigest()[:32] + "]"

    def playlist(self, p):
        key = "playlist:" + p.id
        record = self.state.get(key)
        marker = self.marker(p.id)
        if not record:
            record = {"status": "new", "destination": None, "done": [], "failed": {}}
        # The route is fixed when first journalled so a resume never switches services.
        if not record.get("via"):
            record["via"] = route(p, self.music is not None, self.youtube is not None)
            self.state.set(key, record)
        client = self.client(p, record["via"])
        if record["destination"] is None and record["status"] in {"new", "create_retry"}:
            record["status"] = "creating"
            self.state.set(key, record)
            # Persist intent before dispatch. Only definite quota/auth rejections may be retried.
            try:
                record["destination"] = client.create(p, marker)
            except QuotaError:
                record["status"] = "create_retry"
                self.state.set(key, record)
                raise
            except APIError as exc:
                if is_auth(exc):
                    record["status"] = "create_retry"
                elif 400 <= exc.status < 500:
                    record["status"] = "create_failed"
                record["creation_error"] = exc.reason
                self.state.set(key, record)
                raise
            except ValueError:
                record["status"] = "create_failed"
                self.state.set(key, record)
                raise
            self.state.set(key, record)
        if record["destination"] is None:
            matches = [x for x in client.playlists() if marker in x.description]
            if len(matches) != 1:
                raise Conflict(
                    f"{p.id}: unresolved playlist creation; found {len(matches)} markers. "
                    "Inspect destination and use map-playlist after manual reconciliation"
                )
            record["destination"] = matches[0].id
            self.state.set(key, record)
        destination = record["destination"]

        def read():
            return ids(client.entries(destination))

        remote = client.playlist(destination)
        if marker not in remote.description and not record.get("explicit_mapping"):
            raise Conflict(f"{p.id}: destination mapping marker changed; inspect before resuming")
        if remote.privacy != p.privacy:
            raise Conflict(f"{p.id}: destination privacy changed; restore it before resuming")
        done = record["done"]
        actual = ids(remote.entries)
        expected = [p.entries[i].video_id for i in done]
        pending = record.get("pending")
        if pending is not None:
            target = expected + [p.entries[pending].video_id]
            if actual != target:
                actual = self.settled(read, target)
            if actual == target:
                done.append(pending)
                record.pop("pending")
                record["failed"].pop(str(pending), None)
                self.state.set(key, record)
            else:
                # An absent result is not proof that a timed-out write cannot still commit.
                raise Conflict(
                    f"{p.id}: uncertain insertion at position {pending}; inspect remote "
                    "and use resolve-pending SOURCE_ID applied/absent"
                )
        elif actual != expected:
            raise Conflict(f"{p.id}: destination sequence differs from journal; no writes made")
        for i, entry in enumerate(p.entries):
            if i in done or str(i) in record["failed"]:
                continue
            if not entry.available or not entry.video_id:
                record["failed"][str(i)] = {"status": "unavailable", "entry": entry.key}
                self.state.set(key, record)
                continue
            record["pending"] = i
            self.state.set(key, record)
            try:
                client.add(destination, entry.video_id, len(done))
            except QuotaError:
                record.pop("pending")
                self.state.set(key, record)
                raise
            except AmbiguousError:
                raise
            except APIError as exc:
                if is_auth(exc):
                    record.pop("pending")
                    self.state.set(key, record)
                    raise
                if exc.status == 0:
                    # Unknown Music errors may occur after a successful remote write.
                    raise AmbiguousError(exc.reason) from None
                record.pop("pending")
                record["failed"][str(i)] = {"status": "failed", "reason": exc.reason}
                self.state.set(key, record)
                continue
            # Verify even an apparently successful response before recording completion.
            expected = [p.entries[j].video_id for j in done] + [entry.video_id]
            if self.settled(read, expected) != expected:
                raise Conflict(f"{p.id}: insertion response was not confirmed by remote contents")
            done.append(i)
            record.pop("pending")
            record["failed"].pop(str(i), None)
            record["status"] = "in_progress"
            self.state.set(key, record)
            self.progress(f"{p.title}: {len(done)}/{len(p.entries)} entries confirmed")
        record["status"] = "with_failures" if record["failed"] else "complete"
        self.state.set(key, record)

    def subscriptions(self):
        channels = self.backup["subscriptions"]
        if not channels:
            return
        if self.youtube is None:
            raise UserError("Destination YouTube credentials required for subscriptions")
        writer = self.music if self.subscribe_via == "music" else self.youtube
        if writer is None:
            raise UserError("--subscribe-via music requires destination Music credentials")
        existing = self.youtube.subscriptions()
        attempts = 0
        for n, channel in enumerate(channels, 1):
            key = "subscription:" + channel
            recorded = self.state.get(key, {}).get("status")
            if channel in existing:
                status = "migrated" if recorded in SENT | {"migrated"} else "already_present"
                if status != recorded:
                    self.state.set(key, {"status": status})
                continue
            if recorded in {"failed", "skipped"}:
                continue
            if self.max_subscriptions is not None and attempts >= self.max_subscriptions:
                continue
            attempts += 1
            self.state.set(key, {"status": "pending", "via": self.subscribe_via})
            try:
                writer.subscribe(channel)
            except QuotaError:
                self.state.set(key, {"status": "retry"})
                raise
            except AmbiguousError:
                # Subscribing is idempotent; the confirmation pass or resume settles it.
                continue
            except APIError as exc:
                if exc.reason == "subscriptionDuplicate":
                    self.state.set(key, {"status": "sent", "via": self.subscribe_via})
                    continue
                if is_auth(exc):
                    self.state.set(key, {"status": "retry"})
                    raise
                if exc.reason in PERMANENT_SUBSCRIPTION or (
                    exc.status == 404 and exc.reason != "subscriberNotFound"
                ):
                    self.state.set(key, {"status": "failed", "reason": exc.reason})
                    continue
                self.state.set(key, {"status": "blocked", "reason": exc.reason})
                raise Conflict(
                    f"Subscription {channel} rejected: {exc.reason} (HTTP {exc.status}). "
                    "If YouTube is throttling subscriptions, wait a few hours and run resume; "
                    f"if this channel cannot be subscribed, run skip-subscription {channel}"
                ) from None
            self.state.set(key, {"status": "sent", "via": self.subscribe_via})
            self.progress(f"Subscription sent: {channel} ({n}/{len(channels)})")
        # One list pass confirms every write instead of a per-channel read.
        unconfirmed = [
            c for c in channels if self.state.get("subscription:" + c, {}).get("status") in SENT
        ]
        if not unconfirmed:
            return
        for delay in (0, *self.settle):
            time.sleep(delay)
            actual = self.youtube.subscriptions()
            for channel in unconfirmed:
                if channel in actual:
                    self.state.set("subscription:" + channel, {"status": "migrated"})
            unconfirmed = [c for c in unconfirmed if c not in actual]
            if not unconfirmed:
                self.progress("All sent subscriptions confirmed")
                return
        raise Conflict(
            f"{len(unconfirmed)} sent subscriptions not yet visible on the destination; "
            "wait and run resume (resending is safe for subscriptions)"
        )

    def guarded(self, skipped, fn):
        try:
            fn()
        except (Conflict, AmbiguousError) as exc:
            if not self.continue_on_conflict:
                raise
            skipped.append(str(exc))
            self.progress(f"Skipped after conflict: {exc}")

    def run(self):
        skipped = []
        try:
            # Playlists first: Music-routed playlists need no Data API quota, so they complete
            # even if subscriptions later exhaust the day's allocation.
            for raw in self.backup["playlists"]:
                p = Playlist.load(raw)
                self.guarded(skipped, lambda p=p: self.playlist(p))
            self.guarded(skipped, self.subscriptions)
            if skipped:
                raise Conflict(
                    f"{len(skipped)} item(s) skipped after conflicts: " + " | ".join(skipped)
                )
            failures = sum(
                len(value.get("failed", {}))
                if key.startswith("playlist:")
                else int(value.get("status") in {"failed", "skipped"})
                for key, value in self.state.summary().items()
                if key.startswith(("playlist:", "subscription:"))
            )
            self.state.set(
                "status", f"finished with {failures} failed/unavailable entries; run verify"
            )
        except QuotaError as exc:
            self.state.set("status", "paused: quota exhausted; run resume when quota is available")
            self.state.set("quota_error", {"reason": exc.reason})
            raise
        except (APIError, Conflict) as exc:
            self.state.set("status", f"paused: {type(exc).__name__}; inspect and run resume")
            raise

    def verify(self):
        report = {"subscriptions": [], "playlists": [], "ok": True}
        if self.backup["subscriptions"]:
            try:
                actual = self.youtube.subscriptions()
            except (APIError, AttributeError):
                actual = None
            for channel in self.backup["subscriptions"]:
                recorded = self.state.get("subscription:" + channel, {})
                if actual is None:
                    status = "not_verifiable"
                elif channel in actual:
                    status = (
                        "already_present"
                        if recorded.get("status") in {None, "already_present"}
                        else "migrated"
                    )
                elif recorded.get("status") in {"failed", "skipped"}:
                    status = recorded["status"]
                else:
                    status = "missing"
                item = {"channel_id": channel, "status": status}
                if recorded.get("reason"):
                    item["reason"] = recorded["reason"]
                report["subscriptions"].append(item)
                report["ok"] &= status in {"migrated", "already_present"}
        for raw in self.backup["playlists"]:
            item = self.verify_playlist(Playlist.load(raw))
            report["ok"] &= item["status"] == "migrated"
            report["playlists"].append(item)
        report["summary"] = summarize(report)
        return report

    def verify_playlist(self, p):
        record = self.state.get("playlist:" + p.id, {})
        item = {
            "source_id": p.id,
            "destination_id": record.get("destination"),
            "services": p.services,
            "via": record.get("via"),
            "status": "missing",
            "entries": [],
            "source_count": len(p.entries),
        }
        if record.get("destination"):
            try:
                via = self.via(p)
                remote = self.client(p, via).playlist(record["destination"])
                actual = ids(remote.entries)
                expected = ids(p.entries)
                item.update(
                    {
                        "destination_count": len(actual),
                        "title_matches": p.title == remote.title,
                        "privacy_matches": p.privacy == remote.privacy,
                        "description_matches": remote.description
                        in {p.description, p.description + "\n" + self.marker(p.id)},
                        "sequence_matches": expected == actual,
                        "actual_video_ids": actual,
                    }
                )
                # An occurrence-aware subsequence alignment explains failed entries
                # without collapsing duplicate videos or shifting all following statuses.
                cursor = 0
                unavailable_remote = []
                for i, entry in enumerate(p.entries):
                    if not entry.available or not entry.video_id:
                        status = "unavailable"
                    elif str(i) in record.get("failed", {}) and expected != actual:
                        status = record["failed"][str(i)]["status"]
                    elif cursor < len(actual) and actual[cursor] == entry.video_id:
                        status = "migrated" if remote.entries[cursor].available else "unavailable"
                        if status == "unavailable":
                            unavailable_remote.append(cursor)
                        cursor += 1
                    else:
                        status = record.get("failed", {}).get(str(i), {}).get("status", "missing")
                    item["entries"].append(
                        {"key": entry.key, "video_id": entry.video_id, "status": status}
                    )
                item["destination_unavailable_positions"] = unavailable_remote
                checks = ("title_matches", "privacy_matches", "description_matches")
                item["status"] = (
                    "migrated"
                    if all(item[k] for k in (*checks, "sequence_matches"))
                    and all(e["status"] == "migrated" for e in item["entries"])
                    else "discrepancy"
                )
                # A shared playlist must also be visible and ordered correctly in both services.
                if set(p.services) >= {"youtube", "music"}:
                    self.verify_views(p, record["destination"], via, remote, item)
            except APIError as exc:
                item["status"] = "missing" if exc.status == 404 else "not_verifiable"
                item["reason"] = exc.reason
            except UserError:
                item["status"] = "not_verifiable"
        if not item["entries"] and p.entries:
            item["entries"] = [
                {
                    "key": e.key,
                    "video_id": e.video_id,
                    "status": "unavailable"
                    if not e.available
                    else "not_verifiable"
                    if item["status"] == "not_verifiable"
                    else "missing",
                }
                for e in p.entries
            ]
        return item

    def verify_views(self, p, destination, via, primary, item):
        sources = {
            "youtube": ids(p.entries),
            "music": [t.get("videoId") for t in p.metadata["music"].get("tracks", [])],
        }
        for service, source in sources.items():
            try:
                view = primary if service == via else self.client(p, service).playlist(destination)
            except (APIError, UserError):
                item[f"{service}_status"] = "not_verifiable"
                item["status"] = "not_verifiable"
                continue
            item[f"{service}_sequence_matches"] = ids(view.entries) == source
            item[f"{service}_unavailable_positions"] = [
                i for i, e in enumerate(view.entries) if not e.available
            ]
            if (
                not item[f"{service}_sequence_matches"] or item[f"{service}_unavailable_positions"]
            ) and item["status"] == "migrated":
                item["status"] = "discrepancy"


def plan(backup: dict, subscribe_via: str = "youtube", daily_quota: int = 10_000) -> dict:
    """Offline upper-bound estimate; assumes destination credentials for every exported service."""
    services = backup["manifest"]["services"]
    has_music = "music" in services
    has_youtube = "youtube" in services or "watch_later" in services
    subscriptions = len(backup["subscriptions"])
    units = music_requests = 0
    if subscriptions:
        # Initial subscription listing plus one confirmation pass, 50 per page.
        units += 2 * math.ceil(subscriptions / 50)
        if subscribe_via == "youtube":
            units += 50 * subscriptions
        else:
            music_requests += subscriptions
    rows = []
    for raw in backup["playlists"]:
        p = Playlist.load(raw)
        via = route(p, has_music, has_youtube)
        n = sum(bool(e.available and e.video_id) for e in p.entries)
        row_units = 0
        if via == "youtube":
            # Create, initial read, then each insert plus a paginated read-back of the playlist.
            row_units = 50 + 2 + sum(50 + math.ceil(k / 50) for k in range(1, n + 1))
        else:
            music_requests += 2 + 2 * n
        units += row_units
        rows.append({"title": p.title, "via": via, "entries": n, "youtube_units": row_units})
    return {
        "mode": "offline upper-bound plan; destination not inspected",
        "subscriptions": subscriptions,
        "subscribe_via": subscribe_via,
        "playlists": rows,
        "entries": sum(len(p["entries"]) for p in backup["playlists"]),
        "estimated_youtube_units": units,
        "assumed_daily_quota": daily_quota,
        "estimated_days": math.ceil(units / daily_quota) if units else 0,
        "music_requests": music_requests,
        "note": "Existing destination state reduces writes; settle re-reads and retries add "
        "units. Music requests use no Data API quota; their limits are undocumented.",
    }


def summarize(report: dict) -> dict:
    """Counts retain separate views for shared YouTube/Music playlists."""
    result = {}
    subscriptions = report["subscriptions"]
    result["subscriptions"] = {
        "source": len(subscriptions),
        "confirmed": sum(s["status"] in {"migrated", "already_present"} for s in subscriptions),
        "statuses": {
            status: sum(s["status"] == status for s in subscriptions)
            for status in {s["status"] for s in subscriptions}
        },
    }
    for service in ("youtube", "music", "watch_later"):
        playlists = [p for p in report["playlists"] if service in p["services"]]
        entries = [e for p in playlists for e in p["entries"]]
        result[service] = {
            "source_playlists": len(playlists),
            "verified_playlists": sum(p["status"] == "migrated" for p in playlists),
            "source_entries": sum(p["source_count"] for p in playlists),
            "entry_statuses": {
                status: sum(e["status"] == status for e in entries)
                for status in {e["status"] for e in entries}
            },
        }
    return result
