from unittest.mock import MagicMock

import pytest

from youtube_migrator.clients import YouTube
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


@pytest.mark.parametrize("allow_missing", [False, True])
def test_no_channel_is_allowed_only_for_source_reads(allow_missing):
    from youtube_migrator.errors import UserError

    api = MagicMock()
    api.channels.return_value.list.return_value.execute.return_value = {"items": []}
    client = YouTube(api=api, caller=Caller(0))
    if allow_missing:
        assert client.identity(allow_missing=True) is None
    else:
        with pytest.raises(UserError, match="playlist import requires"):
            client.identity()


def test_multiple_channels_are_never_chosen_arbitrarily():
    from youtube_migrator.errors import UserError

    api = MagicMock()
    api.channels.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "UCone"}, {"id": "UCtwo"}]
    }
    with pytest.raises(UserError, match="returned 2 channels"):
        YouTube(api=api, caller=Caller(0)).identity(allow_missing=True)


@pytest.mark.parametrize("privacy", ["private", "unlisted"])
def test_creation_sets_only_nonpublic_privacy(privacy):
    from youtube_migrator.models import Playlist

    api = MagicMock()
    api.playlists.return_value.insert.return_value.execute.return_value = {"id": "PLtarget"}
    client = YouTube(api=api, caller=Caller(0))
    assert (
        client.create(Playlist("PLsource", "Video list", privacy=privacy), "marker") == "PLtarget"
    )
    body = api.playlists.return_value.insert.call_args.kwargs["body"]
    assert body["status"]["privacyStatus"] == privacy


def test_public_creation_is_rejected_before_any_api_call():
    from youtube_migrator.errors import InputError
    from youtube_migrator.models import Playlist

    api = MagicMock()
    client = YouTube(api=api, caller=Caller(0))
    with pytest.raises(InputError, match="public is forbidden"):
        client.create(Playlist("PLsource", "Video list", privacy="public"), "marker")
    api.playlists.assert_not_called()


def test_video_positions_sort_across_pages_without_collapsing_duplicates():
    api = MagicMock()

    def item(position, vid):
        return {
            "id": f"entry-{position}",
            "snippet": {"position": position, "title": "Video", "resourceId": {"videoId": vid}},
        }

    api.playlistItems.return_value.list.return_value.execute.side_effect = [
        {"items": [item(2, "abcdefghijk"), item(0, "abcdefghijk")], "nextPageToken": "next"},
        {"items": [item(3, "12345678901"), item(1, "ABCDEFGHIJK")]},
    ]
    result = YouTube(api=api, caller=Caller(0)).entries("PLsource")
    assert [e.video_id for e in result] == [
        "abcdefghijk",
        "ABCDEFGHIJK",
        "abcdefghijk",
        "12345678901",
    ]
    assert [e.key for e in result] == ["entry-0", "entry-1", "entry-2", "entry-3"]


def test_insert_sends_the_explicit_position():
    api = MagicMock()
    YouTube(api=api, caller=Caller(0)).add("PLtarget", "abcdefghijk", 7)
    snippet = api.playlistItems.return_value.insert.call_args.kwargs["body"]["snippet"]
    assert snippet["position"] == 7
    assert snippet["resourceId"]["videoId"] == "abcdefghijk"
