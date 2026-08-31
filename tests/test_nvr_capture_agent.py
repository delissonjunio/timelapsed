import pytest
import requests
import requests.auth

from timelapsed.nvr_capture_agent import DEFAULT_TIMEOUT_SECONDS, NVRCaptureAgent
from timelapsed.schema import VideoResolution


class FakeResponse:
    def __init__(self, content=b"\xff\xd8\xff", content_type="image/jpeg", status_code=200):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


@pytest.fixture
def agent():
    return NVRCaptureAgent("http://nvr.local/", "admin", "pw")


@pytest.fixture
def captured_calls(monkeypatch):
    """Record every requests.get call and return a scripted sequence of responses."""
    calls = []

    def install(*responses):
        queue = list(responses)

        def fake_get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            result = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(requests, "get", fake_get)
        return calls

    return install


def test_strips_a_trailing_slash_from_the_base_url(agent):
    assert agent.url == "http://nvr.local"


def test_builds_the_isapi_snapshot_url(agent):
    url = agent._snapshot_url("3", VideoResolution(1280, 720))

    assert url == (
        "http://nvr.local/ISAPI/Streaming/channels/301/picture"
        "?videoResolutionWidth=1280&videoResolutionHeight=720"
    )


def test_capture_returns_bytes_and_extension(agent, captured_calls):
    captured_calls(FakeResponse(content=b"jpegdata"))

    content, extension = agent.capture_image("1")

    assert (content, extension) == (b"jpegdata", "jpg")


def test_capture_always_passes_a_timeout(agent, captured_calls):
    calls = captured_calls(FakeResponse())

    agent.capture_image("1")

    assert calls[0]["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_capture_uses_digest_authentication(agent, captured_calls):
    calls = captured_calls(FakeResponse())

    agent.capture_image("1")

    auth = calls[0]["auth"]
    assert isinstance(auth, requests.auth.HTTPDigestAuth)
    assert (auth.username, auth.password) == ("admin", "pw")


def test_capture_uses_the_explicit_resolution_over_the_default(agent, captured_calls):
    calls = captured_calls(FakeResponse())

    agent.capture_image("2", VideoResolution(640, 480))

    assert "videoResolutionWidth=640&videoResolutionHeight=480" in calls[0]["url"]


def test_capture_falls_back_to_the_default_resolution(captured_calls):
    agent = NVRCaptureAgent("http://nvr.local", "u", "p", default_resolution=VideoResolution(800, 600))
    calls = captured_calls(FakeResponse())

    agent.capture_image("1")

    assert "videoResolutionWidth=800&videoResolutionHeight=600" in calls[0]["url"]


def test_png_responses_get_the_right_extension(agent, captured_calls):
    captured_calls(FakeResponse(content_type="image/png"))

    assert agent.capture_image("1")[1] == "png"


def test_content_type_parameters_are_ignored(agent, captured_calls):
    captured_calls(FakeResponse(content_type="image/jpeg; charset=binary"))

    assert agent.capture_image("1")[1] == "jpg"


def test_a_non_image_response_is_rejected(agent, captured_calls):
    """An NVR that answers 200 with an XML error must not be written to disk as a frame."""
    captured_calls(FakeResponse(content=b"<ResponseStatus/>", content_type="application/xml"))

    with pytest.raises(ValueError, match="expected an image"):
        agent.capture_image("1")


def test_http_errors_are_retried_then_raised(agent, captured_calls):
    calls = captured_calls(FakeResponse(status_code=500))

    with pytest.raises(requests.exceptions.HTTPError):
        agent.capture_image("1")

    assert len(calls) > 1  # backoff actually retried


def test_a_transient_failure_is_retried_and_then_succeeds(agent, captured_calls):
    calls = captured_calls(requests.exceptions.ConnectTimeout("boom"), FakeResponse(content=b"ok"))

    content, _ = agent.capture_image("1")

    assert content == b"ok"
    assert len(calls) == 2


def test_the_password_is_not_logged(caplog):
    with caplog.at_level("DEBUG"):
        NVRCaptureAgent("http://nvr.local", "admin", "sup3rs3cret")

    assert "sup3rs3cret" not in caplog.text
