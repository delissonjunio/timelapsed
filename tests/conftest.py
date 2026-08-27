"""Shared fixtures.

The suite exercises the real filesystem layout and the real ffmpeg binary; only
the NVR itself is faked, since there is no HTTP server to talk to in CI.
"""
import logging
import subprocess
from datetime import datetime, time, timedelta, timezone
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


@pytest.fixture(scope="session")
def png_bytes(tmp_path_factory) -> bytes:
    """One genuinely valid PNG. The NVR client accepts image/png as well as image/jpeg."""
    if not ffmpeg_available():
        pytest.skip("ffmpeg is needed to synthesise a test PNG")

    output = tmp_path_factory.mktemp("fixture") / "frame.png"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=green:s=64x64:d=1", "-frames:v", "1", str(output), "-y",
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
        timelapse_output_fps_by_cadence={},
        timelapse_min_frames_by_cadence={},
        timelapse_cadences=[CADENCES["hourly"], CADENCES["daily"], CADENCES["weekly"]],
        render_timezone=timezone.utc,
        deflicker_keyframe_renders=True,
        max_concurrent_renders=1,
        image_capture_library_root=tmp_path / "capture",
        image_retention=timedelta(days=7),
        keyframe_at=time(12, 0),
        keyframe_tolerance=timedelta(minutes=30),
        keyframe_retention=None,
        timelapse_retention={"hourly": None, "daily": None, "weekly": None},
        minimum_free_bytes=5_000_000_000,
        web_host="127.0.0.1",
        web_port=0,
        analysis_enabled=True,
        analysis_index_path=tmp_path / "index" / "index.sqlite3",
        analysis_crop_root=tmp_path / "index" / "crops",
        analysis_model_root=tmp_path / "index" / "models",
        analysis_score_threshold=0.5,
        analysis_threads=1,
        analysis_batch_size=200,
        analysis_detection_retention=timedelta(days=30),
        analysis_event_retention=timedelta(days=365),
        analysis_reid_enabled=True,
        analysis_reid_threshold=0.8,
        analysis_reid_merge_threshold=0.75,
        analysis_reid_window=timedelta(hours=12),
        analysis_plate_channels=["1"],
        analysis_plate_confidence=0.7,
        logging_level=logging.INFO,
    )


@pytest.fixture
def populate_keyframes(library: ImageCaptureLibrary, jpeg_bytes: bytes):
    """Promote one frame a day into the keyframe track, ending at `end`.

    `keep_stills=False` unlinks the still afterwards, which is the steady state
    the feature exists for: eight days on, every keyframe is the only name its
    inode still has.
    """

    def _populate(
        channel_id: str = "1",
        count: int = 40,
        end: datetime = BASE_TIME,
        keep_stills: bool = True,
    ) -> list[datetime]:
        promoted_for = [end - timedelta(days=offset) for offset in reversed(range(count))]
        for taken_at in promoted_for:
            still = library.store_image(channel_id, "jpg", jpeg_bytes, taken_at)
            library.store_keyframe(channel_id, still, taken_at)
            if not keep_stills:
                still.unlink()
        return promoted_for

    return _populate


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
