"""Portable version 1 backup models; entries retain positions and service metadata."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field

from .errors import InputError


@dataclass
class Entry:
    key: str
    video_id: str | None
    available: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class Playlist:
    id: str
    title: str
    description: str = ""
    privacy: str = "private"
    services: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def dump(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, value: dict) -> "Playlist":
        services = value.get("services", ["youtube"])
        if not services or not set(services) <= {"youtube", "watch_later"}:
            raise InputError("Unsupported playlist type; use a YouTube export")
        return cls(
            **{**value, "services": services, "entries": [Entry(**e) for e in value["entries"]]}
        )


def merge(playlists: list[Playlist]) -> tuple[list[Playlist], list[str]]:
    result: dict[str, Playlist] = {}
    warnings = []
    for original in playlists:
        p = deepcopy(original)
        if p.id not in result:
            result[p.id] = p
            continue
        old = result[p.id]
        old.services = sorted(set(old.services + p.services))
        old.metadata.update(p.metadata)
        if [e.video_id for e in old.entries] != [e.video_id for e in p.entries]:
            warnings.append(f"Conflicting snapshots for playlist {p.id}; first sequence retained")
    return list(result.values()), warnings
