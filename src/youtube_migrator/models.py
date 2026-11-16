"""Portable version 1 backup models; entries retain positions and service metadata."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field


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
        return cls(**{**value, "entries": [Entry(**e) for e in value["entries"]]})


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
            warnings.append(
                f"Shared playlist {p.id}: service contents differ; YouTube is canonical"
            )
        if "youtube" in p.services:
            p.services = old.services
            p.metadata = old.metadata
            result[p.id] = p
    return list(result.values()), warnings
