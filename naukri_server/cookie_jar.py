"""Read cookie EXPIRY out of the Chrome profile without launching Chrome.

A PORT of ``linkedin/linkedin_server/cookie_jar.py``, not a reinvention: the
two rules below are the whole point of the module and both were established
there. Only the domain filter, the temp-dir prefix and this docstring differ.

WHY IT IS NEEDED HERE. Everything this server knows about the operator's
Naukri session lives in a persistent Chrome profile, and the four cookies that
matter have four different lifetimes: ``nauk_at`` is a short-lived JWT,
``nauk_rt`` is the long-lived refresh cookie that silently re-mints it,
``nauk_sid`` carries the session and ``nauk_cs`` the CSRF token. Asking
Playwright for those dates means LAUNCHING the profile: it takes the
cross-process profile lock, costs seconds, and cannot run at all while the
live server already holds that lock -- which is exactly the state the server
is in whenever the question is worth asking. The dates are already on disk.
This reads them there.

Two rules make that safe. Both are the point, not details:

* NO COOKIE VALUE IS EVER FETCHED. The query names its columns explicitly and
  the ones it names are metadata only: name, host, expiry, persistence.
  Neither the plaintext value column nor the DPAPI-sealed blob beside it is
  ever selected, and no wildcard select exists here. A value that is never
  read cannot reach a return value, a log line, or a traceback -- and a leak
  here would hand over a working Naukri login, not merely an expiry date.
* THE LIVE FILE IS NEVER OPENED. sqlite writes to a database it opens even
  when a caller only reads: it replays a hot journal, it can checkpoint a WAL,
  it takes locks. The file in question is the operator's real signed-in
  profile, which Chrome may have open right now, and whose loss cost an
  8.5-hour outage on 2026-08-22. So the jar is COPIED to a temp dir first,
  together with any journal / WAL / SHM sibling (a copy taken without its
  journal can hand back rows the journal would have rolled back, i.e. stale
  ones), the COPY is opened, and the copy is deleted in a ``finally``.
  ``sqlite3.connect`` is never handed the original path, not even read-only.

The records returned are shaped like Playwright's ``BrowserContext.cookies()``
entries -- ``{"name": ..., "expires": <posix seconds as float, or -1.0>}`` --
because that is the convention the rest of this family already speaks, with
``-1`` meaning "dies with the browser". Nothing else is included,
deliberately: a record that carries no value cannot leak one.

Chrome stores ``expires_utc`` in the WebKit epoch: MICROSECONDS since
1601-01-01 UTC, not the Unix epoch. Read as Unix microseconds it lands in the
year 2396; read as Unix seconds it lands past the year 400,000,000; read
without the offset at all it lands in 1601. Every wrong reading still looks
like a date, which is why the offset below is applied exactly, in integer
arithmetic, rather than approximated in floating point.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

#: Seconds between 1601-01-01 UTC (the WebKit epoch Chrome counts from) and
#: 1970-01-01 UTC (the POSIX epoch everything else counts from): 134774 days
#: of exactly 86400 seconds. Both epochs are proleptic and neither counts leap
#: seconds, so this is a constant, not an approximation.
WEBKIT_EPOCH_OFFSET_S = 11644473600

#: Where Chrome keeps the jar inside a user-data dir. Chrome moved it under
#: ``Network/`` in M96; the ``NetworkDataMigrated`` marker beside it is how a
#: profile records that move.
JAR_RELPATH = ("Default", "Network", "Cookies")

#: Sibling files sqlite may need in order to read the jar consistently.
#: ``-journal`` is a rollback journal; ``-wal`` and ``-shm`` appear when the
#: database is in WAL mode. Any that exist are copied alongside the jar.
JAR_SIBLING_SUFFIXES = ("-journal", "-wal", "-shm")

#: The registrable domain whose cookies this reader will report.
NAUKRI_DOMAIN = "naukri.com"

#: Metadata columns only, named one by one. This query is the enforcement
#: point for "a cookie value is never fetched" -- see the module docstring.
_JAR_QUERY = (
    "SELECT name, host_key, expires_utc, has_expires, is_persistent "
    "FROM cookies"
)


class CookieJarUnavailableError(Exception):
    """The cookie jar could not be read, and no answer is being invented.

    Raised INSTEAD of returning an empty list. An empty list because the
    profile directory is missing is indistinguishable from an empty list
    because the operator genuinely has no Naukri cookies, and the second one
    means "sign in again" while the first one means "you passed the wrong
    path" -- exactly the confusion the auth contract refuses everywhere else.

    Deliberately NOT a subclass of anything in ``browser.py``. This module is
    a plain on-disk reader with no Naukri semantics; translating a missing jar
    into whatever a tool surface should say is the caller's job.

    The message always names the path that was tried and distinguishes the
    three ways this fails: no profile directory, no jar file, or sqlite
    refusing the copy. Callers put it through ``utils.scrub_paths`` (or let
    the server-wide result scrubber do it) before it reaches a result.
    """


def _is_naukri_host(host_key: Any) -> bool:
    """Return True when a Chrome ``host_key`` belongs to Naukri.

    Chrome writes a leading dot for domain cookies (``.naukri.com``) and no
    dot for host-only ones (``www.naukri.com``). Matching is done on label
    boundaries rather than as a raw string suffix: a bare
    ``endswith("naukri.com")`` would also accept ``notnaukri.com``, which is a
    different site and can set a cookie with the same name.
    """
    if not isinstance(host_key, str):
        return False
    host = host_key.strip().lower().lstrip(".")
    return host == NAUKRI_DOMAIN or host.endswith("." + NAUKRI_DOMAIN)


def _expires_from_row(
    expires_utc: Any, has_expires: Any, is_persistent: Any
) -> float:
    """Convert one row's expiry columns into Playwright's ``expires`` field.

    Returns POSIX seconds as a float, or ``-1.0`` for a session cookie. A row
    is a session cookie when Chrome says so in EITHER flag (``has_expires`` or
    ``is_persistent`` clear) or when it carries no usable timestamp -- older
    schemas may leave the column NULL.

    The conversion splits into whole seconds and microseconds before touching
    a float, so the result is exact rather than rounded through a 17-digit
    division.
    """
    if not has_expires or not is_persistent:
        return -1.0
    try:
        raw = int(expires_utc)
    except (TypeError, ValueError):
        return -1.0
    if raw <= 0:
        return -1.0
    seconds, micros = divmod(raw, 1000000)
    posix = float(seconds - WEBKIT_EPOCH_OFFSET_S) + micros / 1000000.0
    if posix <= 0.0:
        return -1.0
    return posix


def _copy_jar(jar: Path, dest_dir: Path) -> Path:
    """Copy the jar and any journal / WAL / SHM sibling into ``dest_dir``.

    ``shutil.copy2`` reads the source and writes the destination; it never
    modifies the original, which is the entire reason this function exists.
    Siblings keep their exact names, because sqlite looks for a journal only
    at ``<db>-journal``. Missing siblings are skipped -- a profile Chrome
    closed cleanly has no journal at all.
    """
    dest = dest_dir / jar.name
    shutil.copy2(jar, dest)
    for suffix in JAR_SIBLING_SUFFIXES:
        sibling = jar.with_name(jar.name + suffix)
        if sibling.is_file():
            shutil.copy2(sibling, dest_dir / sibling.name)
    return dest


def read_jar(profile_dir: Path, names: Iterable[str]) -> list:
    """Read expiry metadata for ``names`` out of the Chrome profile cookie jar.

    Args:
        profile_dir: a Chrome user-data dir, e.g. ``chrome-profile``. The jar
            is expected at ``Default/Network/Cookies`` beneath it.
        names: the cookie names to report, e.g.
            ``["nauk_at", "nauk_rt", "nauk_sid", "nauk_cs"]``.

    Returns:
        One record per matching row, in the order the names were requested:
        ``{"name": str, "expires": float}``. ``expires`` is POSIX seconds, or
        ``-1.0`` for a session cookie. A requested name that is not in the jar
        is simply ABSENT from the list -- no placeholder record is emitted,
        because "not there" is a fact the caller already knows how to read.
        Only rows whose host key belongs to Naukri are considered.

    Raises:
        CookieJarUnavailableError: the profile directory is missing, the jar
            file is missing, or sqlite could not open or query the copy. Never
            an empty list in place of one of those.

    The live jar is never opened; see the module docstring.
    """
    wanted = []
    for name in names:
        if name not in wanted:
            wanted.append(name)

    profile_dir = Path(profile_dir)
    if not profile_dir.is_dir():
        raise CookieJarUnavailableError(
            "chrome profile directory does not exist: %s -- there is no "
            "cookie jar to read. Check the profile path, or sign in once so "
            "the profile gets created." % profile_dir
        )

    jar = profile_dir.joinpath(*JAR_RELPATH)
    if not jar.is_file():
        raise CookieJarUnavailableError(
            "cookie jar file does not exist: %s -- the profile directory %s "
            "is there but holds no cookie database yet, which is what a "
            "profile that has never been signed in to looks like."
            % (jar, profile_dir)
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="naukri-cookie-jar-"))
    try:
        try:
            copy = _copy_jar(jar, tmp_dir)
        except OSError as e:
            raise CookieJarUnavailableError(
                "sqlite could not be given a copy of the cookie jar %s: "
                "copying it to %s failed (%s). The live jar is never opened "
                "directly, so this is where the read stops."
                % (jar, tmp_dir, e)
            ) from e

        try:
            # The COPY, never ``jar``. Opened writable on purpose: sqlite may
            # need to replay the copied journal, and the copy is disposable.
            con = sqlite3.connect(str(copy))
        except sqlite3.Error as e:
            raise CookieJarUnavailableError(
                "sqlite could not open the copy of the cookie jar %s "
                "(copy at %s): %s" % (jar, copy, e)
            ) from e
        try:
            rows = con.execute(_JAR_QUERY).fetchall()
        except sqlite3.Error as e:
            raise CookieJarUnavailableError(
                "sqlite could not query the copy of the cookie jar %s "
                "(copy at %s): %s. The file exists but does not look like a "
                "Chrome cookie database." % (jar, copy, e)
            ) from e
        finally:
            con.close()
    finally:
        # Windows will not delete a file sqlite still has open, hence the
        # close above; ignore_errors keeps a cleanup problem from masking an
        # otherwise successful read.
        shutil.rmtree(tmp_dir, ignore_errors=True)

    found = {}
    for name, host_key, expires_utc, has_expires, is_persistent in rows:
        if name not in wanted:
            continue
        if not _is_naukri_host(host_key):
            continue
        found.setdefault(name, []).append(
            _expires_from_row(expires_utc, has_expires, is_persistent)
        )

    out = []
    for name in wanted:
        for expires in found.get(name, ()):
            out.append({"name": name, "expires": expires})
    return out
