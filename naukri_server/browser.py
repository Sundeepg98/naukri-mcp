"""
Browser state — Playwright browser for login and cookie extraction.
"""

import asyncio
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page

from naukri_server.config import CHROME_PROFILE, NAUKRI_BASE, NAV_TIMEOUT, ELEMENT_TIMEOUT, logger


class NaukriBrowser:
    """Playwright browser for login + cookie extraction. Lives entire MCP session."""

    def __init__(self):
        self.pw = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.token: Optional[str] = None  # nauk_at JWT
        self._lock = asyncio.Lock()

    async def start(self):
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self._extract_token()
        logger.info("Browser started, token: %s", "found" if self.token else "none")

    async def stop(self):
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
        logger.info("Browser stopped")

    async def _extract_token(self) -> Optional[str]:
        """Extract nauk_at JWT from browser cookies."""
        try:
            cookies = await self.context.cookies(NAUKRI_BASE)
            for c in cookies:
                if c["name"] == "nauk_at":
                    self.token = c["value"]
                    return self.token
        except Exception:
            pass
        self.token = None
        return None

    async def ensure_token(self) -> str:
        """Get fresh token, raising if not logged in."""
        await self._extract_token()
        if not self.token:
            raise ValueError("Not logged in — call naukri_login first")
        return self.token

    async def goto(self, url: str, wait: str = "domcontentloaded") -> None:
        try:
            await self.page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT)
        except Exception:
            await self.page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT)

    async def text(self, selector: str) -> Optional[str]:
        el = await self.page.query_selector(selector)
        return (await el.text_content()).strip() if el else None

    async def get_profile_name(self) -> str:
        """Get profile name via profile API (same path as naukri_get_profile)."""
        from naukri_server.api import api_get

        try:
            data = await api_get(
                "/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self",
                {"expand_level": "4"},
            )
            profiles = data.get("profile") or []
            name = profiles[0].get("name") if profiles and isinstance(profiles[0], dict) else None
            if name:
                logger.info("Profile name from API: %s", name)
                return name
        except Exception as e:
            logger.warning("Profile name API failed: %s: %s", type(e).__name__, e)
        return "unknown"

    async def exists(self, selector: str) -> bool:
        return await self.page.query_selector(selector) is not None

    async def safe_fill(self, selector: str, value: str, delay: int = 30):
        el = await self.page.wait_for_selector(selector, timeout=ELEMENT_TIMEOUT)
        await el.click()
        await el.fill("")
        await el.type(value, delay=delay)


browser = NaukriBrowser()
