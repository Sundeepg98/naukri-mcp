"""The scrub guarantee is server-wide, or it is not a guarantee.

`tests/test_path_leaks.py` establishes the property for TOOLS: no result may
carry an absolute local filesystem path, enforced at the registration boundary
rather than site by site, because the EXCEPTION class re-opens every time
somebody writes `except Exception as e: ... {e}`.

MEASURED 2026-08-21: 120 of 120 tools were scrubbed. 5 resources and 7 prompts
were NOT. `naukri_server/__init__.py` replaced `mcp.tool` with
`_watchdogged_tool`, and `utils.scrub_result` rode along inside that wrapper --
so `mcp.resource` and `mcp.prompt`, never patched, registered their handlers
raw.

That is a live hole of exactly the class the original census named.
`resources/handlers.py` formats every failure as
`f"Failed to load profile: {exc}"`, and OSError and Playwright errors embed the
absolute filename they failed on -- so the leak needed no new code to appear,
only a disk error on a machine whose layout is `D:\\workspace\\projects\\...`.

Two further asymmetries the tool wrapper did not cover, both tested here:

* the tool wrapper only wraps COROUTINES (`_inspect.iscoroutinefunction`), and
  all 7 prompt handlers are SYNC. A wrapper that keeps that check would leave
  every prompt unscrubbed while reporting itself as applied.
* a handler added TOMORROW must not be able to opt out silently, so the census
  test walks the live registries rather than a hand-written list.

All tests are PURE -- no network, no browser, no file I/O.
"""

import json

import pytest

from tests.test_path_leaks import DRIVE_PATH, assert_no_absolute_path

# The exact string an OSError produces on this machine, which is the EXCEPTION
# class the census named and the one the resource handlers format verbatim.
LEAKY_OSERROR = OSError(
    "[Errno 2] No such file or directory: "
    "'D:\\workspace\\projects\\job-hunting\\mcp-servers\\naukri\\sync_state.json'"
)


async def _read_resource_raw(uri: str) -> str:
    """Read a resource through the REGISTERED path and return its raw text.

    Deliberately not `json.loads`-ed first: the assertion is about the bytes a
    client actually receives, and JSON escaping (`D:\\\\workspace`) still contains
    the drive letter the regex looks for.
    """
    from naukri_server import mcp

    resource = await mcp._resource_manager.get_resource(uri)
    assert resource is not None, "Resource %s not registered" % uri
    raw = await resource.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return raw


class TestResourceResultsAreScrubbed:

    async def test_a_resource_that_formats_an_oserror_does_not_leak_the_path(
            self, monkeypatch):
        """The named site: `f"Failed to load profile: {exc}"` with a real OSError.

        The handler CATCHES and formats, which is why nothing here raises --
        the path arrives as an ordinary success-shaped error dict, invisible to
        anything that only inspects exceptions.
        """
        import naukri_server.resources.handlers as handlers

        async def _boom():
            raise LEAKY_OSERROR

        monkeypatch.setattr(handlers, "get_cached_profile", _boom)

        raw = await _read_resource_raw("naukri://profile")

        assert not DRIVE_PATH.search(raw), (
            "resource result leaks an absolute path: %r" % raw
        )
        payload = json.loads(raw)
        assert_no_absolute_path(payload, "naukri://profile")
        # Still an ANSWER, not a redaction: the useful part must survive.
        assert payload["error_code"] == "API_ERROR"
        assert "No such file or directory" in payload["message"]
        assert "sync_state.json" in payload["message"]

    async def test_a_second_resource_is_covered_too(self, monkeypatch):
        """Coverage is per-registration, not per-handler-someone-remembered."""
        import naukri_server.resources.handlers as handlers

        async def _boom():
            raise LEAKY_OSERROR

        monkeypatch.setattr(handlers, "_get_settings", _boom)

        raw = await _read_resource_raw("naukri://settings")
        assert not DRIVE_PATH.search(raw), (
            "resource result leaks an absolute path: %r" % raw
        )
        assert "settings" in json.loads(raw)["message"].lower()

    async def test_a_clean_resource_is_not_reshaped(self, monkeypatch):
        """The control: scrubbing must not rewrite an ordinary payload."""
        import naukri_server.resources.handlers as handlers

        async def _ok():
            return {"status": "success", "views": 3, "ok": True, "none": None}

        monkeypatch.setattr(handlers, "get_cached_dashboard", _ok)

        payload = json.loads(await _read_resource_raw("naukri://dashboard"))
        assert payload == {"status": "success", "views": 3, "ok": True,
                           "none": None}


class TestPromptResultsAreScrubbed:
    """Prompts are SYNC, which is the half the tool wrapper structurally missed.

    Registered on the live `mcp` and popped again in `finally`, so this
    exercises the real patched `mcp.prompt` rather than a rebuilt stand-in --
    the thing under test IS the registration path.
    """

    async def _register_and_render(self, fn, name):
        from naukri_server import mcp

        mcp.prompt(name=name, description="scrub probe")(fn)
        try:
            return await mcp._prompt_manager.render_prompt(name)
        finally:
            mcp._prompt_manager._prompts.pop(name, None)

    async def test_a_sync_prompt_handler_does_not_leak_a_path(self):
        def leaky_prompt() -> list:
            return [{"role": "user",
                     "content": "could not read %s" % LEAKY_OSERROR}]

        messages = await self._register_and_render(
            leaky_prompt, "naukri/_scrub_probe_sync")

        text = " ".join(getattr(m.content, "text", "") for m in messages)
        assert not DRIVE_PATH.search(text), (
            "prompt result leaks an absolute path: %r" % text
        )
        assert "sync_state.json" in text, "scrubbed into uselessness"

    async def test_an_async_prompt_handler_does_not_leak_a_path(self):
        """Async prompts are legal even though naukri ships none today."""
        async def leaky_prompt_async() -> list:
            return [{"role": "user",
                     "content": "could not read %s" % LEAKY_OSERROR}]

        messages = await self._register_and_render(
            leaky_prompt_async, "naukri/_scrub_probe_async")

        text = " ".join(getattr(m.content, "text", "") for m in messages)
        assert not DRIVE_PATH.search(text)
        assert "sync_state.json" in text

    async def test_a_prompt_argument_survives_and_the_schema_is_preserved(self):
        """The wrapper must not eat parameters.

        FastMCP builds a prompt's argument schema from the handler signature;
        a wrapper that presented `(*args, **kwargs)` would publish a prompt
        with NO arguments and silently drop everything the caller passed.
        """
        from naukri_server import mcp

        def probe(company: str, role: str = "dev") -> list:
            return [{"role": "user", "content": "%s/%s" % (company, role)}]

        mcp.prompt(name="naukri/_scrub_probe_args", description="d")(probe)
        try:
            prompt = mcp._prompt_manager._prompts["naukri/_scrub_probe_args"]
            args = {a.name: a.required for a in prompt.arguments}
            assert args == {"company": True, "role": False}

            messages = await mcp._prompt_manager.render_prompt(
                "naukri/_scrub_probe_args", arguments={"company": "Acme"})
            assert "Acme/dev" in messages[0].content.text
        finally:
            mcp._prompt_manager._prompts.pop("naukri/_scrub_probe_args", None)


class TestEveryHandlerIsWrappedNotJustTheOnesWeChecked:
    """The census. A handler added later must not be able to opt out silently.

    Walks the LIVE registries, so the assertion cannot go stale the way a
    hand-maintained list of names does.
    """

    def test_every_registered_resource_is_wrapped(self):
        from naukri_server import mcp

        registered = mcp._resource_manager._resources
        assert registered, "no resources registered at all"

        unwrapped = [
            uri for uri, res in registered.items()
            if not getattr(res.fn, "__scrubbed_result__", False)
        ]
        assert not unwrapped, (
            "%d of %d resources bypass the scrubber: %s"
            % (len(unwrapped), len(registered), unwrapped)
        )

    def test_every_registered_prompt_is_wrapped(self):
        from naukri_server import mcp

        registered = mcp._prompt_manager._prompts
        assert registered, "no prompts registered at all"

        # `Prompt.from_function` re-wraps the handler in pydantic's
        # `validate_call`, which copies `__dict__` across, so the marker is
        # visible on `.fn` directly; `__wrapped__` is checked as a fallback in
        # case that copying behaviour ever changes.
        unwrapped = []
        for name, prompt in registered.items():
            fn = prompt.fn
            marked = (getattr(fn, "__scrubbed_result__", False)
                      or getattr(getattr(fn, "__wrapped__", None),
                                 "__scrubbed_result__", False))
            if not marked:
                unwrapped.append(name)
        assert not unwrapped, (
            "%d of %d prompts bypass the scrubber: %s"
            % (len(unwrapped), len(registered), unwrapped)
        )

    def test_the_census_counts_the_whole_surface(self):
        """Guards the guard: an empty registry would pass the two tests above
        vacuously if the `assert registered` lines were ever dropped."""
        from naukri_server import mcp

        assert len(mcp._resource_manager._resources) >= 5
        assert len(mcp._prompt_manager._prompts) >= 7
        assert len(mcp._tool_manager._tools) >= 100
