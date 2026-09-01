"""The page loader: shared fragments stitched into each template.

The navbar is the interesting one. It is the same markup on every page, and
the loader is what turns it into *this* page's navbar by lighting the link
that leads here -- so that is what these check, page by page, along with the
loader's refusal to serve a page that has nowhere to put it.
"""

import re
from pathlib import Path

import pytest

from timelapsed import pages
from timelapsed.pages import load_page

PAGES = {"index.html": "/", "live.html": "/live", "library.html": "/library", "status.html": "/status"}


def navbar(page: str) -> str:
    """The <nav> block alone: the stylesheet mentions aria-current too."""
    (nav,) = re.findall(r"<nav[^>]*>.*?</nav>", page, re.S)
    return nav


@pytest.mark.parametrize(("name", "href"), PAGES.items())
def test_each_page_lights_its_own_link_and_no_other(name, href):
    nav = navbar(load_page(name))

    assert nav.count('aria-current="page"') == 1
    assert f'href="{href}" aria-current="page"' in nav


@pytest.mark.parametrize("name", PAGES)
def test_every_page_carries_the_same_links(name):
    nav = navbar(load_page(name))

    for href in PAGES.values():
        assert f'href="{href}"' in nav


@pytest.mark.parametrize("name", PAGES)
def test_the_markers_do_not_reach_the_browser(name):
    assert "data-page=" not in load_page(name)


def test_a_page_the_navbar_has_no_link_for_is_refused(tmp_path: Path, monkeypatch):
    templates = pages._TEMPLATES  # pyright: ignore[reportPrivateUsage]
    for fragment in ("base.css", "shared.js", "nav.html"):
        (tmp_path / fragment).write_text((templates / fragment).read_text(encoding="utf-8"))
    (tmp_path / "orphan.html").write_text("/*__BASE_CSS__*/ __FAVICON__ <!--__NAV__-->")
    monkeypatch.setattr(pages, "_TEMPLATES", tmp_path)

    with pytest.raises(ValueError, match="no link for orphan"):
        load_page("orphan.html")


def test_a_page_without_a_navbar_slot_is_refused(tmp_path: Path, monkeypatch):
    templates = pages._TEMPLATES  # pyright: ignore[reportPrivateUsage]
    for fragment in ("base.css", "shared.js", "nav.html"):
        (tmp_path / fragment).write_text((templates / fragment).read_text(encoding="utf-8"))
    (tmp_path / "index.html").write_text("/*__BASE_CSS__*/ __FAVICON__")
    monkeypatch.setattr(pages, "_TEMPLATES", tmp_path)

    with pytest.raises(ValueError, match="__NAV__"):
        load_page("index.html")
