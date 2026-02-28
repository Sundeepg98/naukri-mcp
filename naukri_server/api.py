"""
API helpers — aiohttp-based GET/POST for Naukri REST endpoints.
"""

import aiohttp

from naukri_server.config import NAUKRI_BASE, API_HEADERS, API_TIMEOUT, logger
from naukri_server.browser import browser


async def _cookie_header() -> str:
    """Build raw Cookie header string from all browser cookies."""
    try:
        cookies = await browser.context.cookies(NAUKRI_BASE)
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception:
        return ""


async def api_get(path: str, params: dict = None) -> dict:
    """GET request to Naukri API (aiohttp — for non-reCAPTCHA endpoints)."""
    token = await browser.ensure_token()
    cookie_str = await _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    url = f"{NAUKRI_BASE}{path}" if path.startswith("/") else path
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    logger.info("API GET %s", url)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        trust_env=True,
    ) as session:
        async with session.get(url, headers=headers) as resp:
            logger.info("API GET %s -> %s", path, resp.status)
            if resp.status == 401:
                raise ValueError("Token expired — call naukri_login to re-authenticate")
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
            return await resp.json()


async def api_post(path: str, body: dict) -> dict:
    """POST request to Naukri API (aiohttp — for non-reCAPTCHA endpoints)."""
    token = await browser.ensure_token()
    cookie_str = await _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    url = f"{NAUKRI_BASE}{path}" if path.startswith("/") else path
    logger.info("API POST %s", url)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        trust_env=True,
    ) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            logger.info("API POST %s -> %s", path, resp.status)
            if resp.status == 401:
                raise ValueError("Token expired — call naukri_login to re-authenticate")
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
            return await resp.json()
