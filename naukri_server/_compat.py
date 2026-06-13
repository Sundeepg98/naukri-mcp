"""Small Python-version compatibility shims.

`asyncio.timeout()` was added in Python 3.11. On 3.10 we fall back to the
`async-timeout` backport (already an aiohttp dependency on `python_version <
"3.11"`, so it is present whenever this fallback is needed). Both raise
`asyncio.TimeoutError` on expiry, so existing `except asyncio.TimeoutError`
handlers work unchanged on every supported version.

Usage:

    from naukri_server._compat import timeout
    async with timeout(10):
        ...
"""

import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout  # noqa: F401  (re-exported)
else:  # Python 3.10
    from async_timeout import timeout  # noqa: F401  (re-exported)
