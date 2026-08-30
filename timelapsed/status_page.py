"""The system status page.

Its own page, like people-and-plates, and for the same reason: it answers a
different question from the timeline. The timeline asks "what happened"; this
asks "is the thing that records what happened still working". Those want
different layouts, and the viewer's header has room for a number, not a report.

Everything on it comes from one request to `/api/system`, so the page is a
renderer and nothing else -- the arithmetic all lives in `system_status.py`,
where it can be tested without a browser.

Standard library only, like web.py and library_page.py: no build step, no
framework. The markup lives in templates/status.html.
"""

from timelapsed.pages import load_page

STATUS_TEMPLATE = load_page("status.html")


def render_status() -> bytes:
    """The status page, as bytes. No per-request state: it fetches everything."""
    return STATUS_TEMPLATE.encode()
