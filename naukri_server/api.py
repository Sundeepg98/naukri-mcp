"""
API helpers — aiohttp-based GET/POST/PUT/DELETE for Naukri REST endpoints.
"""

import asyncio
import json

import aiohttp

from naukri_server.config import NAUKRI_BASE, API_HEADERS, API_TIMEOUT, logger
from naukri_server.browser import browser

RETRIABLE_STATUSES = {429, 502, 503, 504}
SUCCESS_STATUSES = {200, 201, 202, 204}
MAX_RETRIES = 2
BACKOFF_BASE = 1.0


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


async def api_get(path: str, params: dict = None, extra_headers: dict = None, _attempt: int = 0) -> dict:
    """GET request to Naukri API (aiohttp — for non-reCAPTCHA endpoints)."""
    token = await browser.ensure_token()
    cookie_str = await _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    if extra_headers:
        headers.update(extra_headers)
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
            if resp.status in SUCCESS_STATUSES:
                if resp.status == 204:
                    return {}
                return await resp.json()
            text = await resp.text()
            if resp.status == 401 and _attempt == 0:
                logger.info("Token expired, refreshing and retrying...")
                try:
                    await browser.refresh_token()
                except Exception as e:
                    logger.error("Token refresh failed: %s: %s", type(e).__name__, e)
                    raise NaukriAPIError(401, f"Session expired and token refresh failed: {e}. Call naukri_login to re-authenticate.")
                return await api_get(path, params, extra_headers=extra_headers, _attempt=1)
            if resp.status in RETRIABLE_STATUSES and _attempt < MAX_RETRIES:
                delay = BACKOFF_BASE * (2 ** _attempt)
                logger.info("Transient error %s, retrying after %.1fs...", resp.status, delay)
                await asyncio.sleep(delay)
                return await api_get(path, params, extra_headers=extra_headers, _attempt=_attempt + 1)
            _raise_api_error(resp.status, text)


async def api_post(path: str, body: dict, _attempt: int = 0) -> dict:
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
            if resp.status in SUCCESS_STATUSES:
                if resp.status == 204:
                    return {}
                return await resp.json()
            text = await resp.text()
            if resp.status == 401 and _attempt == 0:
                logger.info("Token expired, refreshing and retrying...")
                try:
                    await browser.refresh_token()
                except Exception as e:
                    logger.error("Token refresh failed: %s: %s", type(e).__name__, e)
                    raise NaukriAPIError(401, f"Session expired and token refresh failed: {e}. Call naukri_login to re-authenticate.")
                return await api_post(path, body, _attempt=1)
            if resp.status in RETRIABLE_STATUSES and _attempt < MAX_RETRIES:
                delay = BACKOFF_BASE * (2 ** _attempt)
                logger.info("Transient error %s, retrying after %.1fs...", resp.status, delay)
                await asyncio.sleep(delay)
                return await api_post(path, body, _attempt=_attempt + 1)
            _raise_api_error(resp.status, text)


async def api_put(path: str, body: dict, _attempt: int = 0) -> dict:
    """PUT request to Naukri API (aiohttp — for non-reCAPTCHA endpoints)."""
    token = await browser.ensure_token()
    cookie_str = await _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    url = f"{NAUKRI_BASE}{path}" if path.startswith("/") else path
    logger.info("API PUT %s", url)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        trust_env=True,
    ) as session:
        async with session.put(url, headers=headers, json=body) as resp:
            logger.info("API PUT %s -> %s", path, resp.status)
            if resp.status in SUCCESS_STATUSES:
                if resp.status == 204:
                    return {}
                return await resp.json()
            text = await resp.text()
            if resp.status == 401 and _attempt == 0:
                logger.info("Token expired, refreshing and retrying...")
                try:
                    await browser.refresh_token()
                except Exception as e:
                    logger.error("Token refresh failed: %s: %s", type(e).__name__, e)
                    raise NaukriAPIError(401, f"Session expired and token refresh failed: {e}. Call naukri_login to re-authenticate.")
                return await api_put(path, body, _attempt=1)
            if resp.status in RETRIABLE_STATUSES and _attempt < MAX_RETRIES:
                delay = BACKOFF_BASE * (2 ** _attempt)
                logger.info("Transient error %s, retrying after %.1fs...", resp.status, delay)
                await asyncio.sleep(delay)
                return await api_put(path, body, _attempt=_attempt + 1)
            _raise_api_error(resp.status, text)


async def api_delete(path: str, body: dict = None, _attempt: int = 0) -> dict:
    """DELETE request to Naukri API (aiohttp — for non-reCAPTCHA endpoints)."""
    token = await browser.ensure_token()
    cookie_str = await _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    url = f"{NAUKRI_BASE}{path}" if path.startswith("/") else path
    logger.info("API DELETE %s", url)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        trust_env=True,
    ) as session:
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        async with session.delete(url, **kwargs) as resp:
            logger.info("API DELETE %s -> %s", path, resp.status)
            if resp.status in SUCCESS_STATUSES:
                if resp.status == 204:
                    return {}
                return await resp.json()
            text = await resp.text()
            if resp.status == 401 and _attempt == 0:
                logger.info("Token expired, refreshing and retrying...")
                try:
                    await browser.refresh_token()
                except Exception as e:
                    logger.error("Token refresh failed: %s: %s", type(e).__name__, e)
                    raise NaukriAPIError(401, f"Session expired and token refresh failed: {e}. Call naukri_login to re-authenticate.")
                return await api_delete(path, body, _attempt=1)
            if resp.status in RETRIABLE_STATUSES and _attempt < MAX_RETRIES:
                delay = BACKOFF_BASE * (2 ** _attempt)
                logger.info("Transient error %s, retrying after %.1fs...", resp.status, delay)
                await asyncio.sleep(delay)
                return await api_delete(path, body, _attempt=_attempt + 1)
            _raise_api_error(resp.status, text)
