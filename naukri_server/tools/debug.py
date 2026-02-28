import asyncio
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser


def _truncate_body(body, max_depth=2, max_items=5, max_str=200):
    """Truncate a JSON body for preview — show structure, not full data."""
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


# ============================================================================
# Tool 7: Debug (Playwright)
# ============================================================================


@mcp.tool()
async def naukri_debug(action: str = "snapshot", url: Optional[str] = None) -> dict:
    """Debug tool: capture current page state for troubleshooting.

    Args:
        action: "snapshot" -- DOM structure | "screenshot" -- saves debug.png |
                "scan" -- deep scan for specific elements (chatbot, neo, agent, iframe) |
                "discover" -- intercept JSON API calls on applied/saved jobs pages
        url: Optional URL to navigate to before performing the action.
             For "discover", can be a page key ("applied_jobs" / "saved_jobs") or a custom URL.

    Returns:
        - {status: "ok", url, title, ...action-specific data...}
        - {status: "error", message}
    """
    async with browser._lock:
        if url:
            await browser.goto(url)
            await asyncio.sleep(3)
        url = browser.page.url
        title = await browser.page.title()

        if action == "screenshot":
            path = str(Path(__file__).parent.parent.parent / "debug.png")
            await browser.page.screenshot(path=path, full_page=False)
            return {"status": "ok", "url": url, "title": title, "screenshot": path}

        if action == "scan":
            # Deep scan for chatbot, neo, agent, iframe, widget elements
            scan = await browser.page.evaluate("""() => {
                const keywords = ['neo', 'chat', 'bot', 'agent', 'widget', 'assist', 'copilot'];
                const results = { iframes: [], matching_elements: [], floating_buttons: [] };

                // Check iframes
                document.querySelectorAll('iframe').forEach(f => {
                    results.iframes.push({
                        src: f.src || f.getAttribute('data-src') || '',
                        id: f.id || null,
                        class: f.className || null,
                    });
                });

                // Scan all elements for keyword matches in class/id/data attributes
                document.querySelectorAll('*').forEach(el => {
                    const attrs = (el.className || '') + ' ' + (el.id || '') +
                        ' ' + Array.from(el.attributes).map(a => a.name + '=' + a.value).join(' ');
                    const lower = attrs.toLowerCase();
                    for (const kw of keywords) {
                        if (lower.includes(kw)) {
                            results.matching_elements.push({
                                tag: el.tagName.toLowerCase(),
                                id: el.id || null,
                                class: (el.className || '').toString().slice(0, 120),
                                text: (el.textContent || '').trim().slice(0, 80),
                                attr_match: kw,
                            });
                            break;
                        }
                    }
                });

                // Fixed/absolute positioned elements (floating buttons/widgets)
                document.querySelectorAll('*').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if ((style.position === 'fixed' || style.position === 'sticky') &&
                        el.offsetWidth > 20 && el.offsetHeight > 20 &&
                        el.tagName !== 'NAV' && el.tagName !== 'HEADER') {
                        results.floating_buttons.push({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            class: (el.className || '').toString().slice(0, 120),
                            text: (el.textContent || '').trim().slice(0, 80),
                            rect: el.getBoundingClientRect(),
                        });
                    }
                });

                // Deduplicate matching elements (keep first 20)
                results.matching_elements = results.matching_elements.slice(0, 20);
                results.floating_buttons = results.floating_buttons.slice(0, 10);
                return results;
            }""")
            return {"status": "ok", "url": url, "title": title, "scan": scan}

        if action == "deepscan":
            # Scroll to bottom to trigger lazy loads, then wait
            await browser.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(5)
            await browser.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(2)

            result = await browser.page.evaluate("""() => {
                const out = {
                    high_zindex: [],
                    shadow_hosts: [],
                    chat_scripts: [],
                    bottom_right: [],
                    all_iframes: [],
                    custom_elements: [],
                };

                // ALL elements with z-index > 999 (chatbots use very high z-index)
                document.querySelectorAll('*').forEach(el => {
                    const z = parseInt(window.getComputedStyle(el).zIndex);
                    if (z > 999) {
                        out.high_zindex.push({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            class: (el.className || '').toString().slice(0, 150),
                            z_index: z,
                            visible: el.offsetWidth > 0 && el.offsetHeight > 0,
                            text: (el.textContent || '').trim().slice(0, 100),
                        });
                    }
                });

                // Shadow DOM hosts
                document.querySelectorAll('*').forEach(el => {
                    if (el.shadowRoot) {
                        const inner = el.shadowRoot.innerHTML || '';
                        out.shadow_hosts.push({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            class: (el.className || '').toString().slice(0, 100),
                            inner_length: inner.length,
                            inner_preview: inner.slice(0, 200),
                        });
                    }
                });

                // Script tags with chat/bot/neo/widget/agent in src
                document.querySelectorAll('script[src]').forEach(s => {
                    const src = s.src.toLowerCase();
                    if (/chat|bot|neo|widget|agent|assist|helpdesk|intercom|crisp|drift|freshchat|zendesk|tawk/.test(src)) {
                        out.chat_scripts.push(s.src);
                    }
                });

                // ALL elements in bottom-right quadrant with fixed/absolute position
                document.querySelectorAll('*').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'absolute') {
                        const rect = el.getBoundingClientRect();
                        if (rect.right > window.innerWidth - 200 && rect.bottom > window.innerHeight - 200 &&
                            el.offsetWidth > 10 && el.offsetHeight > 10) {
                            out.bottom_right.push({
                                tag: el.tagName.toLowerCase(),
                                id: el.id || null,
                                class: (el.className || '').toString().slice(0, 150),
                                rect: {x: Math.round(rect.x), y: Math.round(rect.y),
                                       w: Math.round(rect.width), h: Math.round(rect.height)},
                                text: (el.textContent || '').trim().slice(0, 80),
                            });
                        }
                    }
                });

                // All iframes (including dynamically created)
                document.querySelectorAll('iframe').forEach(f => {
                    out.all_iframes.push({
                        src: f.src || f.getAttribute('data-src') || '(empty)',
                        id: f.id || null,
                        class: f.className || null,
                        visible: f.offsetWidth > 0 && f.offsetHeight > 0,
                        rect: f.getBoundingClientRect(),
                    });
                });

                // Custom HTML elements (web components)
                document.querySelectorAll('*').forEach(el => {
                    if (el.tagName.includes('-')) {
                        out.custom_elements.push({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            class: (el.className || '').toString().slice(0, 100),
                            visible: el.offsetWidth > 0 && el.offsetHeight > 0,
                        });
                    }
                });

                // Deduplicate
                out.high_zindex = out.high_zindex.slice(0, 15);
                out.bottom_right = out.bottom_right.slice(0, 10);
                out.custom_elements = [...new Map(out.custom_elements.map(e => [e.tag, e])).values()].slice(0, 10);
                return out;
            }""")
            return {"status": "ok", "url": url, "title": title, "deepscan": result}

        if action == "discover":
            # Discover API endpoints by intercepting all JSON responses on a page
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
                            _captures.append({
                                "url": response.url,
                                "method": response.request.method,
                                "status": response.status,
                                "body_keys": list(body.keys()) if isinstance(body, dict) else f"[list of {len(body)}]",
                                "body_preview": _truncate_body(body),
                            })
                        except Exception:
                            pass

                browser.page.on("response", on_response)
                try:
                    await browser.goto(page_url)
                    await asyncio.sleep(5)
                    # Scroll to trigger lazy-loaded API calls
                    await browser.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(3)
                finally:
                    browser.page.remove_listener("response", on_response)

                all_discovered[page_name] = {
                    "page_url": page_url,
                    "final_url": browser.page.url,
                    "api_calls_count": len(captured_responses),
                    "api_calls": captured_responses,
                }

            return {"status": "ok", "discovered": all_discovered}

        structure = await browser.page.evaluate("""() => {
            const selectors = [
                '[class*="tuple"]', '[class*="jobCard"]', '[data-job-id]',
                '[class*="title"]', '[class*="comp"]', '[class*="salary"]',
                '[class*="location"]', '[class*="apply"]', 'button',
            ];
            const results = {};
            for (const sel of selectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0 && els.length < 50) {
                        results[sel] = Array.from(els).slice(0, 5).map(el => ({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            class: el.className ? el.className.toString().slice(0, 100) : null,
                            text: el.textContent ? el.textContent.trim().slice(0, 80) : null,
                        }));
                    }
                } catch(e) {}
            }
            return results;
        }""")

        return {"status": "ok", "url": url, "title": title, "dom_structure": structure}
