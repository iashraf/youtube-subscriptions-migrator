"""Service adapters. All pagination is exhausted before returning a snapshot."""

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from .errors import APIError, Caller, InputError, UserError
from .models import Entry, Playlist

AUTOMATIC_PLAYLISTS = {"LL", "WL"}


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

    def identity(self, allow_missing: bool = False) -> str | None:
        channels = self.pages("channels", part="id", mine=True)
        if not channels:
            if allow_missing:
                return None
            raise UserError(
                "Google returned no YouTube channel ID. This playlist import requires a "
                "verifiable destination channel. To proceed without a channel, run import "
                "--subscriptions-only using the same export and journal. This skips all "
                "playlist creation, including Watch Later. No new export is needed."
            )
        if len(channels) != 1:
            raise UserError(
                f"Google returned {len(channels)} channels; refusing to choose one arbitrarily. "
                "Re-run setup --account source/destination --reauth "
                "and choose the intended channel."
            )
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
        if p.privacy not in {"private", "unlisted"}:
            raise InputError("Created playlists must be private or unlisted; public is forbidden")
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
