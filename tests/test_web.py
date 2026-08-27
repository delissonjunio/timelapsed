"""Exercises the viewer over real HTTP against a live server on a random port."""
import json
import struct
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from timelapsed.image_capture_library import _parse_image_filename
from timelapsed.schema import CADENCES
from timelapsed.web import THUMBNAIL_WIDTH, TimelapseCatalogue, build_server
from tests.conftest import BASE_TIME, requires_ffmpeg

VIDEO_BODY = b"".join(bytes([index % 256]) for index in range(4096))


@pytest.fixture
def stocked_library(library, tmp_path: Path):
    """Two channels with a mix of cadences, so filtering has something to filter."""
    source = tmp_path / "render.mp4"
    source.write_bytes(VIDEO_BODY)

    for channel_id, cadence, days_ago in [
        ("1", "hourly", 0),
        ("1", "daily", 1),
        ("1", "weekly", 7),
        ("2", "daily", 2),
    ]:
        starts = BASE_TIME - timedelta(days=days_ago)
        library.store_timelapse(channel_id, source, cadence, starts, starts + timedelta(hours=1))
    return library


@pytest.fixture
def base_url(stocked_library, config):
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(url: str, headers: dict | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, dict(response.headers), response.read()


# --- catalogue -------------------------------------------------------------

def test_catalogue_lists_channels_that_have_timelapses(stocked_library):
    assert TimelapseCatalogue(stocked_library.root_path).channels() == ["1", "2"]


def test_catalogue_on_an_empty_root_is_empty(tmp_path: Path):
    catalogue = TimelapseCatalogue(tmp_path / "missing")

    assert catalogue.channels() == []
    assert catalogue.entries() == []


def test_catalogue_sorts_newest_first(stocked_library):
    entries = TimelapseCatalogue(stocked_library.root_path).entries()

    assert [entry.starts for entry in entries] == sorted(
        (entry.starts for entry in entries), reverse=True
    )


@pytest.mark.parametrize("channel, cadence, expected", [
    (None, None, 4),
    ("1", None, 3),
    ("2", None, 1),
    (None, "daily", 2),
    ("1", "daily", 1),
    ("1", "weekly", 1),
    ("2", "weekly", 0),
])
def test_catalogue_filters(stocked_library, channel, cadence, expected):
    catalogue = TimelapseCatalogue(stocked_library.root_path)

    assert len(catalogue.entries(channel, cadence)) == expected


def test_catalogue_refuses_path_traversal(stocked_library):
    catalogue = TimelapseCatalogue(stocked_library.root_path)

    assert catalogue.resolve_video("1", "../../../etc/passwd") is None
    assert catalogue.resolve_video("1", "nope.mp4") is None


# --- HTTP ------------------------------------------------------------------

def embedded_payload(body: bytes) -> list[dict]:
    """The catalogue the page ships with, which is what the timeline draws from."""
    marker = b'<script type="application/json" id="payload">'
    start = body.index(marker) + len(marker)
    return json.loads(body[start:body.index(b"</script>", start)].replace(b"<\\/", b"</"))


def test_index_renders_and_embeds_every_video(base_url):
    status, headers, body = get(base_url + "/")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"Timelapsed" in body
    assert len(embedded_payload(body)) == 4


def test_index_filters_by_channel_and_cadence(base_url):
    _, _, all_videos = get(base_url + "/")
    _, _, channel_one = get(base_url + "/?channel=1")
    _, _, weekly = get(base_url + "/?cadence=weekly")

    assert len(embedded_payload(all_videos)) == 4
    assert len(embedded_payload(channel_one)) == 3
    assert {e["cadence"] for e in embedded_payload(weekly)} == {"weekly"}


def test_index_lays_out_a_lane_per_cadence(base_url):
    _, _, body = get(base_url + "/")

    # The whole point of the timeline: overlapping cadences get their own row.
    for cadence in (b"hourly", b"daily", b"weekly"):
        assert b'"' + cadence + b'"' in body
    # The lane order is injected from the registry rather than written into the
    # page, so adding a cadence gets a lane without touching the viewer. Widest
    # on top, which is the order the lanes are drawn in.
    assert b'id="cadences-payload">["progress","monthly","weekly","daily","hourly"]' in body


def test_every_registered_cadence_has_a_lane_colour(base_url):
    """A lane whose colour is missing falls back to grey and reads as a bug."""
    _, _, body = get(base_url + "/")

    for cadence in CADENCES:
        assert f"--{cadence}:".encode() in body


def test_a_crafted_filename_stays_data_in_the_embedded_payload(base_url, stocked_library):
    # Everything before the first underscore is read back as the cadence name, so
    # a file on disk controls a string the page renders. A path cannot contain a
    # slash, so it cannot close the script block, but it can still carry markup.
    hostile = '<img src=x onerror=alert(1)>_20260101_000000_UTC-20260101_010000_UTC'
    (stocked_library.root_path / "1" / "timelapse" / f"{hostile}.mp4").write_bytes(b"x")

    _, _, body = get(base_url + "/")
    payload = embedded_payload(body)

    assert any(entry["cadence"] == "<img src=x onerror=alert(1)>" for entry in payload)

    # Every occurrence is confined to the JSON block, never live markup. It shows
    # up twice there: once as the cadence, once inside the video URL.
    block_start = body.index(b'id="payload"')
    block_end = body.index(b"</script>", block_start)
    assert body.count(b"<img src=x") == 2
    assert body[block_start:block_end].count(b"<img src=x") == 2


def test_index_is_valid_when_nothing_has_rendered(config, tmp_path):
    config.image_capture_library_root = tmp_path / "empty"
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        status, _, body = get(f"http://{host}:{port}/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert embedded_payload(body) == []
    assert b"Nothing rendered for this camera yet" in body


def test_api_returns_json(base_url):
    status, headers, body = get(base_url + "/api/timelapses")
    payload = json.loads(body)

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert len(payload) == 4
    assert set(payload[0]) == {"channel", "cadence", "starts", "finishes", "size_bytes", "url"}
    assert payload[0]["size_bytes"] == len(VIDEO_BODY)


def test_healthz(base_url):
    assert get(base_url + "/healthz")[0] == 200


def test_unknown_paths_are_404(base_url):
    for path in ["/nope", "/video/", "/video/1/missing.mp4", "/video/99/x.mp4"]:
        with pytest.raises(urllib.error.HTTPError) as raised:
            get(base_url + path)
        assert raised.value.code == 404


def test_video_downloads_in_full(base_url):
    payload = json.loads(get(base_url + "/api/timelapses")[2])
    status, headers, body = get(base_url + payload[0]["url"])

    assert status == 200
    assert body == VIDEO_BODY
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Type"] == "video/mp4"


def test_video_serves_a_byte_range(base_url):
    payload = json.loads(get(base_url + "/api/timelapses")[2])

    status, headers, body = get(base_url + payload[0]["url"], {"Range": "bytes=100-199"})

    assert status == 206
    assert body == VIDEO_BODY[100:200]
    assert headers["Content-Range"] == f"bytes 100-199/{len(VIDEO_BODY)}"
    assert headers["Content-Length"] == "100"


def test_video_serves_an_open_ended_range(base_url):
    payload = json.loads(get(base_url + "/api/timelapses")[2])

    status, _, body = get(base_url + payload[0]["url"], {"Range": "bytes=4000-"})

    assert status == 206
    assert body == VIDEO_BODY[4000:]


def test_video_serves_a_suffix_range(base_url):
    payload = json.loads(get(base_url + "/api/timelapses")[2])

    status, _, body = get(base_url + payload[0]["url"], {"Range": "bytes=-50"})

    assert status == 206
    assert body == VIDEO_BODY[-50:]


def test_an_out_of_bounds_range_is_rejected(base_url):
    payload = json.loads(get(base_url + "/api/timelapses")[2])

    with pytest.raises(urllib.error.HTTPError) as raised:
        get(base_url + payload[0]["url"], {"Range": "bytes=99999-"})

    assert raised.value.code == 416


def test_path_traversal_over_http_is_refused(base_url):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(base_url + "/video/1/%2e%2e%2f%2e%2e%2fetc%2fpasswd")

    assert raised.value.code == 404


# --- camera thumbnails -----------------------------------------------------

@pytest.fixture
def library_with_stills(stocked_library, jpeg_bytes):
    """Channel 1 has stills, channel 2 has none, channel 3 has stills but no renders."""
    for channel_id in ("1", "3"):
        for minutes_ago in (5, 3, 1):
            stocked_library.store_image(
                channel_id, "jpg", jpeg_bytes, BASE_TIME - timedelta(minutes=minutes_ago)
            )
    return stocked_library


@pytest.fixture
def stills_url(library_with_stills, config):
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_catalogue_finds_the_newest_still(library_with_stills):
    catalogue = TimelapseCatalogue(library_with_stills.root_path)

    latest = catalogue.latest_still("1")

    assert latest is not None
    assert _parse_image_filename(latest.stem) == BASE_TIME - timedelta(minutes=1)
    assert catalogue.latest_still("2") is None
    assert catalogue.latest_still("nope") is None


def jpeg_width(data: bytes) -> int:
    """Width from the first JPEG start-of-frame marker."""
    index = 2
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return struct.unpack(">H", data[index + 7:index + 9])[0]
        index += 2 + struct.unpack(">H", data[index + 2:index + 4])[0]
    raise ValueError("no start-of-frame marker")


@requires_ffmpeg
def test_thumbnail_is_served_and_bounded_to_the_tile_width(stills_url, jpeg_bytes):
    status, headers, body = get(stills_url + "/thumb/1.jpg")

    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["Cache-Control"] == "no-store"
    assert body.startswith(b"\xff\xd8")  # JPEG SOI
    assert jpeg_width(body) <= THUMBNAIL_WIDTH
    # Never upscaled past the source, so a small still does not grow.
    assert jpeg_width(body) <= jpeg_width(jpeg_bytes)


@requires_ffmpeg
def test_thumbnails_are_cached_between_requests(stills_url):
    first = get(stills_url + "/thumb/1.jpg")[2]
    second = get(stills_url + "/thumb/1.jpg?t=123")[2]

    assert first == second  # byte identical, so it was not re-encoded


def test_a_camera_with_no_stills_has_no_thumbnail(stills_url):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(stills_url + "/thumb/2.jpg")

    assert raised.value.code == 404


def test_thumbnail_paths_cannot_escape_the_library(stills_url):
    for hostile in ("/thumb/..%2f..%2fetc%2fpasswd", "/thumb/...jpg", "/thumb/.jpg"):
        with pytest.raises(urllib.error.HTTPError) as raised:
            get(stills_url + hostile)
        assert raised.value.code == 404


def test_the_camera_wall_includes_cameras_that_have_not_rendered_yet(stills_url):
    _, _, body = get(stills_url + "/")

    marker = b'<script type="application/json" id="channels-payload">'
    start = body.index(marker) + len(marker)
    channels = json.loads(body[start:body.index(b"</script>", start)])

    # 1 and 2 have renders, 3 only has stills, and all three get a tile.
    assert channels == ["1", "2", "3"]
