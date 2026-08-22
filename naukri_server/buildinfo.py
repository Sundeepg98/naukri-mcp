"""What code THIS naukri process is running. Resolved once, at import, frozen.

A fix committed to disk changes nothing for a server that is already up, and
until now there was no way to ask a naukri server what code it holds. On
2026-08-21 that cost real time: a bug was diagnosed as a regression and an agent
was dispatched to re-fix it, and the truth was that the fix was already on disk
and the PROCESS was stale. Every check available at the time was a behavioural
fingerprint -- does this field appear, is that count right -- which cannot tell
"the code is wrong" apart from "the code is old".

TWO REPOSITORIES ARE STAMPED, AND THAT IS THE POINT. The scoring engine, the
config loader and the path renderer all live in ``jobcore``, which is
editable-installed from a SIBLING checkout that moves on its own schedule. A
stale jobcore is exactly as invisible as a stale naukri and produces the same
"the fix is on disk, why is the number wrong" hour -- so both get a stamp, named
separately, and either one can be compared against its own checkout.

THE FREEZE IS THE WHOLE POINT, NOT A PERFORMANCE NOTE. These are module
CONSTANTS, resolved at import. A per-call ``git rev-parse`` run from a stale
process reports the NEW commit sitting on disk, which is worse than reporting
nothing: it reads as confirmation that the fix is loaded, and the thing it
confirms is false. ``jobcore.buildinfo.stamp`` memoises for the same reason;
holding the result here as well makes the request path touch no git at all.
``tests/test_server_info.py::test_the_stamp_is_not_re_resolved_per_call``
enforces it by making ``subprocess.run`` raise and calling the tool twice.

HOW TO USE IT: compare ``build.code.commit`` against ``git rev-parse HEAD`` in
the naukri checkout, and ``build.jobcore.commit`` against the same in the
jobcore checkout. A mismatch means that process is stale, and no further
debugging of its behaviour is meaningful until it is restarted.

Nothing here may break server import: every git call inside jobcore is bounded
and every failure degrades to a ``source="unknown"`` stamp with a ``detail``
saying which, never to a plausible-looking hash nobody measured.
"""

from __future__ import annotations

import jobcore
from jobcore import buildinfo as _bi

from naukri_server.utils import SERVER_ROOT

__all__ = ["BUILD", "JOBCORE_BUILD", "CLOCK"]

#: The naukri checkout this process was started from.
BUILD = _bi.stamp(SERVER_ROOT)

#: The sibling jobcore checkout, which can be stale independently of naukri.
#: Anchored on ``jobcore.__file__`` rather than on a guessed sibling path, so it
#: names whichever copy was actually IMPORTED -- an editable install and a
#: vendored copy would give different answers, and the imported one is the only
#: one whose code is running.
JOBCORE_BUILD = _bi.stamp(jobcore.__file__)

#: Process identity and uptime. Deliberately NOT folded into the frozen stamps:
#: uptime is derived fresh on every read, and a cached uptime is a lie that
#: grows.
CLOCK = _bi.ProcessClock()
