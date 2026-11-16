from unittest.mock import MagicMock

import pytest

from youtube_migrator.clients import Music, YouTube
from youtube_migrator.errors import Caller


@pytest.mark.parametrize(
    "resource,kwargs",
    [
        ("subscriptions", {"part": "snippet", "mine": True}),
        ("playlists", {"part": "snippet", "mine": True}),
        ("playlistItems", {"part": "snippet", "playlistId": "PLsource"}),
    ],
)
def test_all_pagination(resource, kwargs):
    api = MagicMock()
    request = getattr(api, resource).return_value.list
    request.return_value.execute.side_effect = [
        {"items": [{"id": "first"}], "nextPageToken": "next"},
        {"items": [{"id": "last"}]},
    ]
    yt = YouTube(api=api, caller=Caller(0))
    assert yt.pages(resource, **kwargs) == [{"id": "first"}, {"id": "last"}]
    assert request.call_args_list[1].kwargs["pageToken"] == "next"
    assert yt.caller.units == 2


def test_youtube_order_unavailable_and_duplicates():
    api = MagicMock()
    api.playlistItems.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": str(i),
                "snippet": {
                    "position": i,
                    "title": title,
                    "resourceId": {"videoId": "abcdefghijk"},
                },
            }
            for i, title in [(2, "Deleted video"), (0, "Song"), (1, "Song")]
        ]
    }
    entries = YouTube(api=api, caller=Caller(0)).entries("PLsource")
    assert [e.key for e in entries] == ["0", "1", "2"]
    assert [e.available for e in entries] == [True, True, False]
    assert len(entries) == 3


def test_music_ownership_unlimited_and_duplicates():
    api = MagicMock()
    api.get_library_playlists.return_value = [
        {"playlistId": "PLowned", "owned": True},
        {"playlistId": "PLsaved", "owned": False},
        {"playlistId": "LM", "owned": True},
        {"playlistId": "SE", "owned": True},
        {"playlistId": "PLunknown"},
    ]
    api.get_playlist.return_value = {
        "title": "Songs",
        "tracks": [
            {"videoId": "abcdefghijk", "setVideoId": "1"},
            {"videoId": "abcdefghijk", "setVideoId": "2"},
        ],
    }
    client = Music(api, Caller(0))
    playlists = client.playlists()
    assert len(playlists) == 1
    assert len(playlists[0].entries) == 2
    api.get_library_playlists.assert_called_once_with(limit=None)
    api.get_playlist.assert_called_once_with("PLowned", limit=None)
    api.add_playlist_items.return_value = {"status": "STATUS_SUCCEEDED"}
    client.add("PLtarget", "abcdefghijk", 0)
    api.add_playlist_items.assert_called_once_with("PLtarget", ["abcdefghijk"], duplicates=True)


def test_music_subscribe_is_a_single_unretried_write():
    api = MagicMock()
    api.subscribe_artist.return_value = {"actions": []}
    client = Music(api, Caller(0))
    client.subscribe("UCchannel")
    api.subscribe_artist.assert_called_once_with("UCchannel")
    api.subscribe_artist.return_value = None
    with pytest.raises(Exception, match="musicSubscribeRejected"):
        client.subscribe("UCother")
