"""The recognition endpoints, over real HTTP like the rest of the viewer tests."""
import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from tests.conftest import BASE_TIME
from timelapsed.analysis.index import AnalysisIndex, to_epoch
from timelapsed.web import build_server

BASE = to_epoch(BASE_TIME)


@pytest.fixture
def seeded_index(config):
    """An index with one person event, one vehicle event, a plate and a name."""
    config.analysis_crop_root.mkdir(parents=True, exist_ok=True)
    crop = config.analysis_crop_root / "event" / "20250601"
    crop.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 60), (90, 90, 90)).save(crop / "1.jpg")

    with AnalysisIndex(config.analysis_index_path) as index:
        person = index.open_event("1", "person", BASE, 0.9)
        index.extend_event(person, BASE + 120, 0.95)
        index.set_event_thumb(person, "event/20250601/1.jpg")

        vehicle = index.open_event("1", "vehicle", BASE + 600, 0.88)
        index.add_plate(vehicle, "1", BASE + 600, "ABC1D23", 0.93, 4, None)

        identity = index.create_identity("person", BASE)
        index.add_signature(identity, person, "body", b"\x00" * 16, 300.0, BASE)
        index.assign_identity(person, identity)
        index.rename_identity(identity, "Someone")
        yield index


@pytest.fixture
def base_url(seeded_index, config):
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get_json(url: str):
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def post_json(url: str, payload: dict):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def test_activity_returns_a_bucket_series_per_kind(base_url):
    payload = get_json(f"{base_url}/api/activity?channel=1&start={BASE}&end={BASE + 1200}&buckets=12")
    assert set(payload) == {"person", "vehicle"}
    assert len(payload["person"]) == 12
    assert sum(payload["person"]) > 0
    assert sum(payload["vehicle"]) > 0


def test_activity_requires_a_channel_and_a_window(base_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        get_json(f"{base_url}/api/activity?channel=1")
    assert error.value.code == 400


def test_events_can_be_filtered_by_kind(base_url):
    events = get_json(f"{base_url}/api/events?channel=1&kind=vehicle")
    assert len(events) == 1
    assert events[0]["kind"] == "vehicle"


def test_events_can_be_filtered_to_one_identity(base_url):
    """This is the 'every time this person appeared' query."""
    identity = get_json(f"{base_url}/api/identities")[0]
    events = get_json(f"{base_url}/api/events?identity={identity['id']}")
    assert len(events) == 1
    assert events[0]["kind"] == "person"


def test_identities_report_their_name_and_sighting_count(base_url):
    identities = get_json(f"{base_url}/api/identities?kind=person")
    assert identities[0]["name"] == "Someone"
    assert identities[0]["sightings"] == 1


def test_plates_are_searchable(base_url):
    assert len(get_json(f"{base_url}/api/plates?text=ABC")) == 1
    assert get_json(f"{base_url}/api/plates?text=ZZZ") == []


def test_plate_search_percent_decodes_its_query(base_url):
    """The old hand-rolled query parser never decoded values; free-text search
    is the first endpoint where that actually matters.

    %31 is '1', so a hit here proves the value was decoded -- an undecoded
    parser would search for the literal 'ABC%31D23' and find nothing.
    """
    assert len(get_json(f"{base_url}/api/plates?text=ABC%31D23")) == 1
    assert len(get_json(f"{base_url}/api/plates?text=ABC1D23")) == 1


def test_an_event_crop_is_served(base_url):
    with urllib.request.urlopen(f"{base_url}/crop/event/1.jpg") as response:
        assert response.headers["Content-Type"] == "image/jpeg"
        assert response.read()[:2] == b"\xff\xd8"


def test_a_missing_crop_is_a_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{base_url}/crop/event/999.jpg")
    assert error.value.code == 404


def test_crop_paths_cannot_escape_the_crop_root(base_url, config):
    """Mirrors the traversal guard on video serving."""
    for path in ("/crop/event/../../etc/passwd", "/crop/../secret.jpg", "/crop/other/1.jpg"):
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base_url}{path}")
        assert error.value.code == 404


def test_renaming_an_identity_persists(base_url, config):
    identity = get_json(f"{base_url}/api/identities")[0]
    status, body = post_json(f"{base_url}/api/identities/{identity['id']}", {"name": "Renamed"})

    assert status == 200 and body == {"ok": True}
    assert get_json(f"{base_url}/api/identities")[0]["name"] == "Renamed"


def test_an_identity_name_can_be_cleared(base_url):
    identity = get_json(f"{base_url}/api/identities")[0]
    post_json(f"{base_url}/api/identities/{identity['id']}", {"name": None})
    assert get_json(f"{base_url}/api/identities")[0]["name"] is None


def test_renaming_an_unknown_identity_is_a_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        post_json(f"{base_url}/api/identities/9999", {"name": "Nobody"})
    assert error.value.code == 404


def test_a_name_with_accents_survives_the_round_trip(base_url):
    identity = get_json(f"{base_url}/api/identities")[0]
    post_json(f"{base_url}/api/identities/{identity['id']}", {"name": "José da Silva"})
    assert get_json(f"{base_url}/api/identities")[0]["name"] == "José da Silva"


def test_malformed_post_bodies_are_rejected(base_url):
    identity = get_json(f"{base_url}/api/identities")[0]
    request = urllib.request.Request(
        f"{base_url}/api/identities/{identity['id']}", data=b"not json",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400


def test_posting_to_an_unknown_path_is_a_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        post_json(f"{base_url}/api/nonsense", {"name": "x"})
    assert error.value.code == 404


def test_identities_carry_a_representative_crop(base_url):
    """The URL has to come from the API. Deriving it from the identity id
    happened to work while ids lined up, and showed the wrong person once
    they stopped."""
    identity = get_json(f"{base_url}/api/identities")[0]
    assert identity["thumb"] == "/crop/event/1.jpg"

    with urllib.request.urlopen(f"{base_url}{identity['thumb']}") as response:
        assert response.headers["Content-Type"] == "image/jpeg"


def test_an_identity_with_no_crop_reports_none(base_url, config):
    with AnalysisIndex(config.analysis_index_path) as index:
        index.create_identity("person", BASE)

    bare = [i for i in get_json(f"{base_url}/api/identities") if i["sightings"] == 0]
    assert bare and bare[0]["thumb"] is None


def test_status_reports_how_far_analysis_has_reached(base_url, config):
    """An empty activity lane and an unanalysed one look identical without this."""
    with AnalysisIndex(config.analysis_index_path) as index:
        index.set_watermark("1", BASE + 3600)

    assert get_json(f"{base_url}/api/status") == {"1": "2025-06-01T13:00:00+00:00"}


def test_the_library_page_is_served(base_url):
    with urllib.request.urlopen(f"{base_url}/library") as response:
        page = response.read().decode()
    assert response.headers["Content-Type"].startswith("text/html")
    assert "People &amp; plates" in page
    # It fetches everything; nothing is server-rendered into it.
    assert "/api/identities" in page and "/api/events?identity=" in page


def test_the_library_is_absent_when_recognition_is_off(library, config, tmp_path):
    config.analysis_enabled = False
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{url}/library")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_viewer_links_to_the_library_when_recognition_is_on(base_url):
    with urllib.request.urlopen(base_url) as response:
        assert 'href="/library"' in response.read().decode()


def test_sightings_carry_what_a_deep_link_needs(base_url):
    """Each sighting has to name a channel, a moment and its own still, or the
    library cannot hand it back to the viewer."""
    identity = get_json(f"{base_url}/api/identities")[0]
    sighting = get_json(f"{base_url}/api/events?identity={identity['id']}")[0]

    assert sighting["channel"] == "1"
    assert sighting["starts"].startswith("2025-06-01T12:00")
    assert sighting["thumb"] == "/crop/event/1.jpg"


def test_the_page_advertises_that_recognition_is_available(base_url):
    with urllib.request.urlopen(base_url) as response:
        page = response.read().decode()
    assert 'id="recognition-payload"' in page
    assert ">true<" in page


def test_the_viewer_works_when_recognition_is_disabled(library, config, tmp_path):
    """Recognition is optional; with it off the viewer must still serve clips."""
    source = tmp_path / "render.mp4"
    source.write_bytes(b"\x00" * 1024)
    library.store_timelapse(
        "1", source, "daily", BASE_TIME, BASE_TIME + timedelta(hours=1)
    )

    config.analysis_enabled = False
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://{server.server_address[0]}:{server.server_address[1]}"
        with urllib.request.urlopen(url) as response:
            assert ">false<" in response.read().decode()
        assert len(get_json(f"{url}/api/timelapses")) > 0
        with pytest.raises(urllib.error.HTTPError) as error:
            get_json(f"{url}/api/identities")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
