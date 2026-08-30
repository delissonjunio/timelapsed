"""The daily keyframe track, and the month-long renders it feeds.

A monthly video cannot read the still library: the stills are pruned in eight
days and a month of them would be ~380 GB for six channels. So one still a day is
hardlinked into a keyframe track that is kept for years, and the monthly and
progress renders read that instead.
"""
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timelapsed.image_capture_library import ImageCaptureLibrary
from timelapsed.image_processor import generate_timelapse
from timelapsed.schema import CADENCES
from timelapsed.timelapsed import pending_keyframes, pending_render_windows, promote_keyframes
from tests.conftest import requires_ffmpeg

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def noon_utc(year: int, month: int, day: int) -> datetime:
    """Local noon in Sao Paulo, as the UTC instant a keyframe is named for."""
    return datetime.combine(date(year, month, day), time(12, 0), tzinfo=SAO_PAULO).astimezone(timezone.utc)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
def keyframe_config(config):
    """The deployed shape: monthly and progress renders on a Sao Paulo noon."""
    config.timelapse_cadences = [CADENCES["hourly"], CADENCES["monthly"], CADENCES["progress"]]
    config.render_timezone = SAO_PAULO
    config.keyframe_at = time(12, 0)
    config.keyframe_tolerance = timedelta(minutes=30)
    config.keyframe_retention = None
    config.timelapse_retention = {"hourly": None, "monthly": None, "progress": None}
    config.timelapse_min_frames_by_cadence = {"monthly": 5, "progress": 5}
    config.timelapse_output_fps_by_cadence = {"monthly": 6, "progress": 6}
    return config


def store_stills_across(library, channel_id, jpeg_bytes, start, end, spacing=timedelta(seconds=10)):
    """Fill [start, end] with stills, as a live channel would."""
    taken_at = start
    while taken_at <= end:
        library.store_image(channel_id, "jpg", jpeg_bytes, taken_at)
        taken_at += spacing


def store_video(library, tmp_path: Path, channel_id, cadence_name, starts, finishes) -> Path:
    """Stand in for a finished render: what matters here is the filename."""
    source = tmp_path / "rendered.mp4"
    source.write_bytes(b"v" * 512)
    return library.store_timelapse(channel_id, source, cadence_name, starts, finishes)


# --- promotion -------------------------------------------------------------

def test_promotion_picks_the_still_nearest_local_noon(library, keyframe_config, jpeg_bytes):
    # Sao Paulo is UTC-3, so local noon on 1 June is 15:00 UTC.
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 14), utc(2025, 6, 1, 16))

    promoted = promote_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 18))

    assert promoted == 1
    assert library.image_timestamps("1", "keyframe") == [noon_utc(2025, 6, 1)]


def test_the_promoted_frame_is_the_still_itself_not_a_copy(library, keyframe_config, jpeg_bytes):
    """The hardlink is the whole design: promotion has to cost no bytes."""
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 14), utc(2025, 6, 1, 16))
    promote_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 18))

    keyframe = next((library.root_path / "1" / "keyframe").iterdir())
    still = library.retrieve_image("1", noon_utc(2025, 6, 1), timedelta(0))

    assert keyframe.stat().st_ino == still.stat().st_ino
    assert keyframe.stat().st_nlink == 2


def test_a_promoted_frame_survives_the_still_being_pruned(library, keyframe_config, jpeg_bytes):
    """The point of the feature, in one assertion.

    Retention unlinks the still eight days from now. The keyframe is the other
    name on the same inode, so the frame is still there to render from.
    """
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 14), utc(2025, 6, 1, 16))
    promote_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 18))

    pruned = library.prune("1", "image", timedelta(days=1), utc(2025, 6, 20))

    assert pruned > 0
    assert library.image_timestamps("1", "image") == []
    keyframe = next((library.root_path / "1" / "keyframe").iterdir())
    assert keyframe.read_bytes() == jpeg_bytes
    assert keyframe.stat().st_nlink == 1


def test_promotion_is_idempotent(library, keyframe_config, jpeg_bytes):
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 14), utc(2025, 6, 1, 16))
    now = utc(2025, 6, 1, 18)

    assert promote_keyframes(library, keyframe_config, "1", now) == 1
    assert promote_keyframes(library, keyframe_config, "1", now) == 0
    assert len(library.image_timestamps("1", "keyframe")) == 1


def test_a_day_with_no_still_near_noon_is_simply_absent(library, keyframe_config, jpeg_bytes):
    """The camera was down over noon. There is nothing to promote and nothing to retry."""
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 20), utc(2025, 6, 1, 22))

    assert promote_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 23)) == 0
    assert library.image_timestamps("1", "keyframe") == []


def test_promotion_backfills_days_a_restart_missed(library, keyframe_config, jpeg_bytes):
    for day in (1, 2, 3, 4):
        store_stills_across(
            library, "1", jpeg_bytes, utc(2025, 6, day, 14, 55), utc(2025, 6, day, 15, 5)
        )

    promoted = promote_keyframes(library, keyframe_config, "1", utc(2025, 6, 4, 18))

    assert promoted == 4
    assert library.image_timestamps("1", "keyframe") == [noon_utc(2025, 6, day) for day in (1, 2, 3, 4)]


def test_today_is_not_a_candidate_before_its_keyframe_time(library, keyframe_config, jpeg_bytes):
    """Promoting the nearest still to a noon that has not happened yet would take
    the morning's frame and then never revisit it."""
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 10), utc(2025, 6, 1, 13))

    # 13:00 UTC is 10:00 in Sao Paulo, so today's noon is still three hours off.
    before = pending_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 13))
    after = pending_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 18))

    assert noon_utc(2025, 6, 1) not in before
    assert after[0] == noon_utc(2025, 6, 1)


def test_a_candidate_day_with_no_still_is_offered_but_promotes_nothing(
    library, keyframe_config, jpeg_bytes
):
    """`pending_keyframes` lists days that *should* have one; whether a still is
    close enough to fill them is `promote_keyframes`' problem."""
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 14, 55), utc(2025, 6, 1, 15, 5))

    offered = pending_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 18))

    assert len(offered) > 1  # a week of retention, only one day of stills
    assert promote_keyframes(library, keyframe_config, "1", utc(2025, 6, 1, 18)) == 1


def test_promotion_reaches_no_further_back_than_the_stills_do(library, keyframe_config, jpeg_bytes):
    """A pruned still cannot be promoted, so there is no point offering the day."""
    keyframe_config.image_retention = timedelta(days=3)

    missing = pending_keyframes(library, keyframe_config, "1", utc(2025, 6, 10, 18), keyframes=[])

    assert missing[0] == noon_utc(2025, 6, 10)
    assert min(missing) >= noon_utc(2025, 6, 7)


def test_a_channel_whose_camera_died_still_promotes_the_days_it_has(library, keyframe_config, jpeg_bytes):
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 1, 14, 55), utc(2025, 6, 1, 15, 5))
    store_stills_across(library, "1", jpeg_bytes, utc(2025, 6, 3, 14, 55), utc(2025, 6, 3, 15, 5))

    assert promote_keyframes(library, keyframe_config, "1", utc(2025, 6, 3, 18)) == 2
    assert library.image_timestamps("1", "keyframe") == [noon_utc(2025, 6, 1), noon_utc(2025, 6, 3)]


# --- monthly windows -------------------------------------------------------

def _monthly_windows(library, config, now):
    return pending_render_windows(library, config, "1", CADENCES["monthly"], now)


def test_monthly_windows_step_by_calendar_month(library, keyframe_config, populate_keyframes):
    # Every day from 1 January to 4 May 2025.
    populate_keyframes("1", count=124, end=noon_utc(2025, 5, 4))

    windows = _monthly_windows(library, keyframe_config, utc(2025, 5, 5, 12))

    # Newest first, and the month still filling up is not offered.
    starts = [start for start, _ in windows]
    assert starts == [
        datetime(2025, month, 1, tzinfo=SAO_PAULO).astimezone(timezone.utc)
        for month in (4, 3, 2, 1)
    ]
    # February is 28 days, not the 31-day nominal window.
    february_start, february_end = windows[2]
    assert february_end - february_start == timedelta(days=28)


def test_monthly_reads_the_keyframe_track_not_the_stills(library, keyframe_config, populate_keyframes):
    """Stills only ever cover the last few days, so reading them would render nothing."""
    populate_keyframes("1", count=124, end=noon_utc(2025, 5, 4), keep_stills=False)

    assert library.image_timestamps("1", "image") == []
    assert _monthly_windows(library, keyframe_config, utc(2025, 5, 5, 12))


def test_monthly_reaches_back_far_past_the_image_retention(library, keyframe_config, populate_keyframes):
    """The horizon has to come from the track the cadence reads, not from the stills.

    Bounding a keyframe-sourced cadence by `image_retention` would cap its
    backfill at eight days and it would never render a month at all.
    """
    keyframe_config.image_retention = timedelta(days=8)
    populate_keyframes("1", count=124, end=noon_utc(2025, 5, 4), keep_stills=False)

    windows = _monthly_windows(library, keyframe_config, utc(2025, 5, 5, 12))

    assert len(windows) == 4
    assert windows[-1][0] < utc(2025, 5, 5) - timedelta(days=90)


def test_a_month_already_rendered_is_not_offered_again(
    library, keyframe_config, populate_keyframes, tmp_path
):
    populate_keyframes("1", count=124, end=noon_utc(2025, 5, 4))
    april = (
        datetime(2025, 4, 1, tzinfo=SAO_PAULO).astimezone(timezone.utc),
        datetime(2025, 5, 1, tzinfo=SAO_PAULO).astimezone(timezone.utc),
    )
    store_video(library, tmp_path, "1", "monthly", *april)

    starts = [start for start, _ in _monthly_windows(library, keyframe_config, utc(2025, 5, 5, 12))]

    assert april[0] not in starts


def test_a_month_with_too_few_keyframes_is_skipped(library, keyframe_config, populate_keyframes):
    keyframe_config.timelapse_min_frames_by_cadence = {"monthly": 20, "progress": 5}
    # Only the last four days of April.
    populate_keyframes("1", count=4, end=noon_utc(2025, 4, 30))

    assert _monthly_windows(library, keyframe_config, utc(2025, 5, 5, 12)) == []


# --- the cumulative progress render ---------------------------------------

def _progress_windows(library, config, now):
    return pending_render_windows(library, config, "1", CADENCES["progress"], now)


def test_progress_offers_one_window_reaching_back_to_the_first_keyframe(
    library, keyframe_config, populate_keyframes
):
    promoted = populate_keyframes("1", count=40, end=noon_utc(2025, 6, 1))

    windows = _progress_windows(library, keyframe_config, utc(2025, 6, 2, 12))

    assert len(windows) == 1
    start, end = windows[0]
    assert start == promoted[0]
    # The end is floored to a local day, so the video covers whole days only.
    assert end == datetime(2025, 6, 2, tzinfo=SAO_PAULO).astimezone(timezone.utc)


def test_progress_is_not_offered_once_a_video_already_reaches_that_far(
    library, keyframe_config, populate_keyframes, tmp_path
):
    promoted = populate_keyframes("1", count=40, end=noon_utc(2025, 6, 1))
    now = utc(2025, 6, 2, 12)
    (start, end), = _progress_windows(library, keyframe_config, now)
    store_video(library, tmp_path, "1", "progress", start, end)

    assert _progress_windows(library, keyframe_config, now) == []


def test_progress_is_offered_again_when_its_end_advances(
    library, keyframe_config, populate_keyframes, tmp_path
):
    """The regression test for the trap this cadence exists inside.

    A cumulative video's start is the first keyframe ever captured and never
    moves, so the usual "is a video already stored whose start falls in this
    period" check latches true after the first render and never fires again.
    Done-ness has to be judged on the end.
    """
    populate_keyframes("1", count=40, end=noon_utc(2025, 6, 1))
    (first_start, first_end), = _progress_windows(library, keyframe_config, utc(2025, 6, 2, 12))
    store_video(library, tmp_path, "1", "progress", first_start, first_end)

    windows = _progress_windows(library, keyframe_config, utc(2025, 6, 3, 12))

    assert len(windows) == 1
    next_start, next_end = windows[0]
    assert next_start == first_start  # unchanged, which is exactly the trap
    assert next_end > first_end


def test_progress_needs_more_than_one_day_of_keyframes(library, keyframe_config, populate_keyframes):
    populate_keyframes("1", count=1, end=noon_utc(2025, 6, 1))

    assert _progress_windows(library, keyframe_config, utc(2025, 6, 2, 12)) == []


def test_superseded_progress_videos_are_dropped_and_the_newest_kept(
    library, populate_keyframes, tmp_path
):
    populate_keyframes("1", count=3, end=noon_utc(2025, 6, 3))
    start = noon_utc(2025, 6, 1)
    for end_day in (2, 3, 4):
        store_video(library, tmp_path, "1", "progress", start, noon_utc(2025, 6, end_day))
    store_video(library, tmp_path, "1", "monthly", noon_utc(2025, 5, 1), noon_utc(2025, 6, 1))

    deleted = library.prune_superseded("1", "progress")

    assert deleted == 2
    assert library.rendered_windows("1", "progress") == [(start, noon_utc(2025, 6, 4))]
    # Another cadence's videos are none of its business.
    assert len(library.rendered_windows("1", "monthly")) == 1


# --- rendering -------------------------------------------------------------

@requires_ffmpeg
def test_a_monthly_render_reads_keyframes_and_plays_at_its_own_rate(
    library, keyframe_config, populate_keyframes
):
    from tests.test_image_processor import probe_video

    populate_keyframes("1", count=30, end=noon_utc(2025, 6, 30), keep_stills=False)

    stored = generate_timelapse(
        library, "1", "monthly", noon_utc(2025, 6, 1), noon_utc(2025, 6, 30),
        timelapse_duration=timedelta(seconds=60),
        output_fps=6,
        min_frames=5,
        source="keyframe",
    )

    assert stored is not None
    assert stored.name.startswith("monthly_")
    # 30 frames at 6 fps is five seconds, whatever the container reports its rate
    # as after the playback-rate padding.
    assert 4.5 <= float(probe_video(stored)["duration"]) <= 5.5


@requires_ffmpeg
def test_deflicker_does_not_cost_any_frames(library, keyframe_config, populate_keyframes):
    from tests.test_image_processor import probe_video

    populate_keyframes("1", count=30, end=noon_utc(2025, 6, 30), keep_stills=False)
    render = lambda deflicker: generate_timelapse(  # noqa: E731
        library, "1", "monthly" if deflicker else "weekly",
        noon_utc(2025, 6, 1), noon_utc(2025, 6, 30),
        timelapse_duration=timedelta(seconds=60),
        output_fps=6, min_frames=5, source="keyframe", deflicker=deflicker,
    )

    plain = probe_video(render(False))
    smoothed = probe_video(render(True))

    assert abs(float(plain["duration"]) - float(smoothed["duration"])) < 0.2


def _png_time(offset: int) -> datetime:
    return utc(2025, 6, 1, 12) + timedelta(seconds=10 * offset)


@requires_ffmpeg
def test_a_render_survives_a_channel_that_answers_png(library, png_bytes):
    """The staged frames and the ffmpeg input pattern have to agree on the extension.

    The NVR client accepts image/png as well as image/jpeg, so a channel can
    legitimately fill the library with PNGs.
    """
    for offset in range(20):
        library.store_image("9", "png", png_bytes, _png_time(offset))

    stored = generate_timelapse(
        library, "9", "hourly", _png_time(0), _png_time(19),
        timelapse_duration=timedelta(seconds=1), output_fps=30, min_frames=5,
    )

    assert stored is not None


# --- dense promotion -------------------------------------------------------

@pytest.fixture
def dense_config(keyframe_config):
    """Every six hours across the daylight window: 06:00, 12:00 and 18:00 local."""
    keyframe_config.keyframe_every = timedelta(hours=6)
    keyframe_config.keyframe_window = (time(6, 0), time(18, 0))
    return keyframe_config


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A Sao Paulo wall-clock instant, as the UTC datetime keyframes are named in."""
    return datetime.combine(
        date(year, month, day), time(hour, minute), tzinfo=SAO_PAULO
    ).astimezone(timezone.utc)


def test_pending_covers_every_instant_of_the_window(library, dense_config):
    """Newest first, only instants that have passed, nothing outside the window."""
    missing = pending_keyframes(library, dense_config, "1", local(2025, 6, 2, 7))

    assert missing[:4] == [
        local(2025, 6, 2, 6),
        local(2025, 6, 1, 18),
        local(2025, 6, 1, 12),
        local(2025, 6, 1, 6),
    ]
    # Never before the window opens or after it closes, whatever the day.
    assert all(6 <= target.astimezone(SAO_PAULO).hour <= 18 for target in missing)


def test_promotion_fills_the_whole_window(library, dense_config, jpeg_bytes):
    store_stills_across(
        library, "1", jpeg_bytes,
        local(2025, 6, 1, 5, 30), local(2025, 6, 1, 18, 30),
        spacing=timedelta(minutes=5),
    )

    promoted = promote_keyframes(library, dense_config, "1", local(2025, 6, 1, 19))

    assert promoted == 3
    assert library.image_timestamps("1", "keyframe") == [
        local(2025, 6, 1, 6), local(2025, 6, 1, 12), local(2025, 6, 1, 18)
    ]


def test_an_outage_inside_the_window_leaves_only_that_instant_absent(library, dense_config, jpeg_bytes):
    store_stills_across(
        library, "1", jpeg_bytes,
        local(2025, 6, 1, 5, 30), local(2025, 6, 1, 10),
        spacing=timedelta(minutes=5),
    )
    store_stills_across(
        library, "1", jpeg_bytes,
        local(2025, 6, 1, 14), local(2025, 6, 1, 18, 30),
        spacing=timedelta(minutes=5),
    )

    promote_keyframes(library, dense_config, "1", local(2025, 6, 1, 19))

    assert library.image_timestamps("1", "keyframe") == [
        local(2025, 6, 1, 6), local(2025, 6, 1, 18)
    ]
