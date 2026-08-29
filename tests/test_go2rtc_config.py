"""The go2rtc config renderer: one stream per camera, in each device's dialect."""
from timelapsed.go2rtc_config import render, stream_source
from timelapsed.schema import NVRConfig


def nvr(name, kind, url="http://nvr.local", username="admin", password="pw", channels=("1",)):
    return NVRConfig(
        name=name, kind=kind, url=url, username=username, password=password,
        device_channels=tuple(channels),
    )


def test_hikvision_streams_use_the_isapi_track_path(config):
    rendered = render(config)

    assert '  ch1: "rtsp://tester:hunter2@nvr.invalid:554/Streaming/Channels/101"' in rendered
    assert '  ch2: "rtsp://tester:hunter2@nvr.invalid:554/Streaming/Channels/201"' in rendered


def test_dahua_streams_use_realmonitor_and_namespaced_names(config):
    config.nvrs.append(nvr("garage", "dahua", url="http://192.168.1.11", channels=("1", "3")))

    rendered = render(config)

    assert (
        '  chgarage-1: "rtsp://admin:pw@192.168.1.11:554/cam/realmonitor?channel=1&subtype=0"'
        in rendered
    )
    assert "chgarage-3:" in rendered
    # The default NVR's names are untouched by the second device existing.
    assert "  ch1: " in rendered


def test_credentials_are_percent_encoded():
    source = stream_source(nvr(None, "dahua", username="a@b", password="p#w:x"), "1")

    assert source.startswith("rtsp://a%40b:p%23w%3Ax@")


def test_the_api_listens_on_localhost_only(config):
    rendered = render(config)

    assert 'listen: "127.0.0.1:1984"' in rendered
    assert 'listen: ":8555"' in rendered
