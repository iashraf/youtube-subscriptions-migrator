"""Immutable export generations with one atomic pointer to a complete generation."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .errors import InputError
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

    def export(self, youtube=None, music=None, watch=None, aliases=None, progress=None) -> dict:
        progress = progress or (lambda message: None)
        playlists, subscriptions, warnings, identities = [], [], [], {}
        services = []
        if youtube:
            services.append("youtube")
            identities["youtube_channel"] = youtube.identity()
            subscriptions = sorted(youtube.subscriptions())
            for p in youtube.playlists():
                p.entries = youtube.entries(p.id)
                expected = p.metadata["youtube"].get("contentDetails", {}).get("itemCount")
                if expected is not None and expected != len(p.entries):
                    warnings.append(f"Playlist {p.id}: count differs from reported itemCount")
                playlists.append(p)
                progress(f"YouTube playlist exported: {p.id} ({len(p.entries)} entries)")
        if music:
            services.append("music")
            identities["music"] = music.identity()
            if not identities["music"].get("channel_handle"):
                warnings.append(
                    "Music returned no channel handle; confirm the source account manually"
                )
            music_playlists = music.playlists()
            for p in music_playlists:
                expected = p.metadata["music"].get("trackCount")
                if isinstance(expected, int) and expected != len(p.entries):
                    warnings.append(f"Music playlist {p.id}: incomplete track count")
            playlists.extend(music_playlists)
            warnings.extend(music.warnings)
        if watch:
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
            "completeness": "with_warnings" if warnings else "complete",
        }
        data = {"subscriptions": subscriptions, "playlists": [p.dump() for p in unified]}
        manifest["content_sha256"] = digest(data)
        folder = self.root / generation
        atomic(folder / "backup.json", data)
        atomic(folder / "subscriptions.json", subscriptions)
        atomic(
            folder / "youtube_playlist_items.json",
            {p.id: [e.__dict__ for e in p.entries] for p in playlists if "youtube" in p.services},
        )
        atomic(
            folder / "youtube_playlists.json",
            [p.dump() for p in playlists if "youtube" in p.services],
        )
        atomic(
            folder / "youtube_music_playlists.json",
            [p.dump() for p in playlists if "music" in p.services],
        )
        atomic(folder / "watch_later.json", watch.dump() if watch else None)
        atomic(folder / "unavailable.json", unavailable)
        atomic(folder / "manifest.json", manifest)
        atomic(self.root / "manifest.json", manifest)
        return manifest


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
