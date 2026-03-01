"""Browser-based debug actions: snapshot, screenshot, scan, deepscan, explore, notif_explore."""

import asyncio
from pathlib import Path
from typing import Optional


async def do_snapshot(page, current_url: str, title: str) -> dict:
    """Default action: DOM structure of the current page."""
    structure = await page.evaluate("""() => {
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

    return {"status": "ok", "url": current_url, "title": title, "dom_structure": structure}


async def do_screenshot(page, current_url: str, title: str) -> dict:
    """Save a PNG screenshot to debug.png in the project root."""
    path = str(Path(__file__).parent.parent.parent.parent / "debug.png")
    await page.screenshot(path=path, full_page=False)
    return {"status": "ok", "url": current_url, "title": title, "screenshot": path}


async def do_scan(page, current_url: str, title: str) -> dict:
    """Deep scan for chatbot, neo, agent, iframe, widget elements."""
    scan = await page.evaluate("""() => {
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
    return {"status": "ok", "url": current_url, "title": title, "scan": scan}


async def do_deepscan(page, current_url: str, title: str) -> dict:
    """Thorough scan: high z-index, shadow DOM, web components, bottom-right floaters."""
    # Scroll to bottom to trigger lazy loads, then wait
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(5)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(2)

    result = await page.evaluate("""() => {
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
    return {"status": "ok", "url": current_url, "title": title, "deepscan": result}


async def do_explore(page, current_url: str, title: str) -> dict:
    """Comprehensive DOM exploration: forms, buttons, scripts, data attrs, embedded JSON."""
    result = await page.evaluate(r"""() => {
        const out = {
            page_text_preview: '',
            forms: [],
            buttons: [],
            links_with_actions: [],
            data_attributes: [],
            inline_scripts_summary: [],
            embedded_json: [],
            api_urls_in_dom: [],
            onclick_handlers: [],
            angular_react_hints: [],
            select_dropdowns: [],
            input_fields: [],
            alert_cards: [],
            all_classes_summary: [],
            custom_elements: [],
            naukri_widget_content: null,
        };

        // 1. Full page text preview (first 3000 chars of body)
        out.page_text_preview = (document.body?.innerText || '').slice(0, 3000);

        // 2. All forms
        document.querySelectorAll('form').forEach(f => {
            out.forms.push({
                action: f.action || '(none)',
                method: f.method || 'GET',
                id: f.id || null,
                class: f.className || null,
                inputs: Array.from(f.querySelectorAll('input,select,textarea')).map(i => ({
                    tag: i.tagName.toLowerCase(),
                    type: i.type || null,
                    name: i.name || null,
                    id: i.id || null,
                    value: (i.value || '').slice(0, 100),
                    placeholder: i.placeholder || null,
                })),
            });
        });

        // 3. All buttons (not just in forms)
        document.querySelectorAll('button, [role="button"], a[class*="btn"], input[type="submit"], input[type="button"]').forEach(b => {
            out.buttons.push({
                tag: b.tagName.toLowerCase(),
                text: (b.textContent || '').trim().slice(0, 100),
                id: b.id || null,
                class: (b.className || '').toString().slice(0, 150),
                type: b.type || null,
                href: b.href || null,
                onclick: b.getAttribute('onclick') ? b.getAttribute('onclick').slice(0, 200) : null,
                data_attrs: Array.from(b.attributes)
                    .filter(a => a.name.startsWith('data-'))
                    .map(a => ({name: a.name, value: a.value.slice(0, 100)})),
                ng_click: b.getAttribute('ng-click') || b.getAttribute('(click)') || null,
            });
        });

        // 4. Elements with onclick handlers
        document.querySelectorAll('[onclick]').forEach(el => {
            out.onclick_handlers.push({
                tag: el.tagName.toLowerCase(),
                text: (el.textContent || '').trim().slice(0, 80),
                onclick: el.getAttribute('onclick').slice(0, 300),
            });
        });

        // 5. Links with interesting hrefs (api, action, delete, edit, create, alert)
        document.querySelectorAll('a[href]').forEach(a => {
            const href = a.href.toLowerCase();
            if (/alert|create|edit|delete|api|action|manage|setting|frequency/.test(href)) {
                out.links_with_actions.push({
                    text: (a.textContent || '').trim().slice(0, 80),
                    href: a.href,
                    class: (a.className || '').toString().slice(0, 100),
                });
            }
        });

        // 6. Data attributes on all elements (sample)
        const dataEls = document.querySelectorAll('[data-url], [data-api], [data-action], [data-id], [data-alert], [data-type]');
        dataEls.forEach(el => {
            const attrs = {};
            Array.from(el.attributes).filter(a => a.name.startsWith('data-')).forEach(a => {
                attrs[a.name] = a.value.slice(0, 200);
            });
            out.data_attributes.push({
                tag: el.tagName.toLowerCase(),
                text: (el.textContent || '').trim().slice(0, 60),
                class: (el.className || '').toString().slice(0, 100),
                attrs: attrs,
            });
        });

        // 7. Inline script content — look for API URLs, JSON data, config objects
        document.querySelectorAll('script:not([src])').forEach(s => {
            const text = s.textContent || '';
            if (text.length > 10 && text.length < 50000) {
                // Look for API URLs
                const apiMatches = text.match(/['"`](\/(?:cloudgateway|api|v\d|mnjuser|jobAlert|alert)[^'"`\s]{5,200})['"`]/gi) || [];
                // Look for fetch/XHR patterns
                const fetchMatches = text.match(/(?:fetch|XMLHttpRequest|axios|ajax|\$\.(?:get|post|ajax))\s*\([^)]{0,300}\)/gi) || [];
                // Look for JSON assignments
                const jsonMatches = text.match(/(?:window\.__\w+__|window\.\w+State|window\.\w+Config|__NEXT_DATA__|__INITIAL_STATE__)\s*=/gi) || [];

                if (apiMatches.length > 0 || fetchMatches.length > 0 || jsonMatches.length > 0 || text.includes('alert')) {
                    out.inline_scripts_summary.push({
                        length: text.length,
                        preview: text.slice(0, 500),
                        api_urls: apiMatches.slice(0, 10),
                        fetch_patterns: fetchMatches.slice(0, 5),
                        json_assignments: jsonMatches.slice(0, 5),
                    });
                }
            }
        });

        // 8. Embedded JSON (script type="application/json" or similar)
        document.querySelectorAll('script[type*="json"], script[type*="ld+json"]').forEach(s => {
            try {
                const data = JSON.parse(s.textContent);
                out.embedded_json.push({
                    type: s.type,
                    keys: Object.keys(data).slice(0, 20),
                    preview: JSON.stringify(data).slice(0, 500),
                });
            } catch(e) {}
        });

        // 9. Search all text content for API URL patterns
        const bodyHtml = document.body?.innerHTML || '';
        const apiUrlPattern = /https?:\/\/[^"'\s<>]*(?:api|cloudgateway|jobalert|alert|v\d)[^"'\s<>]*/gi;
        const apiUrls = [...new Set((bodyHtml.match(apiUrlPattern) || []).slice(0, 20))];
        out.api_urls_in_dom = apiUrls;

        // 10. Angular/React hints
        const angularEls = document.querySelectorAll('[ng-app], [ng-controller], [ng-click], [data-ng-app]');
        angularEls.forEach(el => {
            out.angular_react_hints.push({
                tag: el.tagName.toLowerCase(),
                ng_app: el.getAttribute('ng-app') || null,
                ng_controller: el.getAttribute('ng-controller') || null,
                ng_click: el.getAttribute('ng-click') || null,
            });
        });
        // React root
        const reactRoot = document.querySelector('#__next, #root, [data-reactroot]');
        if (reactRoot) {
            out.angular_react_hints.push({type: 'react_root', id: reactRoot.id, class: (reactRoot.className || '').toString().slice(0, 100)});
        }

        // 11. Select dropdowns
        document.querySelectorAll('select').forEach(s => {
            out.select_dropdowns.push({
                name: s.name || null,
                id: s.id || null,
                class: (s.className || '').toString().slice(0, 100),
                options: Array.from(s.options).slice(0, 10).map(o => ({value: o.value, text: o.text})),
            });
        });

        // 12. Input fields (outside forms too)
        document.querySelectorAll('input:not(form input), textarea:not(form textarea)').forEach(i => {
            out.input_fields.push({
                type: i.type || null,
                name: i.name || null,
                id: i.id || null,
                placeholder: i.placeholder || null,
                value: (i.value || '').slice(0, 100),
                class: (i.className || '').toString().slice(0, 100),
            });
        });

        // 13. Look for alert-card-like structures (divs with "alert" in class/id)
        document.querySelectorAll('[class*="alert" i], [class*="Alert" i], [id*="alert" i], [class*="job-alert" i]').forEach(el => {
            out.alert_cards.push({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                class: (el.className || '').toString().slice(0, 200),
                text: (el.textContent || '').trim().slice(0, 300),
                children_count: el.children.length,
                html_preview: el.innerHTML.slice(0, 500),
            });
        });

        // 14. Summary of unique class names containing interesting keywords
        const classSet = new Set();
        document.querySelectorAll('*').forEach(el => {
            const cls = (el.className || '').toString();
            if (/alert|create|edit|delete|manage|frequency|toggle|switch|card|list|item|row/i.test(cls)) {
                classSet.add(cls.slice(0, 150));
            }
        });
        out.all_classes_summary = [...classSet].slice(0, 30);

        // 15. Custom HTML elements (web components)
        const customSet = new Set();
        document.querySelectorAll('*').forEach(el => {
            if (el.tagName.includes('-')) customSet.add(el.tagName.toLowerCase());
        });
        out.custom_elements = [...customSet].slice(0, 20);

        // 16. Check window for global state
        const globals = [];
        for (const key of Object.keys(window)) {
            if (/state|config|data|alert|naukri|init/i.test(key) && typeof window[key] === 'object' && window[key] !== null) {
                try {
                    globals.push({key: key, type: typeof window[key], preview: JSON.stringify(window[key]).slice(0, 300)});
                } catch(e) {
                    globals.push({key: key, type: typeof window[key], preview: '(circular/non-serializable)'});
                }
            }
        }
        out.window_globals = globals.slice(0, 15);

        // 17. naukri-widget content
        const widget = document.querySelector('naukri-widget, [class*="naukri-widget"], #naukri-widget');
        if (widget) {
            out.naukri_widget_content = {
                tag: widget.tagName.toLowerCase(),
                html_preview: widget.innerHTML.slice(0, 2000),
                has_shadow: !!widget.shadowRoot,
            };
            if (widget.shadowRoot) {
                out.naukri_widget_content.shadow_html = widget.shadowRoot.innerHTML.slice(0, 2000);
            }
        }

        return out;
    }""")
    return {"status": "ok", "url": current_url, "title": title, "explore": result}


async def do_notif_explore(page, current_url: str, title: str) -> dict:
    """Targeted notification page exploration (NEXT_DATA, scripts, DOM tree)."""
    # Scroll to trigger lazy loading, wait for dynamic content
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(3)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(2)

    result = await page.evaluate(r"""() => {
        const out = {};

        // 1. __NEXT_DATA__
        const nd = document.getElementById('__NEXT_DATA__');
        if (nd) {
            try {
                const parsed = JSON.parse(nd.textContent);
                out.__NEXT_DATA__ = {
                    page: parsed.page,
                    buildId: parsed.buildId,
                    props_keys: Object.keys(parsed.props || {}),
                    pageProps_keys: Object.keys(parsed.props?.pageProps || {}),
                    pageProps_preview: JSON.stringify(parsed.props?.pageProps || {}).slice(0, 1000),
                };
            } catch(e) { out.__NEXT_DATA__ = 'parse_error: ' + e.message; }
        } else {
            out.__NEXT_DATA__ = null;
        }

        // 2. Window state objects (comprehensive)
        out.window_state = {};
        for (const key of Object.keys(window)) {
            const lower = key.toLowerCase();
            if (/state|store|redux|config|data|notif|naukri|__initial|__next|mynaukri/i.test(key) &&
                typeof window[key] === 'object' && window[key] !== null &&
                !['__proto__'].includes(key)) {
                try {
                    const json = JSON.stringify(window[key]);
                    if (json && json.length > 2) {
                        out.window_state[key] = json.length > 500 ? json.slice(0, 500) + '...' : json;
                    }
                } catch(e) {
                    out.window_state[key] = '(non-serializable: ' + e.message + ')';
                }
            }
        }

        // 3. Full body text
        out.body_text = (document.body?.innerText || '').slice(0, 5000);

        // 4. Full body HTML structure (first 3000 chars)
        out.body_html_preview = (document.body?.innerHTML || '').slice(0, 3000);

        // 5. All script tags - both src and inline mentioning notif/notification
        out.scripts = {
            external: [],
            inline_with_notif: [],
            all_inline_previews: [],
        };
        document.querySelectorAll('script[src]').forEach(s => {
            out.scripts.external.push(s.src);
        });
        document.querySelectorAll('script:not([src])').forEach(s => {
            const t = s.textContent || '';
            if (t.length > 10) {
                out.scripts.all_inline_previews.push({
                    length: t.length,
                    preview: t.slice(0, 300),
                });
            }
            if (/notif/i.test(t)) {
                out.scripts.inline_with_notif.push(t.slice(0, 1000));
            }
        });

        // 6. All elements with 'notif' in class/id/data attributes
        out.notif_elements = [];
        document.querySelectorAll('*').forEach(el => {
            const attrs = (el.className || '').toString() + ' ' + (el.id || '') +
                ' ' + Array.from(el.attributes).map(a => a.name + '=' + a.value).join(' ');
            if (/notif/i.test(attrs)) {
                out.notif_elements.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    class: (el.className || '').toString().slice(0, 200),
                    text: (el.textContent || '').trim().slice(0, 200),
                    html: el.innerHTML.slice(0, 500),
                    attrs: Array.from(el.attributes).map(a => a.name + '=' + a.value.slice(0, 100)),
                });
            }
        });

        // 7. All links on page
        out.links = Array.from(document.querySelectorAll('a[href]')).map(a => ({
            href: a.href,
            text: (a.textContent || '').trim().slice(0, 80),
        })).filter(l => l.text).slice(0, 40);

        // 8. Embedded JSON scripts
        out.embedded_json = [];
        document.querySelectorAll('script[type*="json"]').forEach(s => {
            try {
                out.embedded_json.push({
                    type: s.type,
                    preview: s.textContent.slice(0, 500),
                });
            } catch(e) {}
        });

        // 9. XHR/Fetch interceptor hints - check for service worker
        out.service_worker = !!navigator.serviceWorker?.controller;

        // 10. Full DOM tree (depth 5)
        function htmlTree(el, depth) {
            if (depth <= 0 || !el) return null;
            const children = Array.from(el.children || []).slice(0, 15).map(c => htmlTree(c, depth - 1)).filter(Boolean);
            const result = {
                tag: el.tagName?.toLowerCase(),
            };
            if (el.id) result.id = el.id;
            if (el.className) result.class = el.className.toString().slice(0, 120);
            if (el.children?.length) result.childCount = el.children.length;
            if (children.length) result.children = children;
            return result;
        }
        out.dom_tree = htmlTree(document.body, 5);

        return out;
    }""")
    return {"status": "ok", "url": current_url, "title": title, "notif_explore": result}
