from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from timelapsed.image_capture_library import (
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
