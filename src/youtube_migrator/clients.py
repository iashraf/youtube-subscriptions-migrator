"""Service adapters. All pagination is exhausted before returning a snapshot."""

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from .errors import APIError, Caller, InputError, UserError
from .models import Entry, Playlist

AUTOMATIC_PLAYLISTS = {"LM", "LL", "WL", "SE"}


class YouTube:
    def __init__(self, credentials=None, api=None, caller=None):
        self.api = api or build(
            "youtube",
            "v3",
            http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
            cache_discovery=False,
        )
        self.caller = caller or Caller()

    def pages(self, resource: str, **kwargs) -> list[dict]:
        result, token = [], None
        while True:
            response = self.caller.call(
                lambda token=token: getattr(self.api, resource)()
                .list(**kwargs, maxResults=50, **({"pageToken": token} if token else {}))
                .execute(num_retries=0),
                units=1,
            )
            result.extend(response.get("items", []))
            token = response.get("nextPageToken")
            if not token:
                return result

    def identity(self) -> str:
        channels = self.pages("channels", part="id", mine=True)
        if len(channels) != 1:
            raise UserError("Expected one authenticated YouTube channel; select a channel at setup")
        return channels[0]["id"]

    def subscriptions(self) -> set[str]:
        return {
            s["snippet"]["resourceId"]["channelId"]
            for s in self.pages("subscriptions", part="snippet", mine=True)
        }

    def subscribe(self, channel: str):
        return self.caller.call(
            lambda: self.api.subscriptions()
            .insert(
                part="snippet",
                body={"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": channel}}},
            )
            .execute(num_retries=0),
            write=True,
            units=50,
        )

    def playlists(self) -> list[Playlist]:
        return [
            self._playlist(p)
            for p in self.pages("playlists", part="snippet,status,contentDetails", mine=True)
            if p["id"] not in AUTOMATIC_PLAYLISTS
        ]

    @staticmethod
    def _playlist(raw: dict) -> Playlist:
        return Playlist(
            raw["id"],
            raw["snippet"]["title"],
            raw["snippet"].get("description", ""),
            raw.get("status", {}).get("privacyStatus", "private"),
            ["youtube"],
            metadata={"youtube": raw},
        )

    def playlist(self, pid: str) -> Playlist:
        items = self.pages("playlists", part="snippet,status,contentDetails", id=pid)
        if not items:
            raise APIError("playlistNotFound", 404)
        p = self._playlist(items[0])
        p.entries = self.entries(pid)
        return p

    def entries(self, pid: str) -> list[Entry]:
        items = self.pages("playlistItems", part="snippet,contentDetails,status", playlistId=pid)
        items.sort(key=lambda x: x["snippet"]["position"])
        result = []
        for i, item in enumerate(items):
            snippet = item["snippet"]
            vid = item.get("contentDetails", {}).get("videoId") or snippet.get(
                "resourceId", {}
            ).get("videoId")
            available = bool(vid) and snippet.get("title") not in {"Private video", "Deleted video"}
            result.append(Entry(item.get("id", f"{pid}:{i}"), vid, available, item))
        return result

    def create(self, p: Playlist, marker: str) -> str:
        if len(p.description + "\n" + marker) > 5000:
            raise InputError(
                "Description plus recovery marker exceeds 5000 characters. "
                "Create an empty destination manually and use map-playlist; "
                "metadata is not truncated"
            )
        response = self.caller.call(
            lambda: self.api.playlists()
            .insert(
                part="snippet,status",
                body={
                    "snippet": {"title": p.title, "description": p.description + "\n" + marker},
                    "status": {"privacyStatus": p.privacy},
                },
            )
            .execute(num_retries=0),
            write=True,
            units=50,
        )
        return response["id"]

    def add(self, pid: str, video_id: str, position: int):
        return self.caller.call(
            lambda: self.api.playlistItems()
            .insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": pid,
                        "position": position,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            )
            .execute(num_retries=0),
            write=True,
            units=50,
        )


class Music:
    def __init__(self, api, caller=None, progress=None):
        self.api, self.caller = api, caller or Caller()
        self.warnings: list[str] = []
        self.progress = progress or (lambda message: None)

    def identity(self) -> dict:
        info = self.caller.call(self.api.get_account_info)
        return {
            "channel_handle": info.get("channelHandle"),
            "account_name": info.get("accountName"),
        }

    def playlists(self) -> list[Playlist]:
        library = self.caller.call(lambda: self.api.get_library_playlists(limit=None))
        library = [p for p in library if p.get("playlistId") not in AUTOMATIC_PLAYLISTS]
        for p in library:
            if "owned" not in p:
                self.warnings.append(
                    f"Music playlist {p.get('playlistId')}: ownership unknown; excluded"
                )
        playlists = []
        for p in library:
            if p.get("owned") is True:
                playlists.append(self.playlist(p["playlistId"]))
                self.progress(f"Music playlist read: {p['playlistId']}")
        return playlists

    def entries(self, pid: str) -> list[Entry]:
        return self.playlist(pid).entries

    def subscribe(self, channel: str):
        """Uses the web client's subscribe endpoint; consumes no Data API quota.

        Confirmation comes from a later Data API subscription listing, never this response.
        """
        result = self.caller.call(lambda: self.api.subscribe_artist(channel), write=True)
        if not isinstance(result, dict):
            raise APIError("musicSubscribeRejected")
        return result

    def playlist(self, pid: str) -> Playlist:
        raw = self.caller.call(lambda: self.api.get_playlist(pid, limit=None))
        if not isinstance(raw, dict) or "tracks" not in raw:
            raise APIError("invalidMusicPlaylist")
        entries = [
            Entry(
                t.get("setVideoId") or f"{pid}:{i}",
                t.get("videoId"),
                bool(t.get("videoId")) and t.get("isAvailable", True),
                t,
            )
            for i, t in enumerate(raw["tracks"])
        ]
        return Playlist(
            pid,
            raw["title"],
            raw.get("description") or "",
            (raw.get("privacy") or "private").lower(),
            ["music"],
            entries,
            {"music": raw},
        )

    def create(self, p: Playlist, marker: str) -> str:
        result = self.caller.call(
            lambda: self.api.create_playlist(
                p.title, p.description + "\n" + marker, privacy_status=p.privacy.upper()
            ),
            write=True,
        )
        if not isinstance(result, str) or not result.startswith("PL"):
            raise APIError("musicCreateRejected")
        return result

    def add(self, pid: str, video_id: str, position: int):
        result = self.caller.call(
            lambda: self.api.add_playlist_items(pid, [video_id], duplicates=True), write=True
        )
        if not isinstance(result, dict) or result.get("status") != "STATUS_SUCCEEDED":
            raise APIError("musicInsertRejected")
        return result
