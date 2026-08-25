import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from timelapsed.image_capture_library import parse_timelapse_filename
from timelapsed.image_processor import generate_timelapse, select_frames
from tests.conftest import BASE_TIME, requires_ffmpeg


def probe_video(path: Path) -> dict:
    """Read back real properties with ffprobe rather than trusting the file exists."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames,width,height,codec_name,r_frame_rate",
            "-of", "default=noprint_wrappers=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


@pytest.mark.parametrize(
    "available, target, expected",
    [
        (100, 10, 10),      # sampled down
        (10, 100, 10),      # fewer than asked for: keep everything
        (10, 10, 10),       # exact fit
        (100, 0, 100),      # zero target disables sampling
        (0, 10, 0),         # nothing to sample
    ],
)
def test_select_frames_returns_the_expected_count(available, target, expected):
    paths = [Path(f"input-{index}.jpg") for index in range(available)]

    assert len(select_frames(paths, target)) == expected


def test_select_frames_samples_evenly_and_preserves_order():
    paths = [Path(f"{index}.jpg") for index in range(100)]

    selected = select_frames(paths, 10)

    assert selected == [Path(f"{index}.jpg") for index in range(0, 100, 10)]
    assert selected == sorted(selected, key=lambda p: int(p.stem))


def test_select_frames_spans_the_whole_range():
    """A day's timelapse must not silently cover only the first few hours."""
    paths = [Path(f"{index:04d}.jpg") for index in range(17280)]

    selected = select_frames(paths, 1800)

    assert selected[0] == paths[0]
    assert selected[-1].stem == "17270"  # within one step of the end


def test_returns_none_when_there_are_no_images(library, config):
    result = generate_timelapse(
        library, "1", "daily", BASE_TIME - timedelta(days=1), BASE_TIME,
        config.timelapse_video_duration,
    )

    assert result is None


def test_returns_none_when_below_min_frames(library, populate_images, config):
    populate_images(count=5, interval=timedelta(seconds=5), end=BASE_TIME)

    result = generate_timelapse(
        library, "1", "hourly", BASE_TIME - timedelta(hours=1), BASE_TIME,
        config.timelapse_video_duration, output_fps=30, min_frames=60,
    )

    assert result is None


@requires_ffmpeg
def test_renders_a_playable_video(library, populate_images, config):
    populate_images(count=120, interval=timedelta(seconds=5), end=BASE_TIME)

    stored = generate_timelapse(
        library, "1", "hourly", BASE_TIME - timedelta(hours=1), BASE_TIME,
        timedelta(seconds=2), output_fps=30, min_frames=10,
    )

    assert stored is not None and stored.exists()
    probed = probe_video(stored)
    assert probed["codec_name"] == "h264"
    assert (int(probed["width"]), int(probed["height"])) == (64, 64)
    assert probed["r_frame_rate"] == "30/1"


@requires_ffmpeg
def test_render_honours_the_requested_duration_by_sampling(library, populate_images):
    """1200 images asked to become a 2s/30fps video must yield 60 frames, not 1200."""
    populate_images(count=1200, interval=timedelta(seconds=3), end=BASE_TIME)

    stored = generate_timelapse(
        library, "1", "daily", BASE_TIME - timedelta(days=1), BASE_TIME,
        timedelta(seconds=2), output_fps=30, min_frames=10,
    )

    assert stored is not None
    assert int(probe_video(stored)["nb_frames"]) == 60


@requires_ffmpeg
def test_render_is_shorter_than_requested_when_images_are_scarce(library, populate_images):
    populate_images(count=45, interval=timedelta(seconds=5), end=BASE_TIME)

    stored = generate_timelapse(
        library, "1", "hourly", BASE_TIME - timedelta(hours=1), BASE_TIME,
        timedelta(seconds=10), output_fps=30, min_frames=10,
    )

    assert stored is not None
    assert int(probe_video(stored)["nb_frames"]) == 45  # all of them, 1.5s of video


@requires_ffmpeg
@pytest.mark.parametrize("cadence", ["hourly", "daily", "weekly"])
def test_stored_name_records_the_cadence_and_window(library, populate_images, cadence):
    populate_images(count=100, interval=timedelta(seconds=5), end=BASE_TIME)
    start = BASE_TIME - timedelta(hours=1)

    stored = generate_timelapse(
        library, "1", cadence, start, BASE_TIME, timedelta(seconds=2), output_fps=30, min_frames=10,
    )

    assert stored is not None
    parsed_cadence, parsed_start, parsed_end = parse_timelapse_filename(stored.stem)
    assert parsed_cadence == cadence
    assert (parsed_start, parsed_end) == (start, BASE_TIME)


@requires_ffmpeg
def test_render_leaves_no_temporary_directories_behind(library, populate_images, tmp_path):
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("timelapse*"))
    populate_images(count=100, interval=timedelta(seconds=5), end=BASE_TIME)

    generate_timelapse(
        library, "1", "hourly", BASE_TIME - timedelta(hours=1), BASE_TIME,
        timedelta(seconds=2), output_fps=30, min_frames=10,
    )

    assert set(Path(tempfile.gettempdir()).glob("timelapse*")) == before


@requires_ffmpeg
def test_render_does_not_consume_the_source_images(library, populate_images):
    populate_images(count=100, interval=timedelta(seconds=5), end=BASE_TIME)
    before = library.retrieve_images_within("1", BASE_TIME - timedelta(hours=1), BASE_TIME)

    generate_timelapse(
        library, "1", "hourly", BASE_TIME - timedelta(hours=1), BASE_TIME,
        timedelta(seconds=2), output_fps=30, min_frames=10,
    )

    after = library.retrieve_images_within("1", BASE_TIME - timedelta(hours=1), BASE_TIME)
    assert before == after
    assert all(path.exists() for path in after)
