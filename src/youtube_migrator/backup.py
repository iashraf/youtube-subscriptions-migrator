"""Immutable export generations with one atomic pointer to a complete generation."""

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .errors import APIError, InputError
from .models import merge
from .storage import atomic, read


class Backup:
    def __init__(self, root: Path):
        self.root = root

    def load(self) -> dict:
        manifest = read(self.root / "manifest.json")
        if manifest["schema_version"] != 1:
            raise InputError("Unsupported backup schema")
        generation = manifest["generation"]
        if not isinstance(generation, str) or not generation.isalnum():
            raise InputError("Invalid backup generation")
        data = read(self.root / generation / "backup.json")
        if digest(data) != manifest.get("content_sha256"):
            raise InputError("Backup integrity check failed; restore the original export files")
        return {**data, "manifest": manifest}

    def export(
        self,
        youtube=None,
        watch=None,
        aliases=None,
        progress=None,
        playlist_ids: list[str] | None = None,
        subscriptions_only: bool = False,
    ) -> dict:
        if subscriptions_only and (watch or playlist_ids):
            raise InputError("--subscriptions-only cannot include playlist inputs")
        if playlist_ids and youtube is None:
            raise InputError("Explicit playlist IDs require source YouTube credentials")
        progress = progress or (lambda message: None)
        playlists, subscriptions, warnings, identities = [], [], [], {}
        services = []
        coverage = {}
        partial = False
        if youtube:
            services.append("youtube")
            identities["youtube_channel"] = youtube.identity(allow_missing=True)
            if identities["youtube_channel"] is None:
                warnings.append(
                    "No source YouTube channel ID was returned; confirm the source alias manually"
                )
            subscriptions = sorted(youtube.subscriptions())
            youtube_playlists = []
            if not subscriptions_only:
                try:
                    youtube_playlists = youtube.playlists()
                    coverage["youtube_owned_playlists"] = "complete"
                except APIError as exc:
                    if identities["youtube_channel"] is not None or exc.reason != "channelNotFound":
                        raise
                    partial = True
                    coverage["youtube_owned_playlists"] = "not_accessible: channelNotFound"
                    warnings.append(
                        "Official YouTube owned playlists could not be listed (channelNotFound). "
                        "Subscriptions were exported. Use explicit --playlist-id values for "
                        "accessible shared/unlisted playlists; full playlist coverage is unknown."
                    )
            else:
                coverage["youtube_owned_playlists"] = "not_requested"
            for p in youtube_playlists:
                p.entries = youtube.entries(p.id)
                expected = p.metadata["youtube"].get("contentDetails", {}).get("itemCount")
                if expected is not None and expected != len(p.entries):
                    warnings.append(f"Playlist {p.id}: count differs from reported itemCount")
                playlists.append(p)
                progress(f"YouTube playlist exported: {p.id} ({len(p.entries)} entries)")
            known = {p.id for p in playlists}
            for pid in dict.fromkeys(playlist_ids or []):
                if pid in {"WL", "LL"}:
                    raise InputError(
                        "Native Watch Later and liked videos are not supported API playlists"
                    )
                if pid not in known:
                    p = youtube.playlist(pid)
                    p.metadata["explicitly_selected"] = True
                    playlists.append(p)
                    known.add(pid)
                    progress(f"Explicit video playlist exported: {pid} ({len(p.entries)} entries)")
            if playlist_ids:
                coverage["explicit_playlist_ids"] = list(dict.fromkeys(playlist_ids))
        if watch:
            watch = replace(
                watch,
                id="watch-later",
                title="Watch Later (Migrated)",
                privacy="private",
                services=["watch_later"],
            )
            services.append("watch_later")
            playlists.append(watch)
        else:
            warnings.append("Watch Later not supplied; native Watch Later is not API-accessible")
        unified, conflicts = merge(playlists)
        warnings.extend(conflicts)
        unavailable = [
            {"playlist_id": p.id, **e.__dict__}
            for p in unified
            for e in p.entries
            if not e.available or not e.video_id
        ]
        if unavailable:
            warnings.append(f"{len(unavailable)} identifiable entries unavailable in source")
        generation = uuid.uuid4().hex
        manifest = {
            "schema_version": 1,
            "generation": generation,
            "timestamp": datetime.now(UTC).isoformat(),
            "source": identities,
            "account_aliases": aliases or {},
            "services": services,
            "subscriptions": len(subscriptions),
            "playlists": len(unified),
            "entries": sum(len(p.entries) for p in unified),
            "warnings": warnings,
            "completeness": "partial" if partial else "with_warnings" if warnings else "complete",
            "coverage": coverage,
        }
        data = {"subscriptions": subscriptions, "playlists": [p.dump() for p in unified]}
        manifest["content_sha256"] = digest(data)
        folder = self.root / generation
        atomic(folder / "backup.json", data)
        atomic(folder / "unavailable.json", unavailable)
        atomic(folder / "manifest.json", manifest)
        atomic(self.root / "manifest.json", manifest)
        return manifest


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
