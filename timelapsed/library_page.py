"""The people-and-plates page.

Its own module, and its own page, because it answers a different question from
the timeline. The timeline asks "what happened in this window"; this asks "who
is in the library, and when did they appear". Those want different layouts, and
wedging the second into the viewer's sidebar made both worse.

The two pages meet at a link: a sighting here opens the viewer at
`/?channel=..&at=..`, which seeks the covering clip to that moment.

Standard library only, like web.py: no build step, no framework. The markup
lives in templates/library.html.
"""

from timelapsed.pages import load_page

LIBRARY_TEMPLATE = load_page("library.html")


def render_library() -> bytes:
    """The page is static; every list it shows is fetched from the API.

    Unlike the timeline, nothing is server-rendered into it: the library is
    unbounded and paged through by the user, so there is no small catalogue
    worth embedding the way the viewer embeds its clips.
    """
    return LIBRARY_TEMPLATE.encode()
