"""Automated API endpoint discovery -- weekly config audit."""

import asyncio
import re

import naukri_server.config as config
from naukri_server.health import health_probe, ProbeResult


@health_probe(
    name="discovery.config_audit",
    interval=604800,  # 1 week
    criticality="informational",
    description="Discover new API endpoints by navigating key Naukri pages"
)
async def discover_config_audit() -> ProbeResult:
    """Navigate key pages, capture network traffic, diff against known endpoints."""

    # 1. Get all known *_API constants from config
    known_urls = set()
    for name in dir(config):
        if name.endswith("_API"):
            val = getattr(config, name, "")
            if isinstance(val, str) and val.startswith("/"):
                known_urls.add(val)

    # 2. Navigate key pages and capture API calls
    from naukri_server.browser import browser, page_goto
    discovered_urls = set()
    pages_to_probe = [
        "https://www.naukri.com/mnjuser/homepage",
        "https://www.naukri.com/mnjuser/profile",
    ]

    async with browser.page_pool.acquire() as page:
        for page_url in pages_to_probe:
            captured = []

            async def on_response(response):
                if response.status == 200 and "naukri.com" in response.url:
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct or "javascript" in ct:
                            # Extract path from full URL
                            from urllib.parse import urlparse
                            parsed = urlparse(response.url)
                            path = parsed.path
                            if path and not path.endswith((".js", ".css", ".png", ".gif")):
                                captured.append(path)
                    except Exception:
                        pass

            page.on("response", on_response)
            try:
                await page_goto(page, page_url)
                await asyncio.sleep(3)  # Wait for lazy-loaded API calls
            finally:
                page.remove_listener("response", on_response)

            discovered_urls.update(captured)

    # 3. Filter to API paths only (cloudgateway, jobapi, servicegateway, etc.)
    api_patterns = re.compile(r"/(cloudgateway|jobapi|servicegateway|recommendapi|alertapi|central-login)")
    api_urls = {u for u in discovered_urls if api_patterns.search(u)}

    # 4. Diff against known
    # Normalize: strip query params for comparison
    def normalize(url):
        return url.split("?")[0]

    known_normalized = {normalize(u) for u in known_urls}
    new_urls = {u for u in api_urls if normalize(u) not in known_normalized}

    # 5. Store results + emit events
    if new_urls:
        try:
            from naukri_server.events import event_bus, NewEndpointDiscovered
            from naukri_server.database import log_endpoint_audit
            for url in new_urls:
                await log_endpoint_audit(url, "GET", "NEW", "auto_discovery")
                await event_bus.emit(NewEndpointDiscovered(url=url, method="GET", page_source="auto_discovery"))
        except Exception:
            pass

    return ProbeResult(
        status="healthy",
        message=f"Discovered {len(api_urls)} API endpoints. {len(new_urls)} new.",
        metadata={
            "discovered_count": len(api_urls),
            "config_count": len(known_urls),
            "new_count": len(new_urls),
            "new_endpoints": sorted(list(new_urls))[:10],
        }
    )
