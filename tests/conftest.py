"""Shared fixtures.

The suite exercises the real filesystem layout and the real ffmpeg binary; only
the NVR itself is faked, since there is no HTTP server to talk to in CI.
"""
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.schema import CADENCES, Config, VideoResolution

BASE_TIME = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


requires_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg is not installed")


@pytest.fixture(scope="session")
def jpeg_bytes(tmp_path_factory) -> bytes:
    """One small, genuinely valid JPEG, reused as the payload for every fake frame."""
    if not ffmpeg_available():
        pytest.skip("ffmpeg is needed to synthesise a test JPEG")

    output = tmp_path_factory.mktemp("fixture") / "frame.jpg"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=blue:s=64x64:d=1", "-frames:v", "1", str(output), "-y",
        ],
        check=True,
        capture_output=True,
    )
    return output.read_bytes()


@pytest.fixture
def library(tmp_path: Path) -> ImageCaptureLibrary:
    return ImageCaptureLibrary(tmp_path / "capture")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        nvr_url="http://nvr.invalid",
        nvr_username="tester",
        nvr_password="hunter2",
        channels=["1", "2"],
        capture_interval=timedelta(seconds=5),
        capture_resolution=VideoResolution(width=640, height=480),
        timelapse_video_duration=timedelta(seconds=2),
        timelapse_output_fps=30,
        timelapse_min_frames=10,
        timelapse_cadences=[CADENCES["hourly"], CADENCES["daily"], CADENCES["weekly"]],
        image_capture_library_root=tmp_path / "capture",
        image_retention=timedelta(days=7),
        timelapse_retention={"hourly": None, "daily": None, "weekly": None},
        web_host="127.0.0.1",
        web_port=0,
        logging_level=logging.INFO,
    )


@pytest.fixture
def populate_images(library: ImageCaptureLibrary, jpeg_bytes: bytes):
    """Store `count` frames for a channel, spaced `interval` apart, ending at `end`."""

    def _populate(
        channel_id: str = "1",
        count: int = 120,
        interval: timedelta = timedelta(seconds=5),
        end: datetime = BASE_TIME,
    ) -> list[datetime]:
        timestamps = [end - interval * offset for offset in reversed(range(count))]
        for taken_at in timestamps:
            library.store_image(channel_id, "jpg", jpeg_bytes, taken_at)
        return timestamps

    return _populate
