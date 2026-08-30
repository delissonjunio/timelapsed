"""The live wall: every camera at once, as close to now as remuxing allows.

Its own module and its own page for the same reason the library is: it answers
a different question. The timeline asks "what happened"; this asks "what is
happening". The tiles are real video, not polled stills -- go2rtc pulls each
camera's RTSP main stream on demand and remuxes it (no transcode) to whatever
the browser will take, WebRTC or MSE. The NVR encodes HEVC, which every Apple
device here decodes in hardware; Firefox is the one likely casualty.

The page stays in the house style: standard library only, no build step. The
markup lives in templates/live.html. The player element is go2rtc's own
`video-stream.js`, served by go2rtc itself and reverse-proxied to the same
origin as this page under GO2RTC_PATH -- see the matching location in
deploy/nginx-timelapsed.conf, installed by deploy/go2rtc-setup.sh.
tests/test_nginx_config.py holds the two sides of that path together.

Streams are named `ch{channel id}` in /etc/go2rtc.yaml -- `ch1` for the
default NVR, `chintelbras-3` for a named one -- and go2rtc-setup.sh renders
them (via timelapsed.go2rtc_config) from the same [nvr]/[nvr.*] sections this
page's channel list comes from, so the two lists cannot drift apart on an
installed guest.
"""

import json

from timelapsed.pages import load_page

GO2RTC_PATH = "/go2rtc/"

LIVE_TEMPLATE = load_page("live.html").replace("__GO2RTC_PATH__", GO2RTC_PATH)


def render_live(channels: list[str], recognition_enabled: bool = False) -> bytes:
    """The live wall with the channel list embedded, one request like the rest."""

    def block(element_id: str, value: object) -> str:
        payload = json.dumps(value, separators=(",", ":")).replace("</", "<\\/")
        return f'<script type="application/json" id="{element_id}">{payload}</script>'

    page = LIVE_TEMPLATE.replace(
        '<script type="module">',
        block("channels-payload", list(channels))
        + "\n"
        + block("recognition-payload", recognition_enabled)
        + '\n<script type="module">',
    )
    return page.encode()
