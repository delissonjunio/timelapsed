"""Shared plumbing for the HTML pages.

Each page's markup lives under templates/ as a real .html file, where an
editor treats it as HTML instead of as the inside of a Python string.
load_page reads one and stitches in the fragments every page shares -- the
favicon, the palette and resets in templates/base.css, the helpers in
templates/shared.js -- so each of those exists exactly once. The stitching
happens in Python, at import time: every page is still served whole in a
single request, there is still no build step, and the standard library is
still the only dependency.
"""
from importlib.resources import files

FAVICON = (
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjA"
    "gMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNyIgZmlsbD0iIzEyMTUxZCIvPjxyZWN0IHdp"
    "ZHRoPSIzMiIgaGVpZ2h0PSIzMiIgcng9IjciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzI0MmEzNyIvPjxyZWN0IHg9IjYiI"
    "Hk9IjgiIHdpZHRoPSIyMCIgaGVpZ2h0PSI0IiByeD0iMiIgZmlsbD0iIzM0ZDM5OSIvPjxyZWN0IHg9IjYiIHk9IjE0Ii"
    "B3aWR0aD0iMTMiIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiNhNzhiZmEiLz48cmVjdCB4PSI2IiB5PSIyMCIgd2lkdGg"
    "9IjciIGhlaWdodD0iNCIgcng9IjIiIGZpbGw9IiM1YjlkZmYiLz48cmVjdCB4PSIyNyIgeT0iNiIgd2lkdGg9IjEiIGhl"
    "aWdodD0iMjAiIGZpbGw9IiNmZjZiNmIiLz48L3N2Zz4="
)

_TEMPLATES = files("timelapsed") / "templates"


def _read(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def load_page(name: str) -> str:
    """A template with the shared fragments stitched in, ready to serve.

    The required placeholders are checked rather than assumed: a .replace on
    text that is not there is silent, and a page quietly served without its
    stylesheet tokens would render as a wall of default-styled text.
    """
    page = _read(name)
    for placeholder, value, required in (
        ("/*__BASE_CSS__*/", _read("base.css"), True),
        ("//__SHARED_JS__", _read("shared.js"), False),
        ("__FAVICON__", FAVICON, True),
    ):
        if required and placeholder not in page:
            raise ValueError(f"{name} has no {placeholder} placeholder")
        page = page.replace(placeholder, value)
    return page
