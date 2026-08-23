"""`naukri_debug`'s documented argument format must match what it actually parses.

Every test is PURE: no network, no browser, no file I/O.

The dispatcher docstring is the ONLY specification an AI caller sees -- the
tool takes a bare `url: str` and packs two values into it separated by "|",
so the docstring is load-bearing in a way an ordinary signature is not. Until
2026-08-23 it advertised `discover_click` as "PAGE_URL|CSS_SELECTOR" while
`discovery_actions.do_click_discover` parsed "CSS_SELECTOR|NAV_URL". A caller
who believed the docs handed a URL where a selector was expected, and the
click could only ever fail.

So this file pins BOTH ends and their agreement: the parser's real order
(behaviourally, not by reading it) and the docstring that describes it.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class _FakePage:
    """Minimal page double recording what the action did to it."""

    def __init__(self):
        self.url = "https://www.naukri.com/already-here"
        self.clicked = None
        self.listeners = []
        self.click = AsyncMock(side_effect=self._record_click)
        self.evaluate = AsyncMock(return_value=None)

    async def _record_click(self, selector, timeout=None):
        self.clicked = selector

    def on(self, event, handler):
        self.listeners.append((event, handler))

    def remove_listener(self, event, handler):
        self.listeners.remove((event, handler))


class TestTheParserTakesTheSelectorFirst:

    @pytest.mark.asyncio
    async def test_selector_is_part_zero_and_nav_url_is_part_one(self):
        from naukri_server.tools.debug.discovery_actions import do_click_discover

        page = _FakePage()
        navigated = []

        async def _goto(p, url):
            navigated.append(url)

        with patch("naukri_server.tools.debug.discovery_actions.page_goto", new=_goto), \
                patch("naukri_server.tools.debug.discovery_actions.asyncio.sleep", new=AsyncMock()):
            result = await do_click_discover(page, "button.apply-cta|https://www.naukri.com/jobs")

        assert page.clicked == "button.apply-cta", (
            "part 0 must be the CSS selector; got %r" % page.clicked
        )
        assert navigated == ["https://www.naukri.com/jobs"], (
            "part 1 must be the nav URL; got %r" % navigated
        )
        assert result["selector"] == "button.apply-cta"

    @pytest.mark.asyncio
    async def test_the_nav_url_half_is_optional(self):
        """A bare selector clicks on whatever page the tab already shows."""
        from naukri_server.tools.debug.discovery_actions import do_click_discover

        page = _FakePage()
        navigated = []

        async def _goto(p, url):
            navigated.append(url)

        with patch("naukri_server.tools.debug.discovery_actions.page_goto", new=_goto), \
                patch("naukri_server.tools.debug.discovery_actions.asyncio.sleep", new=AsyncMock()):
            await do_click_discover(page, "a.jobTuple")

        assert page.clicked == "a.jobTuple"
        assert navigated == [], "a one-part argument must not navigate anywhere"


class TestTheDocstringDescribesThatOrder:

    def test_dispatcher_documents_selector_first(self):
        from naukri_server.tools.debug import naukri_debug

        doc = naukri_debug.__doc__ or ""
        assert "CSS_SELECTOR|" in doc, (
            "naukri_debug no longer documents discover_click as selector-first"
        )

    def test_dispatcher_does_not_document_the_reversed_order(self):
        """The exact needle. This is the string that was wrong for months."""
        from naukri_server.tools.debug import naukri_debug

        doc = naukri_debug.__doc__ or ""
        # The docstring is allowed to NAME the old wrong format in the
        # historical note that explains it. What must never come back is
        # documenting it as THE format, on a `discover_click:` line.
        for line in doc.splitlines():
            if "discover_click:" in line:
                assert "PAGE_URL|CSS_SELECTOR" not in line, (
                    "discover_click is documented backwards again: %s" % line.strip()
                )

    def test_the_two_docstrings_agree_with_each_other(self):
        """The handler's own docstring and the dispatcher's must not diverge.

        The handler's was right the whole time; the dispatcher's was wrong.
        Nothing was checking that they said the same thing.
        """
        from naukri_server.tools.debug import naukri_debug
        from naukri_server.tools.debug.discovery_actions import do_click_discover

        handler_doc = do_click_discover.__doc__ or ""
        assert "CSS_SELECTOR|" in handler_doc
        # The dispatcher mentions discover_click twice -- once in the action
        # list, once in Args. Exactly the Args one states a format.
        format_lines = [
            line for line in (naukri_debug.__doc__ or "").splitlines()
            if "discover_click:" in line and "|" in line
        ]
        assert len(format_lines) == 1, format_lines
        assert "CSS_SELECTOR|" in format_lines[0]
