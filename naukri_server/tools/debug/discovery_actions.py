"""Discovery debug actions: discover, fetch_all_statuses, intercept_requests, click_discover."""

import asyncio

from naukri_server.browser import page_goto


def _truncate_body(body, max_depth=2, max_items=5, max_str=200):
    """Truncate a JSON body for preview -- show structure, not full data."""
    if isinstance(body, dict):
        if max_depth <= 0:
            return f"{{...{len(body)} keys}}"
        return {k: _truncate_body(v, max_depth - 1, max_items, max_str)
                for i, (k, v) in enumerate(body.items()) if i < max_items * 2}
    if isinstance(body, list):
        count = len(body)
        if count == 0:
            return []
        preview = [_truncate_body(body[0], max_depth - 1, max_items, max_str)]
        if count > 1:
            preview.append(f"...+{count - 1} more")
        return preview
    if isinstance(body, str) and len(body) > max_str:
        return body[:max_str] + "..."
    return body


async def do_discover(page, url: str) -> dict:
    """Navigate to applied/saved jobs pages, capture all 200 JSON responses."""
    from naukri_server.config import APPLIED_JOBS_PAGE, SAVED_JOBS_PAGE

    pages = {
        "applied_jobs": APPLIED_JOBS_PAGE,
        "saved_jobs": SAVED_JOBS_PAGE,
    }
    # Allow targeting a specific page or custom URL
    if url:
        if url in pages:
            pages = {url: pages[url]}
        elif url.startswith("http"):
            pages = {"custom": url}

    all_discovered = {}

    for page_name, page_url in pages.items():
        captured_responses = []

        async def on_response(response, _captures=captured_responses):
            content_type = response.headers.get("content-type", "")
            if response.status == 200 and "json" in content_type:
                try:
                    body = await response.json()
                    entry = {
                        "url": response.url,
                        "method": response.request.method,
                        "status": response.status,
                        "body_keys": list(body.keys()) if isinstance(body, dict) else f"[list of {len(body)}]",
                        "body_preview": _truncate_body(body),
                    }
                    # Capture POST request body if present
                    if response.request.method == "POST" and response.request.post_data:
                        entry["request_body"] = response.request.post_data[:2000]
                    _captures.append(entry)
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            await page_goto(page, page_url)
            await asyncio.sleep(5)
            # Scroll to trigger lazy-loaded API calls
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)
        finally:
            page.remove_listener("response", on_response)

        all_discovered[page_name] = {
            "page_url": page_url,
            "final_url": page.url,
            "api_calls_count": len(captured_responses),
            "api_calls": captured_responses,
        }

    return {"status": "ok", "discovered": all_discovered}


async def do_fetch_all_statuses(page, url: str) -> dict:
    """Like discover but captures ALL HTTP statuses (not just 200)."""
    captured_responses = []

    async def on_any_response(response, _captures=captured_responses):
        content_type = response.headers.get("content-type", "")
        if "json" in content_type or "javascript" in content_type:
            try:
                text = await response.text()
                body = None
                try:
                    import json as _json
                    body = _json.loads(text)
                except Exception:
                    body = text[:500]
                _captures.append({
                    "url": response.url,
                    "method": response.request.method,
                    "status": response.status,
                    "content_type": content_type,
                    "request_headers": {k: v for k, v in response.request.headers.items()
                                        if k.lower() in ("appid", "systemid", "clientid", "authorization", "content-type")},
                    "body_preview": _truncate_body(body) if isinstance(body, (dict, list)) else str(body)[:500],
                })
            except Exception as ex:
                _captures.append({
                    "url": response.url,
                    "status": response.status,
                    "error": str(ex),
                })

    target_url = url or page.url
    page.on("response", on_any_response)
    try:
        await page_goto(page, target_url)
        await asyncio.sleep(8)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)
    finally:
        page.remove_listener("response", on_any_response)

    return {
        "status": "ok",
        "target_url": target_url,
        "final_url": page.url,
        "total_captured": len(captured_responses),
        "responses": captured_responses,
    }


async def do_intercept_requests(page, url: str) -> dict:
    """Capture outgoing POST request bodies (method, url, post_data)."""
    captured_requests = []

    async def on_request(request, _captures=captured_requests):
        if request.method == "POST" and "json" in (request.headers.get("content-type", "") or ""):
            try:
                _captures.append({
                    "url": request.url,
                    "method": request.method,
                    "post_data": request.post_data[:3000] if request.post_data else None,
                    "headers": {k: v for k, v in request.headers.items()
                                if k.lower() in ("content-type", "appid", "systemid", "authorization")},
                })
            except Exception:
                pass

    page.on("request", on_request)
    try:
        nav_url = url or page.url
        await page_goto(page, nav_url)
        await asyncio.sleep(5)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)
    finally:
        page.remove_listener("request", on_request)

    return {
        "status": "ok",
        "intercepted_requests_count": len(captured_requests),
        "intercepted_requests": captured_requests,
    }


async def do_click_discover(page, url: str) -> dict:
    """Click a CSS selector then capture resulting API calls; url format "CSS_SELECTOR|OPTIONAL_NAV_URL"."""
    parts = (url or "").split("|", 1)
    selector = parts[0] if parts else ""
    nav_url = parts[1] if len(parts) > 1 else None

    if nav_url:
        await page_goto(page, nav_url)
        await asyncio.sleep(3)

    captured_responses = []

    async def on_response(response, _captures=captured_responses):
        content_type = response.headers.get("content-type", "")
        if response.status == 200 and "json" in content_type:
            try:
                body = await response.json()
                entry = {
                    "url": response.url,
                    "method": response.request.method,
                    "status": response.status,
                    "body_keys": list(body.keys()) if isinstance(body, dict) else f"[list of {len(body)}]",
                    "body_preview": _truncate_body(body, max_depth=3, max_items=10),
                }
                # Capture POST request body if present
                if response.request.method == "POST" and response.request.post_data:
                    entry["request_body"] = response.request.post_data[:2000]
                _captures.append(entry)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        if selector:
            await page.click(selector, timeout=5000)
        await asyncio.sleep(5)
    finally:
        page.remove_listener("response", on_response)

    return {
        "status": "ok",
        "selector": selector,
        "api_calls_count": len(captured_responses),
        "api_calls": captured_responses,
    }
