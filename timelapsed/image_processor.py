import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Sequence

from .image_capture_library import ImageCaptureLibrary

logger = logging.getLogger(__name__)


def select_frames(image_paths: Sequence[Path], target_frame_count: int) -> Sequence[Path]:
    """Evenly sample image_paths down to at most target_frame_count frames.

    Capturing every 5 seconds yields 17280 images a day; a 60 second video at
    30 fps only needs 1800 of them. Sampling first means ffmpeg (and the temp
    directory it reads from) never sees the other 15480.
    """
    if target_frame_count <= 0 or len(image_paths) <= target_frame_count:
        return image_paths

    step = len(image_paths) / target_frame_count
    return [image_paths[int(index * step)] for index in range(target_frame_count)]


@contextmanager
def _stage_paths_in_temp_directory(paths: Sequence[Path]) -> Iterator[Path]:
    """Stage paths as input-%015d.<ext> so ffmpeg's image2 demuxer can read them in order.

    Hardlinks where the filesystem allows it (no extra bytes, no copy time) and
    falls back to a real copy across filesystem boundaries.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="timelapse_"))
    try:
        for index, path in enumerate(paths):
            temp_path = temp_dir / f"input-{index:0>15}{path.suffix}"
            try:
                os.link(path, temp_path)
            except OSError:
                shutil.copy(path, temp_path)

        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def generate_timelapse(
    library: ImageCaptureLibrary,
    channel_id: str,
    cadence_name: str,
    start_time: datetime,
    end_time: datetime,
    timelapse_duration: timedelta,
    output_fps: int = 30,
    min_frames: int = 60,
) -> Path | None:
    """Render the images captured between start_time and end_time into an MP4.

    The output plays at a fixed output_fps and lasts at most timelapse_duration;
    surplus images are sampled out. If fewer images exist than a full-length
    video needs, every image is used and the video is simply shorter.

    Returns the stored video path, or None when there was nothing worth rendering.
    """
    image_paths = library.retrieve_images_within(channel_id, start_time, end_time)
    if not image_paths:
        logger.warning(
            "No images found for channel %s between %s and %s; skipping %s render",
            channel_id, start_time.isoformat(), end_time.isoformat(), cadence_name,
        )
        return None

    target_frame_count = int(output_fps * timelapse_duration.total_seconds())
    frames = select_frames(image_paths, target_frame_count)

    if len(frames) < min_frames:
        logger.warning(
            "Skipping %s timelapse for channel %s: only %d frames available, minimum is %d",
            cadence_name, channel_id, len(frames), min_frames,
        )
        return None

    logger.info(
        "Rendering %s timelapse for channel %s: %d of %d images at %d fps (~%.1fs of video)",
        cadence_name, channel_id, len(frames), len(image_paths), output_fps, len(frames) / output_fps,
    )

    output_path = Path(tempfile.mkdtemp(prefix="timelapse_out_")) / "timelapse.mp4"
    try:
        with _stage_paths_in_temp_directory(frames) as working_directory:
            ffmpeg_command = [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-loglevel", "error",
                "-framerate", str(output_fps),
                "-pattern_type", "sequence",
                "-i", str((working_directory / "input-%15d.jpg").absolute()),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(output_path.absolute()),
            ]

            try:
                subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as error:
                logger.error(
                    "ffmpeg failed for channel %s (exit %d): %s",
                    channel_id, error.returncode, (error.stderr or "").strip()[:2000],
                )
                raise

        stored_path = library.store_timelapse(channel_id, output_path, cadence_name, start_time, end_time)
        logger.info(
            "Timelapse stored for channel %s: %s (%d bytes)",
            channel_id, stored_path, stored_path.stat().st_size,
        )
        return stored_path
    finally:
        shutil.rmtree(output_path.parent, ignore_errors=True)
