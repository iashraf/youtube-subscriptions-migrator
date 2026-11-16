"""Safe sequential importer and independent remote verification.

Remote playlist contents are an occurrence-preserving sequence, never a set.
Only a journalled single pending append may explain an extra remote entry.
"""

import hashlib
import math
import time
from dataclasses import replace

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


def allowed_privacy(value: str) -> str:
    if value not in {"private", "unlisted"}:
        raise UserError(
            "Playlist privacy must be private or unlisted; public creation is forbidden"
        )
    return value


class Migrator:
    def __init__(
        self,
        backup,
        state,
        youtube=None,
        progress=None,
        *,
        playlist_privacy: str | None = None,
        max_subscriptions=None,
        continue_on_conflict=False,
        settle=SETTLE,
    ):
        self.backup, self.state = backup, state
        self.youtube = youtube
        saved_privacy = state.get("playlist_privacy")
        if saved_privacy and playlist_privacy and saved_privacy != playlist_privacy:
            raise UserError(
                "Playlist privacy is already fixed in this journal; resume with its saved policy"
            )
        self.playlist_privacy = allowed_privacy(playlist_privacy or saved_privacy or "private")
        self.progress = progress or (lambda message: None)
        self.max_subscriptions = max_subscriptions
        self.subscription_batch_limited = False
        self.continue_on_conflict = continue_on_conflict
        self.settle = settle

    def target_privacy(self, p, record=None):
        record = record or {}
        if "watch_later" in p.services:
            return "private"
        return allowed_privacy(record.get("privacy") or self.playlist_privacy)

    def client(self):
        if self.youtube is None:
            raise UserError("Destination YouTube credentials required for video playlists")
        return self.youtube

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
        client = self.client()
        record["privacy"] = self.target_privacy(p, record)
        self.state.set(key, record)
        p = replace(p, privacy=record["privacy"])
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
                    # An unknown transport error may occur after a successful remote write.
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
        writer = self.youtube
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
                self.subscription_batch_limited = True
                continue
            attempts += 1
            self.state.set(key, {"status": "pending"})
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
                    self.state.set(key, {"status": "sent"})
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
            self.state.set(key, {"status": "sent"})
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
        self.subscription_batch_limited = False
        skipped = []
        try:
            if self.backup["playlists"]:
                self.state.set("playlist_privacy", self.playlist_privacy)
            # Complete each playlist serially before moving on to preserve entry positions.
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
                "status",
                "paused: subscription batch limit reached; run resume to continue"
                if self.subscription_batch_limited
                else f"finished with {failures} failed/unavailable entries; run verify",
            )
        except QuotaError as exc:
            self.state.set("status", "paused: quota exhausted; run resume when quota is available")
            self.state.set("quota_error", {"reason": exc.reason})
            raise
        except (APIError, Conflict) as exc:
            self.state.set("status", f"paused: {type(exc).__name__}; inspect and run resume")
            raise

    def verify(self):
        source_partial = self.backup["manifest"].get("completeness") == "partial"
        subscription_scope = self.backup.get("migration_scope") == "subscriptions"
        report = {
            "scope": "subscriptions" if subscription_scope else "all_exported_data",
            "excluded_playlists": self.backup.get("excluded_playlists", 0),
            "subscriptions": [],
            "playlists": [],
            "ok": subscription_scope or not source_partial,
            "source_export_completeness": self.backup["manifest"].get("completeness", "unknown"),
            "source_export_warnings": self.backup["manifest"].get("warnings", []),
        }
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
            "status": "missing",
            "entries": [],
            "source_count": len(p.entries),
        }
        if record.get("destination"):
            try:
                target_privacy = self.target_privacy(p, record)
                remote = self.client().playlist(record["destination"])
                actual = ids(remote.entries)
                expected = ids(p.entries)
                item.update(
                    {
                        "destination_count": len(actual),
                        "title_matches": p.title == remote.title,
                        "source_privacy": p.privacy,
                        "expected_privacy": target_privacy,
                        "privacy_matches": target_privacy == remote.privacy,
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


def plan(backup: dict, daily_quota: int = 10_000, playlist_privacy: str = "private") -> dict:
    """Estimate official API writes and paginated confirmation reads without authentication."""
    allowed_privacy(playlist_privacy)
    if daily_quota <= 0:
        raise UserError("Daily quota must be positive")
    subscriptions = len(backup["subscriptions"])
    units = 2 * math.ceil(subscriptions / 50) + 50 * subscriptions
    rows = []
    for raw in backup["playlists"]:
        p = Playlist.load(raw)
        n = sum(bool(e.available and e.video_id) for e in p.entries)
        cost = 50 + 2 + sum(50 + math.ceil(k / 50) for k in range(1, n + 1))
        units += cost
        rows.append(
            {
                "title": p.title,
                "entries": n,
                "youtube_units": cost,
                "privacy": "private" if "watch_later" in p.services else playlist_privacy,
            }
        )
    return {
        "mode": "offline upper-bound plan; destination not inspected",
        "scope": backup.get("migration_scope", "all"),
        "excluded_playlists": backup.get("excluded_playlists", 0),
        "subscriptions": subscriptions,
        "playlists": rows,
        "entries": sum(len(p["entries"]) for p in backup["playlists"]),
        "estimated_youtube_units": units,
        "assumed_daily_quota": daily_quota,
        "estimated_days": math.ceil(units / daily_quota) if units else 0,
        "note": "Existing subscriptions and journal entries reduce writes; settle reads and "
        "retries add units. Every playlist insertion includes an ordered read-back.",
    }


def summarize(report: dict) -> dict:
    """Report subscription, video playlist, and regular Watch Later copy results."""
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
    for service in ("youtube", "watch_later"):
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
