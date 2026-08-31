"""The Dahua CGI driver: stills, segment search and segment download.

Dahua-OEM recorders (the Intelbras MHDX line among them) speak their own CGI
API rather than ISAPI, so this module is the second implementation of the same
three operations `nvr_capture_agent` and `nvr_footage` provide for Hikvision:
one frame now, what segments exist, and one segment's bytes. Which driver a
given NVR gets is decided by `type =` in its config section.

Everything here was mapped against a live MHDX 1308 (docs/Second-NVR-Intelbras.md)
and carries that device's traps:

* `mediaFileFind` takes `condition.Channel` 1-based but reports `Channel`
  0-based in its results. The result's channel is therefore ignored -- the
  caller knows what it asked for.
* The clock runs device-local and is presented with no offset anywhere, so the
  offset is measured against this host's own clock and every stamp translated
  at the edge; UTC is what leaves this module.
* `Length` in a search result is cluster-aligned allocation, not file size.
  Good enough for progress display, never used for verification.
* Downloads are DHAV, Dahua's own container. ffmpeg demuxes it natively, so the
  archiver's remux pipeline works unchanged; the first-chunk magic check is the
  honest test of whether a download is really video.
* A handful of failed logins locks the account for minutes, so nothing here
  retries an auth failure: 401/403 give up immediately.
"""
import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from urllib.parse import quote

import backoff
import requests
from requests.auth import HTTPDigestAuth

from timelapsed.nvr_capture_agent import CONTENT_TYPE_TO_EXTENSION, MAX_CAPTURE_TRIES
from timelapsed.schema import NVRConfig, VideoResolution

if TYPE_CHECKING:
    # Runtime imports of nvr_footage stay inside the functions that need them:
    # nvr_footage imports this module back, and only the lazy import breaks the
    # cycle.
    from timelapsed.nvr_footage import RecordedSegment

logger = logging.getLogger(__name__)

CAPTURE_TIMEOUT_SECONDS = (5.0, 20.0)  # (connect, read)
FOOTAGE_TIMEOUT_SECONDS = (5.0, 30.0)
# One findNextFile page. The device answers fewer when fewer remain.
FIND_PAGE_SIZE = 64
# A finder that answers full pages forever must not spin this loop forever.
MAX_PAGES_PER_FINDER = 2000
# Whether this firmware caps a finder's total results (the way the Hikvision
# silently truncates at 4,000) is unverified, so no window is ever allowed to
# grow unbounded: a long sweep is asked as a series of bounded windows instead.
SEARCH_WINDOW = timedelta(days=7)
# The device's clock offset is measured against this host's clock, so it is
# rounded to the nearest quarter hour -- every real UTC offset is a multiple,
# and the rounding absorbs request latency plus ordinary clock drift.
ZONE_GRANULARITY = timedelta(minutes=15)
DEVICE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

DHAV_MAGIC = b"DHAV"
DOWNLOAD_CHUNK_BYTES = 256 * 1024


def _fatal_auth(error: Exception) -> bool:
    """Never retry an auth failure: Dahua firmware locks the account after a
    handful of bad Digest attempts, and a retry loop is exactly that."""
    return (
        isinstance(error, requests.exceptions.HTTPError)
        and error.response is not None
        and error.response.status_code in (401, 403)
    )


def dahua_segment_name(playback_uri: str) -> str:
    """A filesystem-safe, globally unique name for one recorded segment.

    Hikvision URIs carry a `name=` the archiver files segments under; a Dahua
    "URI" is the raw on-disk FilePath, full of brackets and slashes. The path is
    unique per segment and stable across sweeps, so a digest of it is too.
    """
    return "dav-" + hashlib.sha1(playback_uri.encode()).hexdigest()[:16]


def _parse_find_response(text: str) -> tuple[int, list[dict[str, str]]]:
    """findNextFile's key=value lines as (found count, one dict per item)."""
    found = 0
    items: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("found="):
            try:
                found = int(line.partition("=")[2])
            except ValueError:
                pass
        elif line.startswith("items["):
            key, _, value = line.partition("=")
            index_text, _, field = key.partition("].")
            try:
                index = int(index_text[len("items["):])
            except ValueError:
                continue
            items.setdefault(index, {})[field] = value
    return found, [items[index] for index in sorted(items)]


class DahuaCaptureAgent:
    """Pulls single-frame snapshots from a Dahua CGI recorder.

    Same shape as `NVRCaptureAgent`: one HTTP GET per frame, no stream, no
    decoding. The resolution argument is accepted for interface parity and
    ignored -- `snapshot.cgi` serves the device's own snapshot encode
    (`SnapFormat`, a device setting), and there is no per-request override.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        nvr: NVRConfig | None = None,
        timeout: tuple[float, float] = CAPTURE_TIMEOUT_SECONDS,
    ):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.nvr = nvr
        self.timeout = timeout
        # Lazy, so the agent pickles cleanly into capture worker processes and
        # each process authenticates its own session.
        self._session: requests.Session | None = None

        logger.info("Initialised Dahua capture agent for %s as user %s", self.url, username)

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_session"] = None
        return state

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.auth = HTTPDigestAuth(self.username, self.password)
        return self._session

    def _device_channel(self, channel_id: str) -> str:
        return self.nvr.device_channel(channel_id) if self.nvr else channel_id

    @backoff.on_exception(
        backoff.expo,
        requests.exceptions.RequestException,
        max_tries=MAX_CAPTURE_TRIES,
        jitter=backoff.full_jitter,
        giveup=_fatal_auth,
    )
    def capture_image(
        self, channel_id: str, resolution: VideoResolution | None = None
    ) -> tuple[bytes, str]:
        """Fetch one frame. Returns (image_bytes, file_extension)."""
        del resolution  # SnapFormat decides; see the class docstring.
        full_url = f"{self.url}/cgi-bin/snapshot.cgi?channel={self._device_channel(channel_id)}"

        started_at = time.monotonic()
        response = self._get_session().get(full_url, timeout=self.timeout)
        elapsed = time.monotonic() - started_at
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        extension = CONTENT_TYPE_TO_EXTENSION.get(content_type)
        if extension is None:
            raise ValueError(
                f"NVR returned Content-Type {content_type!r} for channel {channel_id}, expected an image"
            )

        content = response.content
        logger.debug("Captured %d bytes for channel %s in %.2fs", len(content), channel_id, elapsed)
        return content, extension


class DahuaFootageClient:
    """Asks a Dahua CGI recorder what it holds, and downloads it.

    The same interface `NVRFootageClient` presents -- `search` yielding
    `RecordedSegment`s and `download` writing one of them -- so the indexer and
    the archiver never know which protocol a channel speaks.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        nvr: NVRConfig | None = None,
        timeout: tuple[float, float] = FOOTAGE_TIMEOUT_SECONDS,
    ):
        self.url = url.rstrip("/")
        self.nvr = nvr
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(username, password)
        self._device_zone: timezone | None = None

    def _device_channel(self, channel_id: str) -> str:
        return self.nvr.device_channel(channel_id) if self.nvr else channel_id

    def device_zone(self) -> timezone:
        """The device's UTC offset, measured against this host's clock and cached.

        The device runs local wall time and admits no offset anywhere --
        `getCurrentTime` is bare local time, and the HTTP Date header stamps
        that same local time "GMT". So the offset is taken as (device now -
        our now), rounded to the quarter hour every real zone sits on.
        """
        if self._device_zone is None:
            response = self.session.get(
                f"{self.url}/cgi-bin/global.cgi?action=getCurrentTime", timeout=self.timeout
            )
            response.raise_for_status()
            _, _, value = response.text.strip().partition("=")
            device_local = datetime.strptime(value.strip(), DEVICE_TIME_FORMAT)
            drift = device_local - datetime.now(tz=timezone.utc).replace(tzinfo=None)
            steps = round(drift / ZONE_GRANULARITY)
            self._device_zone = timezone(steps * ZONE_GRANULARITY)
            logger.info("Dahua NVR clock offset is %s", self._device_zone)
        return self._device_zone

    def _search_time(self, moment: datetime) -> str:
        return moment.astimezone(self.device_zone()).strftime(DEVICE_TIME_FORMAT)

    def _parse_time(self, text: str) -> datetime | None:
        try:
            parsed = datetime.strptime(text.strip(), DEVICE_TIME_FORMAT)
        except ValueError:
            return None
        return parsed.replace(tzinfo=self.device_zone()).astimezone(timezone.utc)

    def _command(self, path: str) -> requests.Response:
        response = self.session.get(f"{self.url}{path}", timeout=self.timeout)
        response.raise_for_status()
        return response

    def search(self, channel: str, start: datetime, end: datetime) -> Iterator["RecordedSegment"]:
        """Every recorded segment for this channel overlapping [start, end].

        Asked as a series of bounded windows rather than one open-ended finder:
        whether this firmware silently caps a finder's results (the Hikvision
        does, at 4,000, with a straight face) is unverified, and a window short
        enough that motion-only recording cannot fill any plausible cap makes
        the question moot. Callers upsert on (channel, started_at), so the
        duplicate straddling a seam is harmless.
        """
        window_start = start
        while window_start < end:
            window_end = min(window_start + SEARCH_WINDOW, end)
            yield from self._search_window(channel, window_start, window_end)
            window_start = window_end

    def oldest_recording(self, channel: str, start: datetime, end: datetime) -> datetime | None:
        """When the oldest segment the device still holds in [start, end] starts.

        The device wraps its quota by deleting oldest footage first, so this is
        its retention horizon: everything before it is gone for good. Asked
        window by window from `start` -- a few cheap empty finders over the
        recycled region, then one real window -- with the minimum taken over
        that whole window rather than its first result, because whether this
        firmware answers in time order is unverified. `start` wants to sit at
        or before the oldest segment the caller still believes in; None means
        the device answered the whole span with nothing.
        """
        window_start = start
        while window_start < end:
            window_end = min(window_start + SEARCH_WINDOW, end)
            segments = self._search_window(channel, window_start, window_end)
            if segments:
                return min(segment.started_at for segment in segments)
            window_start = window_end
        return None

    def _search_window(
        self, channel: str, start: datetime, end: datetime
    ) -> list["RecordedSegment"]:
        finder = None
        segments: list["RecordedSegment"] = []
        try:
            response = self._command("/cgi-bin/mediaFileFind.cgi?action=factory.create")
            _, _, finder = response.text.strip().partition("=")
            finder = finder.strip()
            if not finder:
                raise ValueError("mediaFileFind factory.create returned no finder id")

            # condition.Channel is 1-based here (0 is Bad Request), and
            # Types[0]=dav keeps snapshot files out of the answer.
            condition = (
                f"action=findFile&object={finder}"
                f"&condition.Channel={int(self._device_channel(channel))}"
                f"&condition.StartTime={quote(self._search_time(start))}"
                f"&condition.EndTime={quote(self._search_time(end))}"
                f"&condition.Types[0]=dav"
            )
            try:
                self._command(f"/cgi-bin/mediaFileFind.cgi?{condition}")
            except requests.exceptions.HTTPError as error:
                # An empty window answers 400 "Error" on some firmwares. Auth
                # failures must not be mistaken for that.
                if _fatal_auth(error):
                    raise
                return segments

            for _ in range(MAX_PAGES_PER_FINDER):
                page = self._command(
                    f"/cgi-bin/mediaFileFind.cgi?action=findNextFile"
                    f"&object={finder}&count={FIND_PAGE_SIZE}"
                )
                found, items = _parse_find_response(page.text)
                for item in items:
                    segment = self._parse_item(channel, item)
                    if segment is not None:
                        segments.append(segment)
                if found < FIND_PAGE_SIZE:
                    return segments
            logger.warning(
                "Channel %s search still answering full pages after %d; stopping this window",
                channel, MAX_PAGES_PER_FINDER,
            )
            return segments
        finally:
            if finder:
                for action in ("close", "destroy"):
                    try:
                        self._command(f"/cgi-bin/mediaFileFind.cgi?action={action}&object={finder}")
                    except requests.exceptions.RequestException:
                        pass  # The device expires abandoned finders on its own.

    def _parse_item(self, channel: str, item: dict[str, str]) -> "RecordedSegment | None":
        from timelapsed.nvr_footage import RecordedSegment

        file_path = item.get("FilePath", "")
        if not file_path.endswith(".dav"):
            return None
        started_at = self._parse_time(item.get("StartTime", ""))
        ended_at = self._parse_time(item.get("EndTime", ""))
        if started_at is None or ended_at is None:
            logger.warning(
                "Channel %s returned unreadable segment times %r..%r",
                channel, item.get("StartTime"), item.get("EndTime"),
            )
            return None
        try:
            # Cluster-aligned allocation, not file size. Kept because it is the
            # only size the device offers and progress display wants one.
            size_bytes = int(item.get("Length", "0"))
        except ValueError:
            size_bytes = 0
        return RecordedSegment(
            channel=channel,
            started_at=started_at,
            ended_at=ended_at,
            size_bytes=size_bytes,
            playback_uri=file_path,
        )

    @backoff.on_exception(
        backoff.expo,
        requests.exceptions.RequestException,
        max_tries=4,
        jitter=backoff.full_jitter,
        giveup=_fatal_auth,
    )
    def download(self, playback_uri: str, destination: Path, deadline_seconds: float) -> int:
        """Fetch one recorded segment to `destination`. Returns bytes written.

        `RPC_Loadfile{FilePath}`, the path verbatim from search with its
        URL-significant characters percent-encoded. The first chunk must open
        with the DHAV magic -- the device answers some failures as 200 with a
        text body, so the first bytes are the only honest statement of what
        arrived. Deadline semantics as in the ISAPI client: wall-clock over
        the whole transfer, because a stream that trickles forever is the
        failure per-read timeouts never trip.
        """
        written = 0
        started = time.monotonic()
        with self.session.get(
            f"{self.url}/cgi-bin/RPC_Loadfile{quote(playback_uri, safe='/')}",
            timeout=self.timeout,
            stream=True,
        ) as response:
            response.raise_for_status()
            with open(destination, "wb") as out:
                for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                    if written == 0 and not chunk.startswith(DHAV_MAGIC):
                        raise ValueError(
                            f"Download answered something other than DHAV: {chunk[:120]!r}"
                        )
                    out.write(chunk)
                    written += len(chunk)
                    if time.monotonic() - started > deadline_seconds:
                        raise TimeoutError(
                            f"Download passed its {deadline_seconds:.0f}s deadline "
                            f"after {written} bytes"
                        )
        return written
