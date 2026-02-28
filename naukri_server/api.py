"""
API helpers — aiohttp-based GET/POST for Naukri REST endpoints.
"""

import asyncio
import json

import aiohttp

from naukri_server.config import NAUKRI_BASE, API_HEADERS, API_TIMEOUT, logger
from naukri_server.browser import browser

RETRIABLE_STATUSES = {429, 502, 503, 504}


class NaukriAPIError(Exception):
    """Structured API error with status code and parsed message."""
    def __init__(self, status: int, message: str, code: str = None):
        self.status = status
        self.message = message
        self.code = code
        super().__init__(f"HTTP {status}: {message}")


def _raise_api_error(status: int, text: str):
    """Parse JSON error body and raise structured NaukriAPIError."""
    message = text[:500]
    code = None
    try:
        data = json.loads(text)
        message = data.get("message") or data.get("error", {}).get("message") or message
        code = data.get("code") or data.get("error", {}).get("code")
    except (json.JSONDecodeError, AttributeError):
        pass
    raise NaukriAPIError(status, message, code)


async def _cookie_header() -> str:
    """Build raw Cookie header string from all browser cookies."""
    try:
        cookies = await browser.context.cookies(NAUKRI_BASE)
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception:
        return ""


async def api_get(path: str, params: dict = None, _retried: bool = False) -> dict:
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
            if resp.status == 200:
                return await resp.json()
            text = await resp.text()
            if resp.status == 401 and not _retried:
                logger.info("Token expired, refreshing and retrying...")
                await browser.refresh_token()
                return await api_get(path, params, _retried=True)
            if resp.status in RETRIABLE_STATUSES and not _retried:
                logger.info("Transient error %s, retrying after 1s...", resp.status)
                await asyncio.sleep(1)
                return await api_get(path, params, _retried=True)
            _raise_api_error(resp.status, text)


async def api_post(path: str, body: dict, _retried: bool = False) -> dict:
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
            if resp.status == 200:
                return await resp.json()
            text = await resp.text()
            if resp.status == 401 and not _retried:
                logger.info("Token expired, refreshing and retrying...")
                await browser.refresh_token()
                return await api_post(path, body, _retried=True)
            if resp.status in RETRIABLE_STATUSES and not _retried:
                logger.info("Transient error %s, retrying after 1s...", resp.status)
                await asyncio.sleep(1)
                return await api_post(path, body, _retried=True)
            _raise_api_error(resp.status, text)
