import json
import zipfile

import pytest

from youtube_migrator.watch_later import parse


@pytest.mark.parametrize(
    "name,text",
    [
        ("input.csv", "video_id\nabcdefghijk\nabcdefghijk\n"),
        (
            "input.csv",
            "Playlist Id,Playlist Title\nWL,Watch later\n\nVideo ID,Time Added\n"
            "abcdefghijk,2020\nabcdefghijk,2021\n",
        ),
        ("input.json", '["abcdefghijk", {"url": "https://youtu.be/abcdefghijk"}]'),
        ("input.json", '{"videos":[{"video_id":"abcdefghijk"}, "abcdefghijk"]}'),
    ],
)
def test_formats_preserve_occurrences(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    playlist = parse(p)
    assert len(playlist.entries) == 2
    assert playlist.entries[0].key != playlist.entries[1].key
    assert playlist.privacy == "private"


def test_takeout_and_missing(tmp_path):
    p = tmp_path / "takeout.zip"
    with zipfile.ZipFile(p, "w") as archive:
        archive.writestr(
            "Takeout/YouTube/playlists/Watch later-videos.csv",
            "Video ID,Time Added\nabcdefghijk,2020",
        )
    assert len(parse(p).entries) == 1
    with zipfile.ZipFile(p, "w") as archive:
        archive.writestr("history.json", json.dumps([]))
    with pytest.raises(ValueError, match="No Watch Later"):
        parse(p)


def test_invalid_id_is_not_discarded(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("video_id\ninvalid\n")
    with pytest.raises(ValueError, match="Invalid video ID"):
        parse(p)
