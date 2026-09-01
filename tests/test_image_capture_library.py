import fcntl
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import timelapsed.image_capture_library as icl_module
from timelapsed.image_capture_library import (
    RECLAIM_LOCK_NAME,
    ImageCaptureLibrary,
    _generate_image_filename,
    _parse_image_filename,
    parse_timelapse_filename,
)
from tests.conftest import BASE_TIME


def test_image_filename_round_trips():
    assert _parse_image_filename(_generate_image_filename(BASE_TIME)) == BASE_TIME


def test_image_filenames_sort_chronologically():
    """The whole library relies on lexical order matching time order."""
    timestamps = [BASE_TIME + timedelta(hours=offset) for offset in (0, 5, 13, 26, 400)]
    names = [_generate_image_filename(timestamp) for timestamp in timestamps]

    assert sorted(names) == names


def test_timelapse_filename_round_trips(library: ImageCaptureLibrary, tmp_path: Path):
    source = tmp_path / "render.mp4"
    source.write_bytes(b"video")
    finishes = BASE_TIME + timedelta(days=7)

    stored = library.store_timelapse("1", source, "weekly", BASE_TIME, finishes)

    cadence, starts, parsed_finishes = parse_timelapse_filename(stored.stem)
    assert (cadence, starts, parsed_finishes) == ("weekly", BASE_TIME, finishes)
    assert stored.read_bytes() == b"video"
    assert stored.parent == library.root_path / "1" / "timelapse"


@pytest.mark.parametrize("stem", ["not-a-timelapse", "weekly_garbage", "", "20250601_120000_UTC"])
def test_parse_timelapse_filename_rejects_junk(stem):
    with pytest.raises(ValueError):
        parse_timelapse_filename(stem)


def test_store_image_creates_the_channel_tree(library: ImageCaptureLibrary):
    stored = library.store_image("7", "jpg", b"bytes", BASE_TIME)

    assert stored == library.root_path / "7" / "image" / f"{_generate_image_filename(BASE_TIME)}.jpg"
    assert stored.read_bytes() == b"bytes"


def test_empty_library_reads_return_empty(library: ImageCaptureLibrary):
    """Every read path must tolerate a channel that has never captured anything."""
    assert library.retrieve_images_within("nope", BASE_TIME - timedelta(days=1), BASE_TIME) == []
    assert library.retrieve_image("nope", BASE_TIME, timedelta(minutes=5)) is None
    assert library.prune("nope", "image", timedelta(days=1), BASE_TIME) == 0


def test_retrieve_images_within_is_inclusive_at_both_ends(library, populate_images):
    timestamps = populate_images(count=10, interval=timedelta(minutes=1), end=BASE_TIME)

    found = library.retrieve_images_within("1", timestamps[0], timestamps[-1])

    assert len(found) == 10
    assert found[0].stem == _generate_image_filename(timestamps[0])
    assert found[-1].stem == _generate_image_filename(timestamps[-1])


def test_retrieve_images_within_excludes_outside_the_window(library, populate_images):
    timestamps = populate_images(count=20, interval=timedelta(minutes=1), end=BASE_TIME)

    found = library.retrieve_images_within("1", timestamps[5], timestamps[9])

    assert len(found) == 5


def test_retrieve_images_within_returns_oldest_first(library, populate_images):
    populate_images(count=30, interval=timedelta(seconds=30), end=BASE_TIME)

    found = library.retrieve_images_within("1", BASE_TIME - timedelta(hours=1), BASE_TIME)

    assert [p.stem for p in found] == sorted(p.stem for p in found)


def test_retrieve_images_within_ignores_other_channels(library, populate_images):
    populate_images(channel_id="1", count=5, interval=timedelta(minutes=1), end=BASE_TIME)
    populate_images(channel_id="2", count=9, interval=timedelta(minutes=1), end=BASE_TIME)

    assert len(library.retrieve_images_within("2", BASE_TIME - timedelta(hours=1), BASE_TIME)) == 9


def test_retrieve_images_within_skips_unparseable_files(library, populate_images):
    populate_images(count=5, interval=timedelta(minutes=1), end=BASE_TIME)
    (library.root_path / "1" / "image" / "thumbs.db").write_bytes(b"junk")
    (library.root_path / "1" / "image" / "nested").mkdir()

    assert len(library.retrieve_images_within("1", BASE_TIME - timedelta(hours=1), BASE_TIME)) == 5


def test_latest_image_is_the_newest_still_without_parsing_the_rest(library, populate_images):
    timestamps = populate_images(count=5, interval=timedelta(minutes=1), end=BASE_TIME)
    # Names that would win a plain string comparison but are not frames.
    (library.root_path / "1" / "image" / "thumbs.db").write_bytes(b"junk")
    (library.root_path / "1" / "image" / "99999999_999999_UTC.jpg").mkdir()

    latest = library.latest_image("1")

    assert latest is not None
    assert _parse_image_filename(latest.stem) == timestamps[-1]


def test_latest_image_is_none_for_a_channel_with_nothing(library, populate_images):
    populate_images(channel_id="1", count=2, interval=timedelta(minutes=1), end=BASE_TIME)
    (library.root_path / "2" / "image").mkdir(parents=True)

    assert library.latest_image("2") is None
    assert library.latest_image("nope") is None


def test_retrieve_image_finds_an_exact_match(library, populate_images):
    timestamps = populate_images(count=5, interval=timedelta(minutes=1), end=BASE_TIME)

    found = library.retrieve_image("1", timestamps[2], search_max_distance=None)

    assert found is not None and found.stem == _generate_image_filename(timestamps[2])


def test_retrieve_image_falls_back_to_the_nearest_within_range(library, populate_images):
    timestamps = populate_images(count=5, interval=timedelta(minutes=1), end=BASE_TIME)
    off_by_ten_seconds = timestamps[2] + timedelta(seconds=10)

    found = library.retrieve_image("1", off_by_ten_seconds, search_max_distance=timedelta(seconds=30))

    assert found is not None and found.stem == _generate_image_filename(timestamps[2])


def test_retrieve_image_respects_the_search_distance(library, populate_images):
    timestamps = populate_images(count=5, interval=timedelta(minutes=1), end=BASE_TIME)

    assert library.retrieve_image("1", timestamps[-1] + timedelta(hours=3), timedelta(minutes=1)) is None
    assert library.retrieve_image("1", timestamps[2] + timedelta(seconds=10), None) is None


def test_prune_deletes_only_what_is_older_than_retention(library, populate_images):
    populate_images(count=48, interval=timedelta(hours=1), end=BASE_TIME)

    deleted = library.prune("1", "image", retention=timedelta(hours=24), now=BASE_TIME)

    survivors = library.retrieve_images_within("1", BASE_TIME - timedelta(days=365), BASE_TIME)
    assert deleted == 23
    assert len(survivors) == 25
    assert all(
        _parse_image_filename(path.stem) >= BASE_TIME - timedelta(hours=24) for path in survivors
    )


def test_prune_with_no_retention_keeps_everything(library, populate_images):
    populate_images(count=10, interval=timedelta(days=30), end=BASE_TIME)

    assert library.prune("1", "image", retention=None, now=BASE_TIME) == 0
    assert len(library.retrieve_images_within("1", datetime(2000, 1, 1, tzinfo=timezone.utc), BASE_TIME)) == 10


def test_prune_is_idempotent(library, populate_images):
    populate_images(count=20, interval=timedelta(hours=1), end=BASE_TIME)

    first = library.prune("1", "image", timedelta(hours=5), BASE_TIME)
    second = library.prune("1", "image", timedelta(hours=5), BASE_TIME)

    assert first > 0
    assert second == 0


def test_prune_targets_timelapses_independently(library, populate_images, tmp_path: Path):
    populate_images(count=5, interval=timedelta(days=1), end=BASE_TIME)
    source = tmp_path / "render.mp4"
    source.write_bytes(b"video")
    for age in range(5):
        starts = BASE_TIME - timedelta(days=age)
        library.store_timelapse("1", source, "daily", starts, starts + timedelta(days=1))

    deleted = library.prune("1", "timelapse", timedelta(days=2), BASE_TIME)

    assert deleted == 2
    assert len(library.retrieve_images_within("1", BASE_TIME - timedelta(days=365), BASE_TIME)) == 5


def test_prune_by_cadence_leaves_other_cadences_alone(library, tmp_path: Path):
    source = tmp_path / "render.mp4"
    source.write_bytes(b"video")
    for age in range(10):
        starts = BASE_TIME - timedelta(days=age)
        for cadence in ("hourly", "weekly"):
            library.store_timelapse("1", source, cadence, starts, starts + timedelta(hours=1))

    deleted = library.prune("1", "timelapse", timedelta(days=3), BASE_TIME, cadence_name="hourly")

    survivors = sorted(p.stem for p in (library.root_path / "1" / "timelapse").iterdir())
    assert deleted == 6
    assert sum(1 for stem in survivors if stem.startswith("weekly_")) == 10
    assert sum(1 for stem in survivors if stem.startswith("hourly_")) == 4


@pytest.fixture
def stocked_library(library, tmp_path: Path):
    """A library holding both stills and every cadence of video, across two channels."""
    source = tmp_path / "render.mp4"
    source.write_bytes(b"v" * 1000)
    for channel_id in ("1", "2"):
        for age_days in range(12):
            taken_at = BASE_TIME - timedelta(days=age_days)
            library.store_image(channel_id, "jpg", b"i" * 1000, taken_at)
            for cadence in ("hourly", "daily", "weekly"):
                library.store_timelapse(channel_id, source, cadence, taken_at, taken_at + timedelta(hours=1))
    return library


def _surviving(library, channel_id, target, cadence=None):
    entries = library._timestamped_paths(channel_id, target)
    if cadence:
        entries = [e for e in entries if e[1].stem.startswith(f"{cadence}_")]
    return [timestamp for timestamp, _ in entries]


def test_reclaim_does_nothing_while_free_space_is_above_the_floor(stocked_library, monkeypatch):
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=100, total=0, used=0))

    assert stocked_library.reclaim(["1", "2"], minimum_free_bytes=50, protected_window=timedelta(days=7), now=BASE_TIME) == 0
    assert len(_surviving(stocked_library, "1", "image")) == 12


def test_reclaim_is_disabled_by_a_zero_floor(stocked_library, monkeypatch):
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0, total=0, used=0))

    assert stocked_library.reclaim(["1", "2"], 0, timedelta(days=7), BASE_TIME) == 0
    assert len(_surviving(stocked_library, "1", "image")) == 12


def test_reclaim_takes_expendable_stills_before_any_video(stocked_library, monkeypatch):
    # 8 stills per channel sit past the 7-day window; 3000 bytes is fewer than those 16 files.
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0, total=0, used=0))

    reclaimed = stocked_library.reclaim(["1", "2"], 3000, timedelta(days=7), BASE_TIME)

    assert reclaimed >= 3000
    assert len(_surviving(stocked_library, "1", "timelapse")) == 36  # every video untouched
    survivors = _surviving(stocked_library, "1", "image") + _surviving(stocked_library, "2", "image")
    assert len(survivors) == 24 - 3  # oldest first, globally across channels
    assert min(survivors) > BASE_TIME - timedelta(days=11)


def test_reclaim_walks_the_ladder_and_spares_the_weekly_archive(stocked_library, monkeypatch):
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0, total=0, used=0))

    # 24 stills and 72 videos of 1000 bytes each: 80,000 exhausts everything but
    # part of the weekly archive, which the ladder reaches last.
    reclaimed = stocked_library.reclaim(["1", "2"], 80_000, timedelta(days=7), BASE_TIME)

    assert reclaimed > 0
    for channel_id in ("1", "2"):
        assert _surviving(stocked_library, channel_id, "image") == []
        assert _surviving(stocked_library, channel_id, "timelapse", "hourly") == []
        assert _surviving(stocked_library, channel_id, "timelapse", "daily") == []
        assert len(_surviving(stocked_library, channel_id, "timelapse", "weekly")) < 12


def test_reclaim_skips_when_another_worker_holds_the_lock(stocked_library, monkeypatch):
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0, total=0, used=0))

    held = os.open(stocked_library.root_path / RECLAIM_LOCK_NAME, os.O_CREAT | os.O_WRONLY, 0o644)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        assert stocked_library.reclaim(["1", "2"], 80_000, timedelta(days=7), BASE_TIME) == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    assert len(_surviving(stocked_library, "1", "image")) == 12


# --- images_after ---


def test_images_after_walks_forward_from_a_watermark(library, populate_images):
    timestamps = populate_images("1", count=10, interval=timedelta(seconds=10), end=BASE_TIME)

    found = library.images_after("1", timestamps[3], BASE_TIME, limit=100)

    assert [taken_at for _, taken_at in found] == timestamps[4:]


def test_images_after_with_no_watermark_starts_at_the_beginning(library, populate_images):
    timestamps = populate_images("1", count=5, interval=timedelta(seconds=10), end=BASE_TIME)

    found = library.images_after("1", None, BASE_TIME, limit=100)

    assert [taken_at for _, taken_at in found] == timestamps


def test_images_after_stops_at_the_until_bound(library, populate_images):
    """The newest still on disk may be half-written, so callers hold back."""
    timestamps = populate_images("1", count=10, interval=timedelta(seconds=10), end=BASE_TIME)

    found = library.images_after("1", None, timestamps[5], limit=100)

    assert [taken_at for _, taken_at in found] == timestamps[:6]


def test_images_after_honours_its_limit_and_returns_the_oldest_first(library, populate_images):
    """Oldest-first matters: the caller advances a watermark past what it got,
    so returning an arbitrary subset would skip the frames it did not see."""
    timestamps = populate_images("1", count=20, interval=timedelta(seconds=10), end=BASE_TIME)

    found = library.images_after("1", None, BASE_TIME, limit=5)

    assert [taken_at for _, taken_at in found] == timestamps[:5]


def test_images_after_ignores_files_that_are_not_stills(library, populate_images, jpeg_bytes):
    populate_images("1", count=3, interval=timedelta(seconds=10), end=BASE_TIME)
    (library.root_path / "1" / "image" / "notes.txt").write_text("not a still")
    (library.root_path / "1" / "image" / "partial.jpg").write_bytes(jpeg_bytes)

    found = library.images_after("1", None, BASE_TIME, limit=100)

    assert len(found) == 3


def test_images_after_on_an_unknown_channel_is_empty(library):
    assert library.images_after("nope", None, BASE_TIME, limit=100) == []
# --- the keyframe track ----------------------------------------------------

def test_retrieve_images_within_reads_the_track_it_is_asked_for(library, populate_keyframes):
    promoted = populate_keyframes("1", count=5, end=BASE_TIME, keep_stills=False)

    keyframes = library.retrieve_images_within(
        "1", promoted[0], promoted[-1], target_name="keyframe"
    )

    assert len(keyframes) == 5
    assert library.retrieve_images_within("1", promoted[0], promoted[-1]) == []


def test_frame_entries_hands_back_times_and_paths_together(library, populate_keyframes):
    promoted = populate_keyframes("1", count=3, end=BASE_TIME)

    entries = library.frame_entries("1", "keyframe")

    assert [timestamp for timestamp, _ in entries] == promoted
    assert all(path.is_file() for _, path in entries)


def test_promoting_twice_leaves_one_keyframe(library, jpeg_bytes):
    still = library.store_image("1", "jpg", jpeg_bytes, BASE_TIME)

    library.store_keyframe("1", still, BASE_TIME)
    library.store_keyframe("1", still, BASE_TIME)

    assert library.image_timestamps("1", "keyframe") == [BASE_TIME]


def test_a_keyframe_can_be_named_for_a_different_instant_than_its_still(library, jpeg_bytes):
    """It is named for the noon it was promoted for, so frames land 24h apart."""
    still = library.store_image("1", "jpg", jpeg_bytes, BASE_TIME + timedelta(minutes=17))

    keyframe = library.store_keyframe("1", still, BASE_TIME)

    assert library.image_timestamps("1", "keyframe") == [BASE_TIME]
    assert keyframe.stat().st_ino == still.stat().st_ino


def test_keyframes_are_pruned_on_their_own_retention(library, populate_keyframes):
    populate_keyframes("1", count=10, end=BASE_TIME, keep_stills=False)

    deleted = library.prune("1", "keyframe", timedelta(days=4), BASE_TIME)

    assert deleted == 5
    assert min(library.image_timestamps("1", "keyframe")) >= BASE_TIME - timedelta(days=4)


# --- anchored renders ------------------------------------------------------

def test_rendered_windows_keeps_the_end_that_rendered_window_starts_throws_away(
    library, tmp_path: Path
):
    source = tmp_path / "render.mp4"
    source.write_bytes(b"v" * 100)
    start, end = BASE_TIME - timedelta(days=90), BASE_TIME
    library.store_timelapse("1", source, "progress", start, end)

    assert library.rendered_windows("1", "progress") == [(start, end)]
    assert library.rendered_window_starts("1", "progress") == [start]


def test_prune_superseded_keeps_the_video_that_reaches_furthest(library, tmp_path: Path):
    source = tmp_path / "render.mp4"
    source.write_bytes(b"v" * 100)
    start = BASE_TIME - timedelta(days=90)
    for days in (10, 40, 70):
        library.store_timelapse("1", source, "progress", start, start + timedelta(days=days))
    library.store_timelapse("1", source, "weekly", start, start + timedelta(days=7))

    deleted = library.prune_superseded("1", "progress")

    assert deleted == 2
    assert library.rendered_windows("1", "progress") == [(start, start + timedelta(days=70))]
    assert len(library.rendered_windows("1", "weekly")) == 1


def test_prune_superseded_is_a_no_op_on_a_single_video(library, tmp_path: Path):
    source = tmp_path / "render.mp4"
    source.write_bytes(b"v" * 100)
    library.store_timelapse("1", source, "progress", BASE_TIME - timedelta(days=90), BASE_TIME)

    assert library.prune_superseded("1", "progress") == 0
    assert len(library.rendered_windows("1", "progress")) == 1


# --- reclaim, once stills have a second name -------------------------------

@pytest.fixture
def promoted_library(stocked_library):
    """Every still in the stocked library also promoted, as a live channel would be."""
    for channel_id in ("1", "2"):
        for timestamp, path in stocked_library.frame_entries(channel_id, "image"):
            stocked_library.store_keyframe(channel_id, path, timestamp)
    return stocked_library


def test_reclaim_never_takes_keyframes(promoted_library, monkeypatch):
    """They are the one thing here no amount of CPU can rebuild.

    A pruned still cannot be re-promoted, and at ~500 MB a year against a steady
    state near 100 GB, sacrificing them buys under half a percent of the disk
    while destroying the beginning of the record.
    """
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0, total=0, used=0))

    promoted_library.reclaim(["1", "2"], 10_000_000, timedelta(days=7), BASE_TIME)

    for channel_id in ("1", "2"):
        assert len(_surviving(promoted_library, channel_id, "keyframe")) == 12


def test_reclaim_does_not_count_bytes_a_hardlink_still_holds(promoted_library, monkeypatch):
    """Unlinking a promoted still frees nothing: the inode lives on as the keyframe.

    Counting those bytes would stop the ladder at the first tier while the floor
    it just claimed to have reached was never actually met.
    """
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0, total=0, used=0))

    # 3000 bytes is fewer than the 16 expendable stills hold, so on an unpromoted
    # library the ladder stops in tier one with every video untouched.
    reclaimed = promoted_library.reclaim(["1", "2"], 3000, timedelta(days=7), BASE_TIME)

    assert reclaimed >= 3000
    # It had to walk past the stills into the videos, because unlinking a
    # promoted still bought nothing. Count the bytes and the ladder would have
    # stopped in tier one with all 36 videos untouched.
    assert len(_surviving(promoted_library, "1", "timelapse")) < 36


def test_reclaim_spends_the_regenerable_videos_before_the_weekly_archive(
    stocked_library, tmp_path: Path, monkeypatch
):
    """Monthly and progress videos can be re-rendered from keyframes. A weekly cannot:
    its stills were pruned months ago."""
    source = tmp_path / "render.mp4"
    source.write_bytes(b"v" * 1000)
    for channel_id in ("1", "2"):
        for age_days in range(12):
            taken_at = BASE_TIME - timedelta(days=age_days)
            for cadence in ("monthly", "progress"):
                stocked_library.store_timelapse(
                    channel_id, source, cadence, taken_at, taken_at + timedelta(hours=1)
                )
    monkeypatch.setattr(icl_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0, total=0, used=0))

    # Everything except part of the weekly archive: 24 stills and 120 videos.
    stocked_library.reclaim(["1", "2"], 128_000, timedelta(days=7), BASE_TIME)

    for channel_id in ("1", "2"):
        assert _surviving(stocked_library, channel_id, "timelapse", "monthly") == []
        assert _surviving(stocked_library, channel_id, "timelapse", "progress") == []
        assert len(_surviving(stocked_library, channel_id, "timelapse", "weekly")) < 12
