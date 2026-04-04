"""Job alert tools — unified tool for list, create, detail, update, delete."""

import asyncio
import json
from typing import Optional

from naukri_server import mcp
from naukri_server.interfaces import api_client
from naukri_server.browser import browser, page_goto
from naukri_server.config import (
    logger, NAUKRI_BASE, JOB_ALERT_API, JOB_ALERTS_LIST_API, ALERT_DETAIL_API,
    LAKHS_MULTIPLIER, BROWSER_FORM_LOAD, BROWSER_FORM_SAVE, BROWSER_MODAL_APPEAR,
    BROWSER_PAGE_LOAD, BROWSER_OPERATION_TIMEOUT,
)
from naukri_server.error_handler import handle_tool_action
from naukri_server.models import validate_action_params


async def _get_alerts_list() -> dict:
    """Internal helper — fetch all job alerts via REST API.

    Returns {status, count, alerts: [...]} or {status: "error", message}.
    """
    data = await api_client.get(JOB_ALERTS_LIST_API)
    raw_list = data.get("list", [])
    alerts = []
    for alert in raw_list:
        alerts.append({
            "alert_id": alert.get("alertId"),
            "name": alert.get("name"),
            "keywords": alert.get("keywords"),
            "location": alert.get("location"),
            "experience": alert.get("experience"),
            "min_ctc": round(float(alert.get("minCTC", 0)) / LAKHS_MULTIPLIER, 2) if alert.get("minCTC") else None,
            "max_ctc": round(float(alert.get("maxCTC", 0)) / LAKHS_MULTIPLIER, 2) if alert.get("maxCTC") else None,
            "ctc_unit": "lakhs",
            "alert_type": alert.get("alertType"),  # "ssa" or "cja"
            "function_area_id": alert.get("functionAreaId"),
            "role_id": alert.get("roleId"),
            "industry_type_id": alert.get("industryTypeId"),
        })
    return {
        "status": "success",
        "count": len(alerts),
        "alerts": alerts,
    }


async def _update_alert_browser(a_id: str, alert_name: str, keywords, location, experience, min_salary, new_name) -> dict:
    """Browser-based alert update — extracted for timeout wrapping."""
    async with browser.page_pool.acquire() as page:
        try:
            logger.info("Editing alert '%s' with aId=%s...", alert_name, a_id[:16] + "...")

            # Navigate to modify page -- form loads via /intercept/alert POST
            modify_url = f"{NAUKRI_BASE}/alert/modify?aId={a_id}"
            await page_goto(page, modify_url)
            await asyncio.sleep(BROWSER_FORM_LOAD)

            # Wait for the form to appear
            form_found = await page.evaluate("""() => {
                const form = document.getElementById('cjaFrm') || document.querySelector('form');
                return !!form;
            }""")

            if not form_found:
                # Wait a bit more for the intercept/alert POST to complete
                await asyncio.sleep(BROWSER_FORM_SAVE)
                form_found = await page.evaluate("""() => {
                    return !!(document.getElementById('cjaFrm') || document.querySelector('form'));
                }""")

            if not form_found:
                return {"status": "error", "message": "Modify form did not load on the alert edit page.", "error_code": "BROWSER_ERROR"}

            # Fill in the fields that need updating
            updates = {}
            fill_script = ""

            if keywords is not None:
                updates["keywords"] = keywords
                fill_script += f"""
                    var kwdInput = document.getElementById('Sug_kwdsugg') || document.querySelector('input[name="keyskills"]');
                    if (kwdInput) {{ kwdInput.value = {json.dumps(keywords)}; }}
                """

            if location is not None:
                updates["location"] = location
                fill_script += f"""
                    var locInput = document.getElementById('Sug_locsugg') || document.querySelector('input[name="location"]');
                    if (locInput) {{ locInput.value = {json.dumps(location)}; }}
                """

            if experience is not None:
                updates["experience"] = experience
                fill_script += f"""
                    var expInput = document.getElementById('exp_dd_cjaHid') || document.querySelector('input[name="exp"]');
                    if (expInput) {{ expInput.value = {json.dumps(str(experience))}; }}
                """

            if min_salary is not None:
                updates["min_salary"] = min_salary
                fill_script += f"""
                    var salInput = document.getElementById('minsal_dd_cjaHid') || document.querySelector('input[name="minsal"]');
                    if (salInput) {{ salInput.value = {json.dumps(str(min_salary))}; }}
                """

            if new_name is not None:
                updates["name"] = new_name
                fill_script += f"""
                    var nameInput = document.querySelector('input[name="janame"]');
                    if (nameInput) {{ nameInput.value = {json.dumps(new_name)}; }}
                """

            await page.evaluate(f"() => {{ {fill_script} }}")

            # Submit the form
            submit_result = await page.evaluate("""() => {
                var submitBtn = document.getElementById('cjaSubmit')
                    || document.querySelector('button[type="submit"]')
                    || document.querySelector('input[type="submit"]');
                if (submitBtn) {
                    submitBtn.click();
                    return 'button_clicked';
                }
                var form = document.getElementById('cjaFrm') || document.querySelector('form');
                if (form) {
                    form.submit();
                    return 'form_submitted';
                }
                return null;
            }""")

            if not submit_result:
                return {"status": "error", "message": "Could not find submit button or form on the modify page.", "error_code": "BROWSER_ERROR"}

            await asyncio.sleep(BROWSER_FORM_SAVE)

            display_name = new_name or alert_name
            logger.info("Alert '%s' updated: %s", display_name, updates)
            return {
                "status": "success",
                "alert_name": display_name,
                "updated_fields": list(updates.keys()),
                "message": f"Job alert '{display_name}' updated: {', '.join(updates.keys())}.",
            }

        except Exception as e:
            logger.error("Failed to update job alert: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to update job alert: {type(e).__name__}: {e}",
                "error_code": "BROWSER_ERROR",
            }


async def _delete_alert_browser(a_id: str, alert_name: str) -> dict:
    """Browser-based alert deletion — extracted for timeout wrapping."""
    async with browser.page_pool.acquire() as page:
        try:
            logger.info("Deleting alert '%s' (id=%s)...", alert_name, a_id[:16] + "...")

            # Navigate to the delete confirmation page
            delete_url = f"{NAUKRI_BASE}/alert/delete?aId={a_id}"
            await page_goto(page, delete_url)
            await asyncio.sleep(BROWSER_PAGE_LOAD)

            # Check if logged in
            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

            # Submit the delete confirmation via fetch POST
            delete_result = await page.evaluate("""(params) => {
                const { deleteUrl, aId } = params;
                return fetch(deleteUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded'
                    },
                    body: 'confirm=1&aId=' + encodeURIComponent(aId),
                    credentials: 'include'
                }).then(resp => ({
                    ok: resp.ok,
                    status: resp.status,
                    redirected: resp.redirected,
                    url: resp.url
                })).catch(err => ({
                    ok: false,
                    status: 0,
                    error: err.message
                }));
            }""", {"deleteUrl": f"/alert/delete?aId={a_id}", "aId": a_id})

            if not delete_result:
                return {"status": "error", "message": "Delete fetch returned no result.", "error_code": "BROWSER_ERROR"}

            if delete_result.get("error"):
                return {
                    "status": "error",
                    "message": f"Delete request failed: {delete_result['error']}",
                    "error_code": "BROWSER_ERROR",
                }

            # Also try clicking the submit button as a fallback if fetch didn't work cleanly
            if not delete_result.get("ok"):
                logger.info("Fetch POST returned status %s, trying button click fallback...",
                            delete_result.get("status"))
                button_clicked = await page.evaluate("""() => {
                    var buttons = Array.from(document.querySelectorAll(
                        'input[type="submit"], button[type="submit"], button, input[value*="Delete"], input[value*="Confirm"]'
                    ));
                    for (var i = 0; i < buttons.length; i++) {
                        var btn = buttons[i];
                        var text = (btn.textContent || btn.value || '').toLowerCase();
                        if (text.includes('delete') || text.includes('confirm') || text.includes('yes')) {
                            btn.click();
                            return text;
                        }
                    }
                    var form = document.querySelector('form');
                    if (form) { form.submit(); return 'form_submit'; }
                    return null;
                }""")

                if button_clicked:
                    logger.info("Clicked button/form: %s", button_clicked)
                    await asyncio.sleep(BROWSER_MODAL_APPEAR)
                else:
                    return {
                        "status": "error",
                        "message": f"Delete POST returned status {delete_result.get('status')} "
                                   f"and no confirm button found on page.",
                        "error_code": "BROWSER_ERROR",
                    }

            logger.info("Alert '%s' (id=%s) deleted successfully.", alert_name, a_id)
            return {
                "status": "success",
                "alert_id": a_id,
                "alert_name": alert_name,
                "message": f"Job alert '{alert_name}' (id={a_id}) has been deleted.",
            }

        except Exception as e:
            logger.error("Failed to delete job alert: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to delete job alert: {type(e).__name__}: {e}",
                "error_code": "BROWSER_ERROR",
            }


_VALID_PARAMS_PER_ACTION = {
    "list": set(),
    "detail": {"alert_id"},
    "create": {"name", "keywords", "location", "experience", "min_salary",
               "max_salary", "function_area_id", "role_id", "industry_type_id"},
    "update": {"alert_id", "keywords", "location", "experience", "min_salary", "new_name"},
    "delete": {"alert_id"},
}


@mcp.tool()
async def naukri_job_alerts(
    action: str = "list",
    alert_id: Optional[str] = None,
    name: Optional[str] = None,
    keywords: Optional[str] = None,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    min_salary: Optional[int] = None,
    max_salary: Optional[int] = None,
    function_area_id: Optional[str] = None,
    role_id: Optional[str] = None,
    industry_type_id: Optional[str] = None,
    new_name: Optional[str] = None,
) -> dict:
    """Unified job alert management -- list, create, detail, update, delete.

    Actions:
      - "list": Get all your job alerts
      - "detail": Get details for a specific alert (requires alert_id)
      - "create": Create a new alert (requires name, keywords; location/experience/min_salary optional)
      - "update": Edit alert fields (requires alert_id; pass only fields to change)
      - "delete": Delete an alert (requires alert_id)

    Args:
        action: "list" | "detail" | "create" | "update" | "delete"
        alert_id: Required for detail/update/delete
        name: Alert name for create (e.g., "Python Remote Jobs")
        keywords: Search keywords for create/update (e.g., "python developer")
        location: Location filter for create/update (e.g., "Bangalore", "Remote")
        experience: Years of experience for create/update (0-30)
        min_salary: Minimum CTC in lakhs per annum for create/update (e.g., 15 for 15 LPA)
        max_salary: Maximum CTC in lakhs per annum for create (e.g., 30 for 30 LPA)
        function_area_id: Functional area ID for create (from Naukri taxonomy)
        role_id: Role ID for create (from Naukri taxonomy)
        industry_type_id: Industry type ID for create (from Naukri taxonomy)
        new_name: Rename alert (update only)

    Returns:
        - list: {status, count, alerts: [...]}
        - detail: {status, alert: {alert_id, name, keywords, ...}}
        - create: {status, alert_name, message}
        - update: {status, alert_name, updated_fields, message}
        - delete: {status, alert_id, alert_name, message}
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "alert_id": alert_id, "name": name, "keywords": keywords,
        "location": location, "experience": experience,
        "min_salary": min_salary, "max_salary": max_salary,
        "function_area_id": function_area_id, "role_id": role_id,
        "industry_type_id": industry_type_id, "new_name": new_name,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    if action == "list":
        return _attach_unused(await handle_tool_action(_get_alerts_list, "alerts.list"))

    elif action == "detail":
        if not alert_id:
            return {"status": "error", "message": "detail requires alert_id.", "error_code": "VALIDATION_ERROR"}

        async def _detail():
            data = await api_client.get(f"{ALERT_DETAIL_API}/{alert_id}")
            alerts = data.get("list", [data] if isinstance(data, dict) else data)
            if not alerts:
                return {"status": "error", "message": f"Alert {alert_id} not found", "error_code": "NOT_FOUND"}

            a = alerts[0] if isinstance(alerts, list) else alerts
            if not isinstance(a, dict):
                return {"status": "error", "message": f"Unexpected response for alert {alert_id}", "error_code": "API_ERROR"}

            return {
                "status": "success",
                "alert": {
                    "alert_id": a.get("alertId", alert_id),
                    "name": a.get("name", ""),
                    "keywords": a.get("keywords", ""),
                    "location": a.get("location", ""),
                    "experience": a.get("experience"),
                    "min_ctc": round(float(a.get("minCTC", 0)) / LAKHS_MULTIPLIER, 2) if a.get("minCTC") else None,
                    "max_ctc": round(float(a.get("maxCTC", 0)) / LAKHS_MULTIPLIER, 2) if a.get("maxCTC") else None,
                    "ctc_unit": "lakhs",
                    "alert_type": a.get("alertType", ""),
                    "function_area_id": a.get("functionAreaId"),
                    "role_id": a.get("roleId"),
                    "industry_type_id": a.get("industryTypeId"),
                },
            }
        return _attach_unused(await handle_tool_action(_detail, "alerts.detail"))

    elif action == "create":
        if not name or not keywords:
            return {"status": "error", "message": "create requires name and keywords.", "error_code": "VALIDATION_ERROR"}

        async def _create():
            body = {"name": name, "keyword": keywords}
            if location is not None:
                body["location"] = location
            if experience is not None:
                body["experience"] = str(experience)
            if min_salary is not None:
                body["minCTC"] = str(min_salary * LAKHS_MULTIPLIER)
            if max_salary is not None:
                body["maxCTC"] = str(max_salary * LAKHS_MULTIPLIER)
            if function_area_id is not None:
                body["functionAreaId"] = function_area_id
            if role_id is not None:
                body["roleId"] = role_id
            if industry_type_id is not None:
                body["industryTypeId"] = industry_type_id

            response = await api_client.post(JOB_ALERT_API, body)
            result = {
                "status": "success",
                "alert_name": name,
                "message": f"Job alert '{name}' created for '{keywords}'.",
            }
            # v1 API returns matched jobs inline — expose them
            total_matched = response.get("totalRes") if isinstance(response, dict) else None
            matched_jobs = response.get("list", []) if isinstance(response, dict) else []
            if total_matched is not None:
                result["total_matched"] = total_matched
            if matched_jobs:
                result["matched_jobs"] = matched_jobs[:5]
            return result
        return _attach_unused(await handle_tool_action(_create, "alerts.create"))

    elif action == "update":
        if not alert_id:
            return {"status": "error", "message": "update requires alert_id.", "error_code": "VALIDATION_ERROR"}
        if all(v is None for v in (keywords, location, experience, min_salary, new_name)):
            return {"status": "error", "message": "No fields to update. Pass at least one parameter.", "error_code": "VALIDATION_ERROR"}

        # Look up alert name for logging
        try:
            alerts_result = await _get_alerts_list()
        except Exception as e:
            logger.debug("Failed to fetch alerts list for update lookup: %s", e)
            alerts_result = {"status": "error", "message": "Failed to fetch alerts list", "error_code": "API_ERROR"}

        alert_name = str(alert_id)  # fallback
        if alerts_result.get("status") == "success":
            for alert in alerts_result.get("alerts", []):
                if str(alert.get("alert_id")) == str(alert_id):
                    alert_name = alert.get("name", str(alert_id))
                    break

        a_id = str(alert_id)

        try:
            return _attach_unused(await asyncio.wait_for(
                _update_alert_browser(a_id, alert_name, keywords, location, experience, min_salary, new_name),
                timeout=BROWSER_OPERATION_TIMEOUT,
            ))
        except asyncio.TimeoutError:
            return {"status": "error", "message": f"Alert update timed out after {BROWSER_OPERATION_TIMEOUT}s", "error_code": "TIMEOUT"}

    elif action == "delete":
        if not alert_id:
            return {"status": "error", "message": "delete requires alert_id.", "error_code": "VALIDATION_ERROR"}

        # Get all alerts to find the target
        try:
            alerts_result = await _get_alerts_list()
        except Exception as e:
            logger.debug("Failed to fetch alerts list for delete lookup: %s", e)
            alerts_result = {"status": "error", "error_code": "API_ERROR"}

        if alerts_result.get("status") != "success":
            return {"status": "error", "message": "Failed to fetch alerts.", "error_code": "API_ERROR"}

        target = None
        for alert in alerts_result.get("alerts", []):
            if str(alert.get("alert_id")) == str(alert_id) or str(alert.get("id")) == str(alert_id):
                target = alert
                break

        if not target:
            return {"status": "error", "message": f"Alert ID '{alert_id}' not found.", "error_code": "NOT_FOUND"}

        a_id = str(target["alert_id"])
        alert_name = target.get("name", a_id)

        try:
            return _attach_unused(await asyncio.wait_for(
                _delete_alert_browser(a_id, alert_name),
                timeout=BROWSER_OPERATION_TIMEOUT,
            ))
        except asyncio.TimeoutError:
            return {"status": "error", "message": f"Alert delete timed out after {BROWSER_OPERATION_TIMEOUT}s", "error_code": "TIMEOUT"}

    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, detail, create, update, delete", "error_code": "VALIDATION_ERROR"}
