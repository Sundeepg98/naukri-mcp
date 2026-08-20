"""Shared utility functions."""

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path, PureWindowsPath


import functools
import logging as _logging

_wd_logger = _logging.getLogger("naukri")


SERVER_ROOT = Path(__file__).resolve().parent.parent
"""The naukri/ directory. Paths under it are returned RELATIVE to it."""

# The repo root, as a pattern that tolerates either separator and any casing --
# the same path arrives as "D:\...", "D:/..." and "d:\..." depending on
# whether it came from pathlib, a URL-ish string, or a Windows API error.
_ROOT_PARTS = list(SERVER_ROOT.parts)
if _ROOT_PARTS:
    _ROOT_PARTS[0] = _ROOT_PARTS[0].rstrip("\\/")
_ROOT_RE = re.compile(
    r"[\\/]+".join(re.escape(part) for part in _ROOT_PARTS) + r"[\\/]*",
    re.IGNORECASE,
)

# Any Windows drive-letter path. POSIX absolute paths are deliberately NOT
# matched generically: every Naukri API path in these responses starts with a
# slash ("/jobapi/v3/search"), as does the path component of every URL, and a
# scrubber that ate those would do far more damage than the leak it fixes.
# The negative lookbehind is load-bearing: without it "https://host/x" matches
# as drive "s:", and the scrubber turns every URL in every response into
# "httpx". A drive letter is a SINGLE letter, so anything alphanumeric before it
# means this is a URL scheme, not a path.
_DRIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"<>|,;)\]}]*")

_SCRUB_MAX_DEPTH = 12


def scrub_paths(text):
    """Remove absolute local filesystem paths from a string.

    Two rules, in order:

    1. A path INSIDE the server root loses the root and keeps the rest, so
       "<root>/exports/apps.json" becomes "exports/apps.json" -- still enough to
       find the file, with the machine layout gone.
    2. Anything still absolute is outside the repo, where not even the directory
       is the caller's business, so only the basename survives.

    Non-strings are returned untouched, so this is safe to map over a result.
    """
    if not isinstance(text, str):
        return text
    out = _ROOT_RE.sub("", text)
    return _DRIVE_PATH_RE.sub(
        lambda m: PureWindowsPath(m.group(0)).name or m.group(0), out
    )


def scrub_result(value, _depth: int = 0):
    """Apply :func:`scrub_paths` to every string in a tool result.

    Exists because the leak cannot be held shut site by site. Six of the twenty
    known emission sites put a path in a field deliberately; the other fourteen
    got one from `f"...{e}"`, and OSError and Playwright both embed the filename
    they failed on. Every future `except Exception as e:` re-opens that class,
    so the guarantee has to live at the boundary, not at the sites.
    """
    if _depth > _SCRUB_MAX_DEPTH:
        return value
    if isinstance(value, str):
        return scrub_paths(value)
    if isinstance(value, dict):
        return {
            scrub_result(k, _depth + 1): scrub_result(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_result(v, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_result(v, _depth + 1) for v in value)
    return value


def tool_watchdog(timeout: float = None, name: str = None):
    """Last-resort per-tool-call budget.

    A tool that never returns is worse than a tool that fails: the MCP client
    waits out its own timeout, the operator sees nothing, and (as on 2026-08-20)
    the stall can be holding a shared lock the rest of the server needs. This
    decorator converts "never" into the server's ordinary error envelope,
    ``{"status": "error", "error_code": "TIMEOUT"}``, which every caller already
    knows how to read.

    It is a BACKSTOP, not a policy: the default budget
    (``TOOL_WATCHDOG_TIMEOUT``, 600s) sits far above any legitimate tool -
    auto_hunt and batch_apply legitimately run for minutes - because its job is
    to bound a PERMANENT wedge, not to police slow work. The real fixes live at
    the awaits themselves (see naukri_server/browser.py).

    Deliberately NOT caught: ordinary exceptions. A tool that fails must keep
    failing with its own error, or the watchdog would mask real bugs as
    timeouts.
    """
    from naukri_server.config import TOOL_WATCHDOG_TIMEOUT

    budget = TOOL_WATCHDOG_TIMEOUT if timeout is None else timeout

    def decorate(fn):
        label = name or getattr(fn, "__name__", "tool")

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return scrub_result(
                    await asyncio.wait_for(fn(*args, **kwargs), timeout=budget)
                )
            except asyncio.TimeoutError:
                _wd_logger.error(
                    "Tool %s exceeded its %.0fs watchdog budget and was cancelled - "
                    "returning a TIMEOUT envelope instead of hanging the client.",
                    label, budget,
                )
                return {
                    "status": "error",
                    "error_code": "TIMEOUT",
                    "message": (
                        f"{label} did not complete within {budget:.0f}s and was "
                        "cancelled. The browser or an upstream API is likely "
                        "unresponsive; try naukri_health_check."
                    ),
                }

        wrapper.__wrapped_by_watchdog__ = True
        return wrapper

    return decorate


def derive_slug(company_name: str) -> str:
    """Derive an AmbitionBox-style URL slug from a company name.

    Strips common suffixes (Pvt. Ltd., Inc., etc.) and normalizes to lowercase
    hyphenated format.
    """
    name = company_name.strip()
    for suffix in ("Pvt. Ltd.", "Pvt Ltd", "Private Limited", "Ltd.", "Ltd",
                   "Limited", "Inc.", "Inc", "Corp.", "Corp", "Corporation",
                   "LLP", "LLC", "Technologies", "Technology", "Solutions",
                   "Services", "India"):
        if name.lower().endswith(suffix.lower()):
            name = name[:len(name) - len(suffix)].strip()
            break  # Only strip one suffix (fixes research.py bug)
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    slug = re.sub(r'-+', '-', slug)
    return slug


def load_json_with_backup(path: Path, logger) -> list:
    """Load JSON file with .backup fallback recovery."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            backup = path.with_suffix(".backup")
            if backup.exists():
                try:
                    logger.warning("Primary %s corrupted, recovering from backup", path.name)
                    return json.loads(backup.read_text(encoding="utf-8"))
                except Exception:
                    pass
            logger.critical("BOTH primary and backup corrupted for %s — returning empty list", path.name)
            return []
    return []


def save_json_atomic(path: Path, data, logger):
    """Atomic JSON write with .backup creation."""
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if path.exists():
        backup = path.with_suffix(".backup")
        shutil.copy2(str(path), str(backup))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


class TtlCache:
    """Simple async TTL cache for a single value.

    Usage:
        cache = TtlCache(ttl=30)
        data = await cache.get(fetch_fn)  # calls fetch_fn() on cache miss
        cache.invalidate()                # force next call to refetch
    """
    def __init__(self, ttl: float):
        self._ttl = ttl
        self._data = None
        self._ts = 0.0
        self._lock = asyncio.Lock()

    async def get(self, fetch_fn):
        """Return cached data or call fetch_fn() if stale."""
        now = time.time()
        if self._data is not None and (now - self._ts) < self._ttl:
            return self._data
        async with self._lock:
            now = time.time()
            if self._data is not None and (now - self._ts) < self._ttl:
                return self._data
            self._data = await fetch_fn()
            self._ts = time.time()
            return self._data

    def invalidate(self):
        """Clear cached data."""
        self._data = None
        self._ts = 0.0
