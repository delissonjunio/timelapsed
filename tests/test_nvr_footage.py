import uuid
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone

import pytest
import requests

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.nvr_footage import (
    FULL_SWEEP_START,
    MPEG_PS_MAGIC,
    SEARCH_PAGE_SIZE,
    SWEEP_OVERLAP,
    NVRFootageClient,
    SegmentIndexer,
)

NOW = BASE_TIME
BASE = to_epoch(BASE_TIME)


def playback_uri(start: str, end: str, size: int) -> str:
    return (
        "rtsp://nvr.local/Streaming/tracks/501"
        f"?starttime={start}&endtime={end}&name=ch05_00000000033000801&size={size}"
    )


def match_item(start: str, end: str, size: int) -> str:
    uri = playback_uri(start, end, size).replace("&", "&amp;")
    return f"""
    <searchMatchItem>
        <trackID>501</trackID>
        <timeSpan><startTime>{start}</startTime><endTime>{end}</endTime></timeSpan>
        <mediaSegmentDescriptor>
            <contentType>video</contentType>
            <codecType>H.265-BP</codecType>
            <playbackURI>{uri}</playbackURI>
        </mediaSegmentDescriptor>
    </searchMatchItem>"""


def search_page(status: str, items: list[str]) -> str:
    """One CMSearchResult page, in the namespace the real device answers with."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
        <searchID>ignored</searchID>
        <responseStatus>true</responseStatus>
        <responseStatusStrg>{status}</responseStatusStrg>
        <numOfMatches>{len(items)}</numOfMatches>
        <matchList>{''.join(items)}</matchList>
    </CMSearchResult>"""


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.content = text.encode()
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


def time_answer(local_time: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>'
        '<Time version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        f"<timeMode>NTP</timeMode><localTime>{local_time}</localTime>"
        "<timeZone>CST+3:00:00</timeZone></Time>"
    )


class StreamResponse:
    """What session.post(stream=True) hands back: a context manager of chunks."""

    def __init__(self, chunks, status_code=200):
        self.chunks = chunks
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def iter_content(self, _size):
        return iter(self.chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeSession:
    def __init__(self):
        self.auth = None
        self.responses = []
        self.calls = []
        self.time_calls = 0
        # A device sitting on UTC, so most tests read stamps at face value.
        # The zone-translation tests script an offset instead.
        self.time_text = time_answer("2026-08-28T12:00:00+00:00")

    def script(self, *responses):
        self.responses = list(responses)

    def get(self, url, timeout=None):
        self.time_calls += 1
        return FakeResponse(self.time_text)

    def post(self, url, data=None, headers=None, timeout=None, stream=False):
        self.calls.append({
            "url": url, "data": data, "headers": headers, "timeout": timeout, "stream": stream,
        })
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def client():
    built = NVRFootageClient("http://nvr.local/", "admin", "pw")
    built.session = FakeSession()
    return built


def request_field(call, tag):
    return ElementTree.fromstring(call["data"]).find(f".//{tag}").text


# --- the search request ---

def test_the_search_id_is_a_real_uuid(client):
    """Anything else is HTTP 400, statusCode 6, on the real device."""
    client.session.script(FakeResponse(search_page("OK", [])))

    list(client.search("5", NOW - timedelta(hours=1), NOW))

    uuid.UUID(request_field(client.session.calls[0], "searchID"))


def test_the_search_asks_for_the_main_stream_track(client):
    client.session.script(FakeResponse(search_page("OK", [])))

    list(client.search("5", NOW - timedelta(hours=1), NOW))

    assert request_field(client.session.calls[0], "trackID") == "501"


def test_the_search_window_is_sent_in_the_device_wall_clock(client):
    """The device reads the request's 'Z' times as its own local wall clock --
    asking in true UTC returned NO MATCHES on the real hardware -- so the
    window is translated to the offset the device itself reports."""
    client.session.time_text = time_answer("2026-08-28T13:05:59-03:00")
    client.session.script(FakeResponse(search_page("OK", [])))
    start = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)

    list(client.search("5", start, NOW))

    assert request_field(client.session.calls[0], "startTime") == "2026-08-28T12:00:00Z"


def test_the_device_clock_is_read_once_not_per_page(client):
    client.session.script(
        FakeResponse(search_page("OK", [])),
        FakeResponse(search_page("OK", [])),
    )

    list(client.search("5", NOW - timedelta(hours=1), NOW))
    list(client.search("5", NOW - timedelta(hours=1), NOW))

    assert client.session.time_calls == 1


def test_the_search_respects_the_device_page_cap(client):
    client.session.script(FakeResponse(search_page("OK", [])))

    list(client.search("5", NOW - timedelta(hours=1), NOW))

    call = client.session.calls[0]
    assert request_field(call, "maxResults") == str(SEARCH_PAGE_SIZE)
    assert call["url"] == "http://nvr.local/ISAPI/ContentMgmt/search"
    assert call["timeout"] is not None


# --- paging ---

def test_more_pages_are_followed_until_the_device_says_ok(client):
    client.session.script(
        FakeResponse(search_page("MORE", [
            match_item("2026-08-27T12:00:00Z", "2026-08-27T12:01:30Z", 100),
            match_item("2026-08-27T12:05:00Z", "2026-08-27T12:06:00Z", 200),
        ])),
        FakeResponse(search_page("OK", [
            match_item("2026-08-27T12:10:00Z", "2026-08-27T12:11:00Z", 300),
        ])),
    )

    segments = list(client.search("5", NOW - timedelta(hours=1), NOW))

    assert len(segments) == 3
    assert len(client.session.calls) == 2
    # The follow-up continues the same search, further along.
    first, second = client.session.calls
    assert request_field(second, "searchID") == request_field(first, "searchID")
    assert request_field(second, "searchResultPostion") == "2"


def test_a_capped_session_is_resumed_where_it_stopped(client, monkeypatch):
    """The device truncates a search at its result cap and stamps the last page
    OK as if complete, so a session that fills the cap is not believed: a fresh
    search picks up at the last segment it returned."""
    monkeypatch.setattr("timelapsed.nvr_footage.SESSION_RESULT_CAP", 3)
    client.session.script(
        FakeResponse(search_page("MORE", [
            match_item("2026-08-27T12:00:00Z", "2026-08-27T12:01:00Z", 1),
            match_item("2026-08-27T12:02:00Z", "2026-08-27T12:03:00Z", 2),
        ])),
        FakeResponse(search_page("OK", [
            match_item("2026-08-27T12:04:00Z", "2026-08-27T12:05:30Z", 3),
        ])),
        FakeResponse(search_page("OK", [
            match_item("2026-08-27T12:06:00Z", "2026-08-27T12:07:00Z", 4),
        ])),
    )

    segments = list(client.search("5", NOW - timedelta(days=1), NOW))

    assert [segment.size_bytes for segment in segments] == [1, 2, 3, 4]
    resumed = client.session.calls[2]
    assert request_field(resumed, "searchID") != request_field(client.session.calls[0], "searchID")
    assert request_field(resumed, "searchResultPostion") == "0"
    assert request_field(resumed, "startTime") == "2026-08-27T12:05:30Z"


def test_a_capped_session_that_cannot_advance_stops_rather_than_spinning(client, monkeypatch):
    monkeypatch.setattr("timelapsed.nvr_footage.SESSION_RESULT_CAP", 1)
    same_item = match_item("2026-08-27T12:00:00Z", "2026-08-27T12:01:00Z", 1)
    client.session.script(
        FakeResponse(search_page("OK", [same_item])),
        FakeResponse(search_page("OK", [same_item])),
    )

    segments = list(client.search("5", datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc), NOW))

    # The second session re-answers the seam segment and advances nowhere, so
    # the search stops instead of asking a third time.
    assert len(segments) == 2
    assert len(client.session.calls) == 2


def test_an_empty_page_ends_the_search_even_if_the_device_says_more(client):
    client.session.script(FakeResponse(search_page("MORE", [])))

    assert list(client.search("5", NOW - timedelta(hours=1), NOW)) == []
    assert len(client.session.calls) == 1


# --- parsing ---

def test_segments_carry_their_span_size_and_exact_uri(client):
    client.session.script(FakeResponse(search_page("OK", [
        match_item("2026-08-27T12:00:00Z", "2026-08-27T12:01:30Z", 14776788),
    ])))

    segment = next(client.search("5", NOW - timedelta(hours=1), NOW))

    assert segment.channel == "5"
    assert segment.started_at == datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    assert segment.ended_at == datetime(2026, 8, 27, 12, 1, 30, tzinfo=timezone.utc)
    # The size comes from the URI's own size= field, and the URI survives
    # verbatim: download rejects anything without its name= and size=.
    assert segment.size_bytes == 14776788
    assert "name=ch05_00000000033000801" in segment.playback_uri
    assert "&amp;" not in segment.playback_uri


def test_z_stamped_results_are_read_as_the_device_wall_clock(client):
    """The firmware stamps results 'Z' but means its own local time; a stamp of
    12:00Z from a -03:00 device is 15:00 UTC."""
    client.session.time_text = time_answer("2026-08-28T13:05:59-03:00")
    client.session.script(FakeResponse(search_page("OK", [
        match_item("2026-08-28T12:00:00Z", "2026-08-28T12:01:00Z", 1),
    ])))

    segment = next(client.search("5", NOW - timedelta(hours=1), NOW))

    assert segment.started_at == datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)


def test_a_stamp_with_a_real_offset_is_taken_at_its_word(client):
    client.session.script(FakeResponse(search_page("OK", [
        match_item("2026-08-27T09:00:00-03:00", "2026-08-27T09:01:00-03:00", 1),
    ])))

    segment = next(client.search("5", NOW - timedelta(hours=1), NOW))

    assert segment.started_at == datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def test_a_match_missing_its_uri_is_skipped_not_fatal(client, caplog):
    broken = """
    <searchMatchItem>
        <timeSpan><startTime>2026-08-27T12:00:00Z</startTime><endTime>2026-08-27T12:01:00Z</endTime></timeSpan>
    </searchMatchItem>"""
    client.session.script(FakeResponse(search_page("OK", [
        broken,
        match_item("2026-08-27T12:05:00Z", "2026-08-27T12:06:00Z", 5),
    ])))

    segments = list(client.search("5", NOW - timedelta(hours=1), NOW))

    assert len(segments) == 1
    assert segments[0].size_bytes == 5


def test_an_http_error_raises_rather_than_returning_half_a_sweep(client):
    client.session.script(FakeResponse("busy", status_code=500))

    with pytest.raises(requests.exceptions.HTTPError):
        list(client.search("5", NOW - timedelta(hours=1), NOW))


# --- downloading ---

def test_download_streams_the_segment_to_disk(client, tmp_path):
    client.session.script(StreamResponse([MPEG_PS_MAGIC + b"video", b"more video"]))
    destination = tmp_path / "segment.ps"
    uri = "rtsp://nvr/Streaming/tracks/501?starttime=x&endtime=y&name=ch05_1&size=15"

    written = client.download(uri, destination, deadline_seconds=60)

    assert destination.read_bytes() == MPEG_PS_MAGIC + b"videomore video"
    assert written == len(MPEG_PS_MAGIC + b"videomore video")
    call = client.session.calls[0]
    assert call["url"] == "http://nvr.local/ISAPI/ContentMgmt/download"
    assert call["stream"] is True
    # The URI goes into an XML body, so its ampersands must arrive escaped.
    assert "starttime=x&amp;endtime=y" in call["data"]


def test_download_accepts_the_hikvision_wrapped_stream(client, tmp_path):
    """The live device opens its PS with a proprietary IMKH pseudo-header, not
    the bare pack header; ffmpeg skips it, so the archiver keeps it."""
    client.session.script(StreamResponse([b"IMKH" + b"\x00" * 28 + MPEG_PS_MAGIC + b"video"]))

    written = client.download("rtsp://nvr/x?name=a&size=1", tmp_path / "segment.ps", 60)

    assert written > 0


def test_download_retries_the_transient_400s_the_device_throws(client, tmp_path):
    """Under back-to-back downloads the device 400s intermittently; the same
    URI succeeds on replay moments later, so a refusal is retried."""
    client.session.script(
        StreamResponse([], status_code=400),
        StreamResponse([MPEG_PS_MAGIC + b"video"]),
    )

    written = client.download("rtsp://nvr/x?name=a&size=1", tmp_path / "segment.ps", 60)

    assert written > 0
    assert len(client.session.calls) == 2


def test_download_rejects_an_xml_answer_dressed_as_success(client, tmp_path):
    """The device reports some failures as HTTP 200 with an XML body. The first
    bytes are the only honest statement of what arrived."""
    client.session.script(StreamResponse([b'<?xml version="1.0"?><ResponseStatus/>']))

    with pytest.raises(ValueError, match="MPEG-PS"):
        client.download("rtsp://nvr/x?name=a&size=1", tmp_path / "segment.ps", 60)


def test_download_enforces_its_wall_clock_deadline(client, tmp_path):
    client.session.script(StreamResponse([MPEG_PS_MAGIC + b"drip", b"drip", b"drip"]))

    with pytest.raises(TimeoutError):
        client.download("rtsp://nvr/x?name=a&size=1", tmp_path / "segment.ps", -1)


# --- the indexer ---

class ScriptedClient:
    """Duck-typed NVRFootageClient: records the windows asked for and answers
    from a script, one entry per call."""

    def __init__(self):
        self.results = []
        self.calls = []

    def search(self, channel, start, end):
        self.calls.append((channel, start, end))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return iter(result)


def segment(channel, started_at, ended_at, size=1000):
    from timelapsed.nvr_footage import RecordedSegment

    return RecordedSegment(
        channel=channel,
        started_at=started_at,
        ended_at=ended_at,
        size_bytes=size,
        playback_uri=f"rtsp://nvr.local/tracks/{channel}01?name=x&size={size}",
    )


@pytest.fixture
def index(tmp_path):
    with AnalysisIndex(tmp_path / "index.sqlite3") as opened:
        yield opened


@pytest.fixture
def indexer(index):
    # One scripted client behind both channels, exposed as .client so the tests
    # can script it without caring that the indexer routes per channel now.
    client = ScriptedClient()
    built = SegmentIndexer({"5": client, "6": client}, index, ["5", "6"])
    built.client = client
    return built


def test_the_first_sweep_covers_the_device_whole_history(indexer, index):
    indexer.client.results = [[segment("5", NOW - timedelta(minutes=10), NOW - timedelta(minutes=8))]]

    count = indexer.sync("5", NOW)

    assert count == 1
    (channel, start, _end) = indexer.client.calls[0]
    assert (channel, start) == ("5", FULL_SWEEP_START)
    assert len(index.segments(channel="5")) == 1
    assert index.segment_sweep("5") == to_epoch(NOW)


def test_later_sweeps_only_reask_the_recent_past(indexer, index):
    indexer.client.results = [[], []]
    indexer.sync("5", NOW - timedelta(hours=2))

    indexer.sync("5", NOW)

    (_, start, end) = indexer.client.calls[1]
    assert start == NOW - timedelta(hours=2) - SWEEP_OVERLAP
    assert end == NOW


def test_a_growing_segment_is_extended_in_place(indexer, index):
    """A segment still being written keeps its start while its end walks
    forward; re-sweeping must not duplicate it."""
    started = NOW - timedelta(minutes=5)
    indexer.client.results = [
        [segment("5", started, NOW - timedelta(minutes=3), size=100)],
        [segment("5", started, NOW - timedelta(minutes=1), size=250)],
    ]

    indexer.sync("5", NOW - timedelta(minutes=2))
    indexer.sync("5", NOW)

    rows = index.segments(channel="5")
    assert len(rows) == 1
    assert rows[0]["size_bytes"] == 250


def test_a_failed_sweep_moves_no_watermark(indexer, index):
    indexer.client.results = [requests.exceptions.ConnectTimeout("nvr away")]

    with pytest.raises(requests.exceptions.ConnectTimeout):
        indexer.sync("5", NOW)

    assert index.segment_sweep("5") is None


def test_sync_all_lets_one_channel_fail_alone(indexer, index):
    indexer.client.results = [
        requests.exceptions.ConnectTimeout("nvr away"),
        [segment("6", NOW - timedelta(minutes=10), NOW - timedelta(minutes=9))],
    ]

    indexer.sync_all(NOW)

    assert index.segment_sweep("5") is None
    assert len(index.segments(channel="6")) == 1


def test_rebuild_drops_the_mirror_and_sweeps_from_scratch(indexer, index):
    indexer.client.results = [
        [segment("5", NOW - timedelta(hours=3), NOW - timedelta(hours=2))],
        [segment("5", NOW - timedelta(minutes=10), NOW - timedelta(minutes=9))],
    ]
    indexer.sync("5", NOW)

    indexer.rebuild("5", NOW)

    rows = index.segments(channel="5")
    assert len(rows) == 1  # the stale row from the first sweep is gone
    (_, start, _) = indexer.client.calls[1]
    assert start == FULL_SWEEP_START


def test_segment_runs_include_straddlers_and_carry_their_totals(index):
    index.record_segments(
        "5",
        [
            (BASE - 30, BASE + 10, 100, "rtsp://nvr/a"),   # straddles the window start
            (BASE + 12, BASE + 20, 200, "rtsp://nvr/b"),   # gap 2 from the straddler
            (BASE + 500, BASE + 510, 400, "rtsp://nvr/c"),  # far away
        ],
        swept_through=BASE + 1000,
    )

    runs = index.segment_runs("5", BASE, BASE + 1000, max_gap=2)

    assert [run["segments"] for run in runs] == [2, 1]
    assert runs[0]["size_bytes"] == 300
    assert runs[0]["starts"] == to_iso(BASE - 30)
    assert runs[1]["finishes"] == to_iso(BASE + 510)


def to_iso(epoch):
    from timelapsed.analysis.index import from_epoch

    return from_epoch(epoch).isoformat()


def test_segments_are_queried_by_overlap_not_containment(indexer, index):
    indexer.client.results = [[
        segment("5", NOW - timedelta(minutes=30), NOW - timedelta(minutes=20)),
        segment("5", NOW - timedelta(minutes=10), NOW - timedelta(minutes=5)),
    ]]
    indexer.sync("5", NOW)

    # A window opening mid-segment still sees the segment that straddles it.
    rows = index.segments(
        channel="5",
        start=to_epoch(NOW - timedelta(minutes=25)),
        end=to_epoch(NOW),
    )

    assert len(rows) == 2
