"""Renders /etc/go2rtc.yaml from the NVR sections of /etc/timelapsed.ini.

Called by deploy/go2rtc-setup.sh, which used to render this itself in awk. It
moved here when a second NVR could exist: the stream list now spans sections
and two RTSP URL shapes, and the section parsing already lives in
`timelapsed.config`. Stream names are `ch{channel id}` -- `ch1` for the default
NVR's channel 1, `ch<name>-<n>` for a named one -- and the /live page derives
the same names from the same config, so the two cannot drift apart.

Usage (stdout is the rendered file):

    CONFIG_PATH=/etc/timelapsed.ini python3 -m timelapsed.go2rtc_config
"""
import os
import sys
from urllib.parse import quote, urlparse

from timelapsed.config import CONFIG_PATHS, get_config
from timelapsed.schema import Config, NVRConfig

RTSP_PORT = 554


def stream_source(nvr: NVRConfig, device_channel: str) -> str:
    """The RTSP URL for one camera's main stream, in the device's own dialect.

    Credentials go into the URL, so anything URL-significant in them is
    percent-encoded or the device sees a mangled username.
    """
    auth = f"{quote(nvr.username, safe='')}:{quote(nvr.password, safe='')}"
    host = urlparse(nvr.url).hostname or nvr.url
    if nvr.kind == "dahua":
        return f"rtsp://{auth}@{host}:{RTSP_PORT}/cam/realmonitor?channel={device_channel}&subtype=0"
    # Channel N's main stream is N01, same mapping as the snapshot URL.
    return f"rtsp://{auth}@{host}:{RTSP_PORT}/Streaming/Channels/{device_channel}01"


def render(config: Config) -> str:
    """The whole go2rtc.yaml.

    The api listens on localhost only: nginx proxies it under /go2rtc/ and
    Tailscale is the authentication, exactly as for the viewer. WebRTC's media
    port has to be reachable directly -- the browser connects to it after the
    proxied signalling -- so it binds wide.
    """
    lines = [
        "# Rendered by deploy/go2rtc-setup.sh from /etc/timelapsed.ini -- edit that, not this.",
        "api:",
        '  listen: "127.0.0.1:1984"',
        "",
        "rtsp:",
        "  # Local restream, for checking a camera with ffprobe from the guest.",
        '  listen: "127.0.0.1:8554"',
        "",
        "webrtc:",
        '  listen: ":8555"',
        "",
        "streams:",
    ]
    for nvr in config.nvrs:
        for device_channel in nvr.device_channels:
            name = f"ch{nvr.channel_id(device_channel)}"
            lines.append(f'  {name}: "{stream_source(nvr, device_channel)}"')
    return "\n".join(lines) + "\n"


def main() -> None:
    config_path = os.environ.get("CONFIG_PATH")
    config = get_config((config_path,) if config_path else CONFIG_PATHS)
    sys.stdout.write(render(config))


if __name__ == "__main__":
    main()
