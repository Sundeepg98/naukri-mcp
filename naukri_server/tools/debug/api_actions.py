"""API-based debug actions: fetch_api, post_api, put_api, delete_api, fetch_widget, settings_api."""

from naukri_server.config import NAUKRI_BASE


async def do_fetch_api(page, url: str) -> dict:
    """Browser-context GET request; url is the API path to fetch."""
    api_url = url or ""
    if not api_url.startswith("http"):
        api_url = f"{NAUKRI_BASE}{api_url}"
    result = await page.evaluate("""async (apiUrl) => {
        try {
            const resp = await fetch(apiUrl, {
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'appid': '121',
                    'clientid': 'd3skt0p',
                    'content-type': 'application/json',
                    'systemid': 'Naukri',
                    'x-requested-with': 'XMLHttpRequest',
                },
            });
            const text = await resp.text();
            let body = null;
            try { body = JSON.parse(text); } catch(e) { body = text.slice(0, 2000); }
            return {
                status: resp.status,
                url: resp.url,
                ok: resp.ok,
                headers: Object.fromEntries(resp.headers.entries()),
                body: body,
            };
        } catch(e) {
            return {error: e.message};
        }
    }""", api_url)
    return {"status": "ok", "fetch_result": result}


async def do_post_api(page, url: str) -> dict:
    """Browser-context POST; url format "API_PATH|JSON_BODY"."""
    parts = (url or "").split("|", 1)
    api_url = parts[0] if parts else ""
    post_body = parts[1] if len(parts) > 1 else "{}"
    if not api_url.startswith("http"):
        api_url = f"{NAUKRI_BASE}{api_url}"
    result = await page.evaluate("""async ([apiUrl, postBody]) => {
        try {
            const resp = await fetch(apiUrl, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'appid': '121',
                    'clientid': 'd3skt0p',
                    'content-type': 'application/json',
                    'systemid': 'Naukri',
                    'x-requested-with': 'XMLHttpRequest',
                },
                body: postBody,
            });
            const text = await resp.text();
            let body = null;
            try { body = JSON.parse(text); } catch(e) { body = text.slice(0, 5000); }
            return {
                status: resp.status,
                url: resp.url,
                ok: resp.ok,
                headers: Object.fromEntries(resp.headers.entries()),
                body: body,
            };
        } catch(e) {
            return {error: e.message};
        }
    }""", [api_url, post_body])
    return {"status": "ok", "post_result": result}


async def do_put_api(page, url: str) -> dict:
    """Browser-context PUT; url format "API_PATH|JSON_BODY"."""
    parts = (url or "").split("|", 1)
    api_url = parts[0] if parts else ""
    put_body = parts[1] if len(parts) > 1 else "{}"
    if not api_url.startswith("http"):
        api_url = f"{NAUKRI_BASE}{api_url}"
    result = await page.evaluate("""async ([apiUrl, putBody]) => {
        try {
            const resp = await fetch(apiUrl, {
                method: 'PUT',
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'appid': '121',
                    'clientid': 'd3skt0p',
                    'content-type': 'application/json',
                    'systemid': 'Naukri',
                    'x-requested-with': 'XMLHttpRequest',
                },
                body: putBody,
            });
            const text = await resp.text();
            let body = null;
            try { body = JSON.parse(text); } catch(e) { body = text.slice(0, 5000); }
            return {
                status: resp.status,
                url: resp.url,
                ok: resp.ok,
                headers: Object.fromEntries(resp.headers.entries()),
                body: body,
            };
        } catch(e) {
            return {error: e.message};
        }
    }""", [api_url, put_body])
    return {"status": "ok", "put_result": result}


async def do_delete_api(page, url: str) -> dict:
    """Browser-context DELETE; url is API path, optional "|JSON_BODY"."""
    parts = (url or "").split("|", 1)
    api_url = parts[0] if parts else ""
    del_body = parts[1] if len(parts) > 1 else None
    if not api_url.startswith("http"):
        api_url = f"{NAUKRI_BASE}{api_url}"
    result = await page.evaluate("""async ([apiUrl, delBody]) => {
        try {
            const opts = {
                method: 'DELETE',
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'appid': '121',
                    'clientid': 'd3skt0p',
                    'content-type': 'application/json',
                    'systemid': 'Naukri',
                    'x-requested-with': 'XMLHttpRequest',
                },
            };
            if (delBody) opts.body = delBody;
            const resp = await fetch(apiUrl, opts);
            const text = await resp.text();
            let body = null;
            try { body = JSON.parse(text); } catch(e) { body = text.slice(0, 5000); }
            return {
                status: resp.status,
                url: resp.url,
                ok: resp.ok,
                headers: Object.fromEntries(resp.headers.entries()),
                body: body,
            };
        } catch(e) {
            return {error: e.message};
        }
    }""", [api_url, del_body])
    return {"status": "ok", "delete_result": result}


async def do_fetch_widget(page, url: str) -> dict:
    """Like fetch_api but uses widget headers (appid:109, systemid:109)."""
    api_url = url or ""
    if not api_url.startswith("http"):
        api_url = f"{NAUKRI_BASE}{api_url}"
    result = await page.evaluate("""async (apiUrl) => {
        try {
            const resp = await fetch(apiUrl, {
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'appid': '109',
                    'systemid': '109',
                    'clientid': 'd3skt0p',
                    'x-requested-with': 'XMLHttpRequest',
                },
            });
            const text = await resp.text();
            let body = null;
            try { body = JSON.parse(text); } catch(e) { body = text.slice(0, 2000); }
            return {
                status: resp.status,
                url: resp.url,
                ok: resp.ok,
                headers: Object.fromEntries(resp.headers.entries()),
                body: body,
            };
        } catch(e) {
            return {error: e.message};
        }
    }""", api_url)
    return {"status": "ok", "fetch_result": result}


async def do_settings_api(page, url: str) -> dict:
    """Fetch profile/communication/visibility settings from Naukri API (url ignored)."""
    from naukri_server.api import api_get

    result = {"user": None, "communication_settings": None, "visibility": None,
              "additional_details": None, "extended_profile": None, "raw_keys": []}
    try:
        data = await api_get(
            "/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self",
            {"expand_level": "4"},
        )
        result["raw_keys"] = list(data.keys())

        # Full user object (contains communicationSettings, resdexVisibility, etc.)
        user = data.get("user", {})
        result["user"] = user
        result["communication_settings"] = user.get("communicationSettings")
        result["visibility"] = user.get("resdexVisibility")

        # Additional details and extended profile
        result["additional_details"] = data.get("additionalDetails")
        result["extended_profile"] = data.get("extendedProfile")
        result["profile_additional"] = data.get("profileAdditional")

        # Also check for any settings-related endpoints in user properties
        result["user_properties"] = user.get("userProperties")

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return {"status": "ok", "settings_api": result}
