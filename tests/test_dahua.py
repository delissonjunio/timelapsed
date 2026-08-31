"""The Dahua CGI driver, against a scripted device.

The fakes answer what a live MHDX 1308 actually answered (see
docs/Second-NVR-Intelbras.md), traps included: 1-based ask / 0-based answer on
channels, a clock that admits no offset anywhere, and downloads whose failures
arrive as HTTP 200 with a text body.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
import requests

from timelapsed.dahua import (
    DHAV_MAGIC,
    FIND_PAGE_SIZE,
    DahuaCaptureAgent,
    DahuaFootageClient,
    _parse_find_response,
    dahua_segment_name,
)
from timelapsed.schema import NVRConfig

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

NVR = NVRConfig(
    name="intelbras",
    kind="dahua",
    url="http://nvr.local",
    username="u",
    password="p",
    device_channels=("1", "2"),
)


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200, content_type: str = "text/plain"):
        self.text = text
        self.content = text.encode()
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=cast(Any, self))


class StreamResponse:
    def __init__(self, chunks, status_code=200):
        self.chunks = chunks
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=cast(Any, self))

    def iter_content(self, _size):
        return iter(self.chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeSession:
    """Routes by URL substring: the driver's whole conversation is GETs."""

    def __init__(self):
        self.auth = None
        self.calls = []
        self.routes = {}  # substring -> response or list of responses

    def script(self, substring, *responses):
        self.routes[substring] = list(responses)

    def get(self, url, timeout=None, stream=False):
        self.calls.append(url)
        for substring, responses in self.routes.items():
            if substring in url:
                result = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"Unscripted URL: {url}")


def find_page(rows: list[dict], found: int | None = None) -> FakeResponse:
    lines = [f"found={len(rows) if found is None else found}"]
    for index, row in enumerate(rows):
        for field, value in row.items():
            lines.append(f"items[{index}].{field}={value}")
    return FakeResponse("\n".join(lines))


def dav_row(start: str, end: str, path: str, length: int = 4988928) -> dict:
    # Channel comes back 0-based even though the ask was 1-based; the driver
    # must ignore it.
    return {
        "Channel": "0", "StartTime": start, "EndTime": end,
        "FilePath": path, "Length": str(length), "Type": "dav",
        "Events[0]": "VideoMotion", "VideoStream": "Main",
    }


def device_clock(offset: timedelta = timedelta(0)) -> FakeResponse:
    """What getCurrentTime answers for a device whose zone is `offset`.

    Relative to the real clock, because the driver measures the offset against
    it; the quarter-hour rounding absorbs the test's own few milliseconds.
    """
    local = (datetime.now(timezone.utc) + offset).replace(tzinfo=None)
    return FakeResponse(f"result={local:%Y-%m-%d %H:%M:%S}")


@pytest.fixture
def client():
    built = DahuaFootageClient("http://nvr.local/", "u", "p", nvr=NVR)
    session = FakeSession()
    # A device sitting on UTC unless a test scripts otherwise.
    session.script("getCurrentTime", device_clock())
    built.session = cast(Any, session)
    return built


# --- parsing ---

def test_find_response_parsing_handles_nested_fields():
    found, items = _parse_find_response(
        "found=2\n"
        "items[0].Channel=0\nitems[0].FilePath=/a.dav\nitems[0].Events[0]=VideoMotion\n"
        "items[1].Channel=0\nitems[1].FilePath=/b.dav\n"
    )
    assert found == 2
    assert [item["FilePath"] for item in items] == ["/a.dav", "/b.dav"]
    assert items[0]["Events[0]"] == "VideoMotion"


def test_segment_names_are_stable_and_safe():
    path = "/mnt/dvr/2026-08-28/0/dav/00/0/1/98673/00.14.57-00.15.31[M][0@0][0].dav"
    assert dahua_segment_name(path) == dahua_segment_name(path)
    assert "/" not in dahua_segment_name(path)


# --- the clock ---

def test_the_device_zone_is_measured_against_our_clock(client):
    """The device runs local time (-03 here) and admits no offset anywhere, so
    the offset is the difference against this host, rounded to a real zone."""
    client.session.script("getCurrentTime", device_clock(timedelta(hours=-3)))

    zone = client.device_zone()

    assert zone.utcoffset(None) == timedelta(hours=-3)


# --- search ---

def test_search_asks_one_based_and_translates_times(client):
    client.session.script("factory.create", FakeResponse("result=12345"))
    client.session.script("findFile&", FakeResponse("OK"))
    client.session.script(
        "findNextFile",
        find_page([dav_row("2026-08-28 08:14:57", "2026-08-28 08:15:31", "/mnt/dvr/x[0].dav")]),
    )
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    segments = list(client.search("intelbras-2", NOW - timedelta(hours=6), NOW))

    assert len(segments) == 1
    segment = segments[0]
    # The global channel id survives; the device saw its own number, 1-based.
    assert segment.channel == "intelbras-2"
    find_call = next(url for url in client.session.calls if "findFile" in url)
    assert "condition.Channel=2" in find_call
    assert "condition.Types%5B0%5D=dav" in find_call or "condition.Types[0]=dav" in find_call
    # Device-local (UTC here) in, UTC out.
    assert segment.started_at == datetime(2026, 8, 28, 8, 14, 57, tzinfo=timezone.utc)
    assert segment.playback_uri == "/mnt/dvr/x[0].dav"
    # The finder was closed and destroyed.
    assert any("action=close" in url for url in client.session.calls)
    assert any("action=destroy" in url for url in client.session.calls)


def test_search_pages_until_a_short_page(client):
    client.session.script("factory.create", FakeResponse("result=1"))
    client.session.script("findFile&", FakeResponse("OK"))
    full = [dav_row("2026-08-28 08:00:00", "2026-08-28 08:00:30", f"/m/{i}.dav") for i in range(FIND_PAGE_SIZE)]
    client.session.script(
        "findNextFile",
        find_page(full),
        find_page([dav_row("2026-08-28 09:00:00", "2026-08-28 09:00:30", "/m/last.dav")]),
    )
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    segments = list(client.search("intelbras-1", NOW - timedelta(hours=6), NOW))

    assert len(segments) == FIND_PAGE_SIZE + 1


def test_an_empty_window_answering_400_is_no_matches(client):
    client.session.script("factory.create", FakeResponse("result=1"))
    client.session.script("findFile&", FakeResponse("Error", status_code=400))
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    assert list(client.search("intelbras-1", NOW - timedelta(hours=1), NOW)) == []


def test_a_long_sweep_is_asked_as_bounded_windows(client):
    """Whether this firmware caps a finder's results is unverified, so no
    window is allowed to grow unbounded."""
    client.session.script("factory.create", FakeResponse("result=1"))
    client.session.script("findFile&", FakeResponse("OK"))
    client.session.script("findNextFile", find_page([]))
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    list(client.search("intelbras-1", NOW - timedelta(days=30), NOW))

    find_calls = [url for url in client.session.calls if "findFile&" in url]
    assert len(find_calls) >= 4  # 30 days at a 7-day window


def test_non_dav_rows_are_skipped(client):
    client.session.script("factory.create", FakeResponse("result=1"))
    client.session.script("findFile&", FakeResponse("OK"))
    client.session.script(
        "findNextFile",
        find_page([
            {"Channel": "0", "StartTime": "2026-08-28 08:00:00", "EndTime": "2026-08-28 08:00:10",
             "FilePath": "/m/still.jpg", "Length": "100", "Type": "jpg"},
        ]),
    )
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    assert list(client.search("intelbras-1", NOW - timedelta(hours=1), NOW)) == []


# --- the retention horizon probe ---

def test_oldest_recording_steps_over_the_recycled_region(client):
    """Empty windows over the recycled region cost one cheap finder each; the
    first window with footage answers. Minimum of the whole window, not its
    first result, because time order is unverified on this firmware."""
    client.session.script("factory.create", FakeResponse("result=1"))
    # Two recycled windows (an empty window answers 400), then footage,
    # deliberately out of time order.
    client.session.script(
        "findFile&",
        FakeResponse("Error", status_code=400),
        FakeResponse("Error", status_code=400),
        FakeResponse("OK"),
    )
    client.session.script(
        "findNextFile",
        find_page([
            dav_row("2026-08-23 10:00:00", "2026-08-23 10:00:30", "/m/later.dav"),
            dav_row("2026-08-23 08:00:00", "2026-08-23 08:00:30", "/m/oldest.dav"),
        ]),
    )
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    oldest = client.oldest_recording("intelbras-1", NOW - timedelta(days=20), NOW)

    assert oldest == datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc)


def test_oldest_recording_with_nothing_anywhere_is_none(client):
    client.session.script("factory.create", FakeResponse("result=1"))
    client.session.script("findFile&", FakeResponse("Error", status_code=400))
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    assert client.oldest_recording("intelbras-1", NOW - timedelta(days=20), NOW) is None


def test_a_probe_auth_failure_propagates_rather_than_reading_as_empty(client):
    """The empty-window 400 must not swallow a 401: the caller treats a probe
    failure as 'no filtering', never as 'device holds nothing'."""
    client.session.script("factory.create", FakeResponse("result=1"))
    client.session.script("findFile&", FakeResponse("Denied", status_code=401))
    client.session.script("action=close", FakeResponse("OK"))
    client.session.script("action=destroy", FakeResponse("OK"))

    with pytest.raises(requests.exceptions.HTTPError):
        client.oldest_recording("intelbras-1", NOW - timedelta(days=1), NOW)


# --- download ---

def test_download_url_encodes_the_path_and_checks_the_magic(client, tmp_path):
    client.session.script("RPC_Loadfile", StreamResponse([DHAV_MAGIC + b"rest", b"more"]))

    written = client.download("/mnt/dvr/a[M][0@0][0].dav", tmp_path / "out.ps", 60)

    assert written == len(DHAV_MAGIC + b"rest") + len(b"more")
    call = next(url for url in client.session.calls if "RPC_Loadfile" in url)
    assert "[" not in call and "@" not in call.split("http://", 1)[1]
    assert (tmp_path / "out.ps").read_bytes().startswith(DHAV_MAGIC)


def test_a_text_answer_dressed_as_200_is_refused(client, tmp_path):
    client.session.script("RPC_Loadfile", StreamResponse([b"Error\r\n"]))

    with pytest.raises(ValueError, match="DHAV"):
        client.download("/mnt/dvr/a.dav", tmp_path / "out.ps", 60)


def test_an_auth_failure_is_never_retried(client, tmp_path):
    """Dahua locks the account after a handful of bad attempts; a retry loop is
    exactly that."""
    client.session.script("RPC_Loadfile", StreamResponse([], status_code=401))

    with pytest.raises(requests.exceptions.HTTPError):
        client.download("/mnt/dvr/a.dav", tmp_path / "out.ps", 60)

    assert sum("RPC_Loadfile" in url for url in client.session.calls) == 1


# --- the capture agent ---

def test_capture_asks_the_device_channel_and_returns_jpeg():
    agent = DahuaCaptureAgent("http://nvr.local", "u", "p", nvr=NVR)
    session = FakeSession()
    session.script("snapshot.cgi", FakeResponse("\xff\xd8jpeg", content_type="image/jpeg"))
    agent._session = cast(Any, session)

    content, extension = agent.capture_image("intelbras-2")

    assert extension == "jpg"
    assert content
    assert session.calls == ["http://nvr.local/cgi-bin/snapshot.cgi?channel=2"]


def test_capture_refuses_a_non_image_answer():
    agent = DahuaCaptureAgent("http://nvr.local", "u", "p", nvr=NVR)
    session = FakeSession()
    session.script("snapshot.cgi", FakeResponse("Error", content_type="text/plain"))
    agent._session = cast(Any, session)

    with pytest.raises(ValueError, match="expected an image"):
        agent.capture_image("intelbras-1")


def test_the_agent_pickles_without_its_session():
    """Capture agents cross into worker processes; the session must not go with
    them, so each process authenticates its own."""
    import pickle

    agent = DahuaCaptureAgent("http://nvr.local", "u", "p", nvr=NVR)
    agent._get_session()

    revived = pickle.loads(pickle.dumps(agent))

    assert revived._session is None
    assert revived.nvr.device_channel("intelbras-2") == "2"
