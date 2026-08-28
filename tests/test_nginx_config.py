"""Holds the nginx site file and the viewer's own URLs together.

With nginx in front, /video/ never reaches Python, so the location regex in
deploy/nginx-timelapsed.conf is the only thing deciding which requests are
served and which walk out of the library. Nothing at runtime would notice it
drifting from the URLs TimelapseEntry builds — a mismatch just means every video
quietly falls back to the slow path — so it is checked here instead.
"""
import re
from datetime import timedelta
from pathlib import Path

import pytest

from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.web import TimelapseCatalogue
from tests.conftest import BASE_TIME

SITE_CONFIG = Path(__file__).resolve().parent.parent / "deploy" / "nginx-timelapsed.conf"

# location ~ "<regex>" {
LOCATION = re.compile(r'^\s*location\s+~\s+"([^"]+)"\s*\{', re.MULTILINE)

PLACEHOLDERS = ("__LIBRARY_ROOT__", "__ARCHIVE_ROOT__", "__LISTEN_PORT__", "__UPSTREAM_PORT__")


def _location_for(prefix: str) -> re.Pattern:
    """The regex nginx matches requests with this prefix against.

    nginx uses PCRE and Python uses its own engine, but these patterns stay
    inside the subset both agree on: character classes, anchors and captures.
    """
    matches = [m for m in LOCATION.findall(SITE_CONFIG.read_text()) if m.startswith(prefix)]
    assert len(matches) == 1, f"expected one {prefix} location, found {matches}"
    return re.compile(matches[0])


@pytest.fixture(scope="module")
def video_location() -> re.Pattern:
    return _location_for("^/video/")


@pytest.fixture(scope="module")
def archive_location() -> re.Pattern:
    return _location_for("^/archive/")


@pytest.fixture
def stocked_library(library: ImageCaptureLibrary, tmp_path: Path) -> ImageCaptureLibrary:
    """A couple of channels of renders, named exactly as the daemon names them."""
    source = tmp_path / "render.mp4"
    source.write_bytes(b"\0" * 64)

    for channel_id, cadence, days_ago in [
        ("1", "hourly", 0),
        ("1", "daily", 1),
        ("1", "weekly", 7),
        ("12", "daily", 2),
        # The keyframe-sourced cadences. Included because the location regex is
        # deliberately cadence-agnostic and nothing else proves it: a pattern
        # naming the three original cadences would 404 these in production while
        # every other test stayed green.
        ("1", "monthly", 31),
        ("1", "progress", 400),
    ]:
        starts = BASE_TIME - timedelta(days=days_ago)
        library.store_timelapse(channel_id, source, cadence, starts, starts + timedelta(hours=1))
    return library


def test_placeholders_are_all_present():
    """install.sh substitutes these; a renamed one would ship a literal to nginx."""
    text = SITE_CONFIG.read_text()
    for placeholder in PLACEHOLDERS:
        assert placeholder in text


def test_matches_the_urls_the_viewer_publishes(video_location, stocked_library):
    """Every URL in the real catalogue has to be one nginx serves itself."""
    catalogue = TimelapseCatalogue(stocked_library.root_path)
    entries = catalogue.entries()
    assert entries, "the fixture should have stocked some renders"

    for entry in entries:
        url = entry.as_dict()["url"]
        match = video_location.match(url)
        assert match, f"nginx would not serve {url}"
        assert match.group(1) == entry.channel_id
        assert match.group(2) == entry.path.name


def test_captures_rebuild_the_library_path(video_location, stocked_library):
    """The captures are pasted straight into `alias`, so they have to land on the file."""
    catalogue = TimelapseCatalogue(stocked_library.root_path)
    root = stocked_library.root_path

    for entry in catalogue.entries():
        channel, filename = video_location.match(entry.as_dict()["url"]).groups()
        assert root / channel / "timelapse" / filename == entry.path


@pytest.mark.parametrize(
    "url",
    [
        # nginx normalises the URI before matching, so a traversal arrives here
        # already collapsed — but a channel that is literally ".." must not slip
        # through either.
        "/video/../../etc/passwd",
        "/video/../etc/passwd.mp4",
        "/video/1/../../../etc/passwd.mp4",
        "/video/1/nested/render.mp4",
        # Only rendered videos take the fast path. Anything else is the
        # viewer's business.
        "/video/1/render.mp4.txt",
        "/video/1/render.jpg",
        "/video/1/render",
        # Hidden directories are not channels.
        "/video/.ssh/id_rsa.mp4",
        # The viewer's own routes must never be swallowed by the file location.
        "/api/timelapses",
        "/thumb/1.jpg",
        "/crop/1/frame.jpg",
        "/healthz",
        "/",
    ],
)
def test_refuses_anything_that_is_not_a_render(video_location, url):
    assert video_location.match(url) is None


def test_a_dot_cannot_start_a_channel(video_location):
    """`..` as a channel would let `alias` build a path above the library."""
    assert video_location.match("/video/../x.mp4") is None
    assert video_location.match("/video/./x.mp4") is None
    # A dot elsewhere in the name is harmless and stays allowed.
    assert video_location.match("/video/cam.1/x.mp4")


def test_a_realistic_render_filename_matches(video_location, library, tmp_path):
    """The stored name is long and full of underscores; the regex must not care."""
    source = tmp_path / "render.mp4"
    source.write_bytes(b"\0")
    stored = library.store_timelapse(
        "9", source, "weekly", BASE_TIME, BASE_TIME + timedelta(days=7)
    )

    match = video_location.match(f"/video/9/{stored.name}")
    assert match
    assert match.group(2) == stored.name
    assert stored.name.startswith("weekly_")


def test_archive_location_matches_the_urls_the_catalogue_builds(archive_location, tmp_path):
    """Every URL /api/archive hands out has to be one nginx serves itself."""
    from datetime import timedelta

    from timelapsed.archiver import segment_filename
    from timelapsed.web import ArchiveCatalogue

    root = tmp_path / "archive"
    day = root / "5" / BASE_TIME.strftime("%Y%m%d")
    day.mkdir(parents=True)
    stored = day / segment_filename(BASE_TIME, BASE_TIME + timedelta(minutes=2), "ch05_007")
    stored.write_bytes(b"\0")

    segments = ArchiveCatalogue(root).segments(
        "5", BASE_TIME - timedelta(hours=1), BASE_TIME + timedelta(hours=1)
    )
    assert segments, "the fixture should have stocked a segment"
    match = archive_location.match(segments[0]["url"])
    assert match, f"nginx would not serve {segments[0]['url']}"
    channel, day_name, filename = match.groups()
    assert root / channel / day_name / filename == stored


@pytest.mark.parametrize(
    "url",
    [
        "/archive/../../etc/passwd",
        "/archive/5/../20260828/x.mp4",
        "/archive/.ssh/20260828/x.mp4",
        "/archive/5/notaday/x.mp4",
        "/archive/5/2026082/x.mp4",
        "/archive/5/20260828/nested/x.mp4",
        "/archive/5/20260828/x.jpg",
        "/api/archive",
        "/video/1/render.mp4",
    ],
)
def test_archive_location_refuses_what_it_should(archive_location, url):
    assert archive_location.match(url) is None


def test_live_wall_and_nginx_agree_on_the_go2rtc_prefix():
    """The /live page loads its player and websocket under GO2RTC_PATH.

    Only nginx answers that prefix -- the Python viewer has no such route --
    so if the location and the page drift apart, every tile spins forever and
    nothing at runtime says why.
    """
    from timelapsed.live_page import GO2RTC_PATH, render_live

    assert GO2RTC_PATH in render_live(["1"]).decode()
    assert f"location {GO2RTC_PATH} " in SITE_CONFIG.read_text()


def test_go2rtc_location_proxies_websockets():
    """MSE and WebRTC signalling both arrive as a websocket upgrade."""
    site = SITE_CONFIG.read_text()
    location = site[site.index("location /go2rtc/"):]
    location = location[: location.index("}")]

    # The trailing slash on proxy_pass is what strips the /go2rtc/ prefix
    # before the request reaches go2rtc, which serves from its own root.
    assert "proxy_pass http://127.0.0.1:1984/;" in location
    assert "proxy_set_header Upgrade $http_upgrade;" in location
    assert 'proxy_set_header Connection "upgrade";' in location
