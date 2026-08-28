"""What footage the NVR itself holds, mirrored into the index.

Recognition answers *was this moment worth keeping*; this answers *is there
footage for it*. The NVR records on events, not continuously, so the only way
to know whether a moment is on the device is to ask it -- and the answer lives
in `ContentMgmt/search`, paged 64 results at a time.

The mirror is a rebuildable cache. The device is authoritative for what it
holds, so a sweep that is ever suspected of drift is simply dropped and run
again from the start (see `SegmentIndexer.rebuild`). Nothing here needs backing
up, and nothing here downloads any video: this is the map, not the footage.
"""
import logging
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import parse_qs, urlparse

import requests
from requests.auth import HTTPDigestAuth

from timelapsed.analysis.index import AnalysisIndex, from_epoch, to_epoch

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = (5.0, 30.0)  # (connect, read)
# The device's hard cap. Asking for more does not error; it answers 64 anyway.
SEARCH_PAGE_SIZE = 64
# How far back an incremental sweep re-asks. A segment still being written when
# the last sweep saw it keeps growing until its event closes, so the recent past
# is re-queried and the upsert extends the row. Segments here chain to a couple
# of minutes; an hour of overlap is one page and closes the question.
SWEEP_OVERLAP = timedelta(hours=1)
# Where a channel's first sweep starts. Predates anything the device could
# still hold; searching a range with nothing in it costs one empty page.
FULL_SWEEP_START = datetime(2020, 1, 1, tzinfo=timezone.utc)
# A device that answers MORE forever would otherwise spin this loop forever.
# The busiest channel's entire history is under a hundred pages.
MAX_PAGES_PER_SEARCH = 2000

# `searchResultPostion` is the device's own spelling. The searchID must be a
# real UUID: anything else is HTTP 400, statusCode 6.
SEARCH_BODY = """<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription>
    <searchID>{search_id}</searchID>
    <trackIDList><trackID>{track_id}</trackID></trackIDList>
    <timeSpanList><timeSpan>
        <startTime>{start}</startTime>
        <endTime>{end}</endTime>
    </timeSpan></timeSpanList>
    <maxResults>{page_size}</maxResults>
    <searchResultPostion>{position}</searchResultPostion>
    <metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>
</CMSearchDescription>"""


@dataclass(frozen=True)
class RecordedSegment:
    channel: str
    started_at: datetime
    ended_at: datetime
    size_bytes: int
    playback_uri: str


def _search_time(moment: datetime, device_zone: timezone) -> str:
    # The device's wall clock, stamped 'Z' because that is what the device
    # itself writes. See NVRFootageClient.device_zone for the measurement.
    return moment.astimezone(device_zone).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: str, device_zone: timezone) -> datetime:
    # A 'Z' from this firmware is a lie -- the stamp is device-local wall time
    # -- so Z and naive both mean the device's own zone. A stamp carrying a
    # real numeric offset is taken at its word.
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        moment = datetime.fromisoformat(raw[:-1]).replace(tzinfo=device_zone)
    else:
        moment = datetime.fromisoformat(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=device_zone)
    return moment.astimezone(timezone.utc)


def _first_text(element: ElementTree.Element, tag: str) -> str | None:
    # The namespace differs across firmwares (hikvision.com vs std-cgi.com),
    # so every lookup is namespace-blind.
    found = element.find(f".//{{*}}{tag}")
    return found.text if found is not None and found.text else None


def _uri_size(playback_uri: str) -> int:
    values = parse_qs(urlparse(playback_uri).query).get("size")
    try:
        return int(values[0]) if values else 0
    except ValueError:
        return 0


class NVRFootageClient:
    """Asks an ISAPI (Hikvision-compatible) NVR what recorded segments it holds.

    Search only -- downloading is a later stage. One `Session` for the life of
    the client, so a multi-page sweep authenticates once instead of paying the
    digest challenge's extra round trip on every page.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(username, password)
        self._device_zone: timezone | None = None

    def device_zone(self) -> timezone:
        """The device's UTC offset, read from its own clock and cached.

        The search API speaks the device's local wall clock while stamping it
        'Z' -- the window it is asked for AND the results it returns. Measured
        on the real device (2026-08-28): the same hour asked for as true UTC
        answered NO MATCHES; asked for as local-with-Z it matched, and results
        came back stamped local-with-Z. So every conversation translates
        through this offset, which /ISAPI/System/time reports honestly
        (localTime carries a real numeric offset).
        """
        if self._device_zone is None:
            response = self.session.get(f"{self.url}/ISAPI/System/time", timeout=self.timeout)
            response.raise_for_status()
            local_time = _first_text(ElementTree.fromstring(response.content), "localTime")
            if not local_time:
                raise ValueError("The NVR's /ISAPI/System/time answer carries no localTime")
            offset = datetime.fromisoformat(local_time.strip()).utcoffset()
            if offset is None:
                raise ValueError(f"The NVR's localTime {local_time!r} carries no UTC offset")
            self._device_zone = timezone(offset)
            logger.info("NVR clock offset is %s", self._device_zone)
        return self._device_zone

    def search(self, channel: str, start: datetime, end: datetime) -> Iterator[RecordedSegment]:
        """Every recorded segment for this channel overlapping [start, end]."""
        device_zone = self.device_zone()
        search_id = str(uuid.uuid4())
        position = 0
        for _ in range(MAX_PAGES_PER_SEARCH):
            body = SEARCH_BODY.format(
                search_id=search_id,
                track_id=f"{channel}01",
                start=_search_time(start, device_zone),
                end=_search_time(end, device_zone),
                page_size=SEARCH_PAGE_SIZE,
                position=position,
            )
            response = self.session.post(
                f"{self.url}/ISAPI/ContentMgmt/search",
                data=body,
                headers={"Content-Type": "application/xml"},
                timeout=self.timeout,
            )
            response.raise_for_status()

            root = ElementTree.fromstring(response.content)
            matches = root.findall(".//{*}searchMatchItem")
            for item in matches:
                segment = self._parse_match(channel, item)
                if segment is not None:
                    yield segment

            position += len(matches)
            status = (_first_text(root, "responseStatusStrg") or "").strip().upper()
            if status != "MORE" or not matches:
                return
        logger.warning(
            "Channel %s search still answering MORE after %d pages; stopping this sweep",
            channel, MAX_PAGES_PER_SEARCH,
        )

    def _parse_match(self, channel: str, item: ElementTree.Element) -> RecordedSegment | None:
        started = _first_text(item, "startTime")
        ended = _first_text(item, "endTime")
        playback_uri = _first_text(item, "playbackURI")
        if not (started and ended and playback_uri):
            logger.warning("Channel %s returned a search match missing its time span or URI", channel)
            return None
        try:
            device_zone = self.device_zone()
            started_at, ended_at = _parse_time(started, device_zone), _parse_time(ended, device_zone)
        except ValueError:
            logger.warning("Channel %s returned unreadable segment times %r..%r", channel, started, ended)
            return None
        return RecordedSegment(
            channel=channel,
            started_at=started_at,
            ended_at=ended_at,
            size_bytes=_uri_size(playback_uri),
            playback_uri=playback_uri,
        )


class SegmentIndexer:
    """Keeps `nvr_segment` mirroring what the device holds.

    Runs inside the analyzer daemon, which is the index's one writer. A sweep is
    all-or-nothing per channel: the segment list is materialised before anything
    is written, so a search that dies mid-page moves no watermark and the next
    poll simply asks again.
    """

    def __init__(self, client: NVRFootageClient, index: AnalysisIndex, channels: list[str]):
        self.client = client
        self.index = index
        self.channels = channels

    def sync(self, channel: str, now: datetime) -> int:
        """One incremental sweep of one channel. Returns how many segments it saw."""
        swept_through = self.index.segment_sweep(channel)
        if swept_through is None:
            start = FULL_SWEEP_START
        else:
            start = from_epoch(swept_through) - SWEEP_OVERLAP
        segments = list(self.client.search(channel, start, now))
        self.index.record_segments(
            channel,
            [
                (
                    to_epoch(segment.started_at),
                    to_epoch(segment.ended_at),
                    segment.size_bytes,
                    segment.playback_uri,
                )
                for segment in segments
            ],
            swept_through=to_epoch(now),
        )
        if swept_through is None:
            logger.info("Channel %s footage map built: %d segment(s)", channel, len(segments))
        else:
            logger.debug("Channel %s footage sweep saw %d segment(s)", channel, len(segments))
        return len(segments)

    def sync_all(self, now: datetime) -> None:
        """Sweep every channel, letting one channel's failure cost only itself."""
        for channel in self.channels:
            try:
                self.sync(channel, now)
            except Exception:
                logger.exception("NVR footage sweep failed for channel %s, continuing", channel)

    def rebuild(self, channel: str, now: datetime) -> int:
        """Drop the channel's mirror and sweep the device's whole history again."""
        self.index.clear_segments(channel)
        return self.sync(channel, now)
