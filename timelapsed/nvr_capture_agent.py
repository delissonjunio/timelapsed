import logging
import time

import backoff
import requests
from requests.auth import HTTPDigestAuth

from timelapsed.schema import NVRConfig, VideoResolution

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = (5.0, 20.0)  # (connect, read)
MAX_CAPTURE_TRIES = 4

CONTENT_TYPE_TO_EXTENSION = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
}


class NVRCaptureAgent:
    """Pulls single-frame snapshots from an ISAPI (Hikvision-compatible) NVR.

    Uses the still-image endpoint rather than decoding RTSP: one HTTP GET per
    frame, no persistent stream, no video decoding on the client.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        default_resolution: VideoResolution = VideoResolution(1920, 1080),
        timeout: tuple[float, float] = DEFAULT_TIMEOUT_SECONDS,
        nvr: NVRConfig | None = None,
    ):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.default_resolution = default_resolution
        self.timeout = timeout
        # For translating a global channel id back to the device's own number.
        # None means the two are the same, which is true of the default NVR.
        self.nvr = nvr

        logger.info("Initialised NVR capture agent for %s as user %s", self.url, username)

    def _snapshot_url(self, channel_id: str, resolution: VideoResolution) -> str:
        device_channel = self.nvr.device_channel(channel_id) if self.nvr else channel_id
        return (
            f"{self.url}/ISAPI/Streaming/channels/{device_channel}01/picture"
            f"?videoResolutionWidth={resolution.width}&videoResolutionHeight={resolution.height}"
        )

    @backoff.on_exception(
        backoff.expo,
        requests.exceptions.RequestException,
        max_tries=MAX_CAPTURE_TRIES,
        jitter=backoff.full_jitter,
    )
    def capture_image(self, channel_id: str, resolution: VideoResolution | None = None) -> tuple[bytes, str]:
        """Fetch one frame. Returns (image_bytes, file_extension).

        Raises requests.exceptions.RequestException if every retry fails, or
        ValueError if the NVR answered with something that is not an image.
        """
        capture_resolution = resolution or self.default_resolution
        full_url = self._snapshot_url(channel_id, capture_resolution)

        started_at = time.monotonic()
        # A timeout is mandatory: without it a wedged NVR blocks this worker forever.
        response = requests.get(
            full_url,
            auth=HTTPDigestAuth(self.username, self.password),
            timeout=self.timeout,
        )
        elapsed = time.monotonic() - started_at
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        extension = CONTENT_TYPE_TO_EXTENSION.get(content_type)
        if extension is None:
            raise ValueError(
                f"NVR returned Content-Type {content_type!r} for channel {channel_id}, expected an image"
            )

        content = response.content
        logger.debug(
            "Captured %d bytes for channel %s in %.2fs", len(content), channel_id, elapsed
        )
        return content, extension


def capture_agent_for(nvr: NVRConfig):
    """The right capture driver for one NVR, chosen by its configured type."""
    if nvr.kind == "dahua":
        from timelapsed.dahua import DahuaCaptureAgent

        return DahuaCaptureAgent(nvr.url, nvr.username, nvr.password, nvr=nvr)
    return NVRCaptureAgent(nvr.url, nvr.username, nvr.password, nvr=nvr)
