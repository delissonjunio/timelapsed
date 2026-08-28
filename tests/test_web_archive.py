"""The archive endpoints: the moment lookup and the file serving."""
import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from tests.conftest import BASE_TIME
from timelapsed.archiver import segment_filename
from timelapsed.web import build_server

SEGMENT_BODY = bytes(range(256)) * 16


@pytest.fixture
def stocked_archive(config, tmp_path):
    """Two archived segments a few minutes apart, named as the archiver names them."""
    config.archive_root = tmp_path / "archive"
    day = config.archive_root / "5" / BASE_TIME.strftime("%Y%m%d")
    day.mkdir(parents=True)
    for name, offset in (("ch05_001", 0), ("ch05_002", 10)):
        started = BASE_TIME + timedelta(minutes=offset)
        (day / segment_filename(started, started + timedelta(minutes=2), name)).write_bytes(SEGMENT_BODY)
    return config.archive_root


@pytest.fixture
def base_url(stocked_archive, config):
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


def epoch(moment):
    return int(moment.timestamp())


def test_the_lookup_answers_the_segments_covering_a_window(base_url):
    at = epoch(BASE_TIME + timedelta(minutes=1))
    status, _, body = get(f"{base_url}/api/archive?channel=5&start={at - 1}&end={at + 1}")

    segments = json.loads(body)
    assert status == 200
    assert len(segments) == 1
    assert segments[0]["url"].startswith("/archive/5/")
    assert segments[0]["size_bytes"] == len(SEGMENT_BODY)


def test_the_lookup_is_bounded_by_overlap_not_containment(base_url):
    start = epoch(BASE_TIME - timedelta(hours=1))
    end = epoch(BASE_TIME + timedelta(hours=1))
    _, _, body = get(f"{base_url}/api/archive?channel=5&start={start}&end={end}")

    assert [s["url"].rsplit("_", 2)[-2:] for s in json.loads(body)] == [
        ["ch05", "001.mp4"], ["ch05", "002.mp4"],
    ]


def test_the_lookup_requires_a_window(base_url):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(f"{base_url}/api/archive?channel=5")
    assert raised.value.code == 400


def test_the_archived_file_is_served_with_ranges(base_url):
    at = epoch(BASE_TIME)
    _, _, body = get(f"{base_url}/api/archive?channel=5&start={at}&end={at + 1}")
    url = json.loads(body)[0]["url"]

    status, headers, whole = get(base_url + url)
    assert status == 200
    assert whole == SEGMENT_BODY
    assert headers["Accept-Ranges"] == "bytes"

    status, headers, part = get(base_url + url, {"Range": "bytes=16-31"})
    assert status == 206
    assert part == SEGMENT_BODY[16:32]
    assert headers["Content-Range"] == f"bytes 16-31/{len(SEGMENT_BODY)}"


@pytest.mark.parametrize(
    "path",
    [
        "/archive/5/20250601/../../../etc/passwd.mp4",
        "/archive/../5/20250601/x.mp4",
        "/archive/5/notaday/x.mp4",
        "/archive/5/20250601/missing.mp4",
        "/archive/5/20250601/x.txt",
        "/archive/5",
    ],
)
def test_the_file_route_refuses_what_it_should(base_url, path):
    with pytest.raises(urllib.error.HTTPError) as raised:
        get(base_url + path)
    assert raised.value.code == 404


def test_everything_archive_is_404_when_the_replica_is_off(config, library):
    config.archive_root = None
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        for path in ("/api/archive?channel=5&start=0&end=1", "/archive/5/20250601/x.mp4"):
            with pytest.raises(urllib.error.HTTPError) as raised:
                get(base + path)
            assert raised.value.code == 404, path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_page_says_whether_the_replica_exists(base_url):
    _, _, body = get(base_url + "/")
    page = body.decode()
    assert '<script type="application/json" id="archive-payload">true</script>' in page
