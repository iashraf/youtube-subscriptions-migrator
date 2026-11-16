"""Read explicit CSV/JSON or named Watch Later files inside Takeout ZIP/TAR."""

import csv
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .errors import InputError
from .models import Entry, Playlist

ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_FILE = 64 * 1024 * 1024


def video_id(value: str) -> str:
    value = value.strip()
    if ID.fullmatch(value):
        return value
    url = urlparse(value)
    if url.hostname in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}:
        candidate = (
            url.path.strip("/")
            if url.hostname == "youtu.be"
            else parse_qs(url.query).get("v", [""])[0]
        )
        if ID.fullmatch(candidate):
            return candidate
    raise InputError("Invalid video ID or YouTube watch URL in Watch Later input")


def parse_text(name: str, content: str) -> list[Entry]:
    if name.lower().endswith(".json"):
        rows = json.loads(content)
        if isinstance(rows, dict):
            rows = rows.get("videos", rows.get("entries"))
        if not isinstance(rows, list):
            raise InputError("JSON must be a list or an object containing videos/entries")
    elif name.lower().endswith(".csv"):
        # Takeout can precede the video table with a playlist metadata table.
        lines = content.splitlines()
        start = next(
            (
                i
                for i, line in enumerate(lines)
                if line.split(",")[0].strip('" ').lower()
                in {"video id", "video_id", "videoid", "url"}
            ),
            None,
        )
        if start is None:
            raise InputError("CSV requires video_id, Video ID, videoId, or url header")
        rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    else:
        raise InputError("Supported Watch Later formats: CSV, JSON, ZIP, TAR, TAR.GZ")
    entries = []
    for i, row in enumerate(rows):
        value = (
            row
            if isinstance(row, str)
            else next(
                (row.get(k) for k in ("video_id", "Video ID", "videoId", "url") if row.get(k)), None
            )
        )
        if not value:
            raise InputError(f"Watch Later row {i + 1} is missing a video ID")
        entries.append(Entry(f"watch-later:{i}", video_id(value), metadata={"input": row}))
    return entries


def parse(path: Path) -> Playlist:
    candidates = []

    def eligible(name):
        base = Path(name).name.lower().replace("_", " ").replace("-", " ")
        return "watch later" in base and base.endswith((".csv", ".json"))

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if eligible(item.filename):
                    if item.file_size > MAX_FILE:
                        raise InputError("Watch Later member exceeds 64 MiB safety limit")
                    candidates.append((item.filename, archive.read(item).decode("utf-8-sig")))
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for item in archive:
                if item.isfile() and eligible(item.name):
                    if item.size > MAX_FILE:
                        raise InputError("Watch Later member exceeds 64 MiB safety limit")
                    with archive.extractfile(item) as stream:
                        candidates.append((item.name, stream.read().decode("utf-8-sig")))
    else:
        if path.stat().st_size > MAX_FILE:
            raise InputError("Watch Later input exceeds 64 MiB safety limit")
        candidates.append((path.name, path.read_text(encoding="utf-8-sig")))
    if not candidates:
        raise InputError("No Watch Later CSV/JSON found; use the documented manual input fallback")
    if len(candidates) > 1:
        raise InputError("Multiple Watch Later files found; provide one extracted file explicitly")
    entries = parse_text(*candidates[0])
    return Playlist(
        "watch-later",
        "Watch Later (Migrated)",
        privacy="private",
        services=["watch_later"],
        entries=entries,
    )
