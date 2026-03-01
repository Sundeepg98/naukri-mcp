"""Job alert tools — list, create, delete, and manage saved search alerts on Naukri."""

import asyncio
from typing import Optional
from urllib.parse import parse_qs, urlparse

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.browser import browser, page_goto
from naukri_server.config import logger, NAUKRI_BASE, JOB_ALERT_API, JOB_ALERTS_LIST_API, ALERT_DETAIL_API


@mcp.tool()
@api_tool("List job alerts")
async def naukri_get_job_alerts() -> dict:
    """List all job alerts (saved searches) on your Naukri account.

    Returns both SSA (Save Search Alert) and CJA (Custom Job Alert) types.

    Returns:
        - {status: "success", count, alerts: [{alert_id, name, keywords, location,
          experience, min_ctc, max_ctc, alert_type, function_area_id, role_id,
          industry_type_id}]}
        - {status: "error", message}
    """
    data = await api_get(JOB_ALERTS_LIST_API)
    raw_list = data.get("list", [])
    alerts = []
    for alert in raw_list:
        alerts.append({
            "alert_id": alert.get("alertId"),
            "name": alert.get("name"),
            "keywords": alert.get("keywords"),
            "location": alert.get("location"),
            "experience": alert.get("experience"),
            "min_ctc": alert.get("minCTC"),
            "max_ctc": alert.get("maxCTC"),
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


@mcp.tool()
@api_tool("Create job alert")
async def naukri_create_job_alert(
    name: str,
    keyword: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    min_ctc: Optional[int] = None,
    function_area_id: Optional[str] = None,
    role_id: Optional[str] = None,
    industry_type_id: Optional[str] = None,
) -> dict:
    """Create a job alert (saved search) on Naukri. You'll receive email
    notifications when new jobs match your criteria.

    Args:
        name: Alert name (e.g., "Python Remote Jobs")
        keyword: Search keywords (e.g., "python developer")
        location: City filter (e.g., "Bangalore", "Remote")
        experience: Years of experience filter (e.g., 5)
        min_ctc: Minimum CTC in lakhs (e.g., 15 for 15 LPA)
        function_area_id: Functional area ID (from Naukri taxonomy)
        role_id: Role ID (from Naukri taxonomy)
        industry_type_id: Industry type ID (from Naukri taxonomy)

    Returns:
        - {status: "success", alert_name, message}
        - {status: "duplicate", alert_name, message, replace_url}
        - {status: "error", message}
    """
    body = {"name": name, "keyword": keyword}
    if location is not None:
        body["location"] = location
    if experience is not None:
        body["experience"] = str(experience)
    if min_ctc is not None:
        body["minCTC"] = str(min_ctc * 100000)
    if function_area_id is not None:
        body["functionAreaId"] = function_area_id
    if role_id is not None:
        body["roleId"] = role_id
    if industry_type_id is not None:
        body["industryTypeId"] = industry_type_id

    await api_post(JOB_ALERT_API, body)
    return {
        "status": "success",
        "alert_name": name,
        "message": f"Job alert '{name}' created for '{keyword}'.",
    }


@mcp.tool()
@api_tool("Get alert detail")
async def naukri_get_alert_detail(alert_id: str) -> dict:
    """Get details of a specific job alert by its ID.

    Use naukri_get_job_alerts first to get alert IDs.

    Args:
        alert_id: The alert ID (from naukri_get_job_alerts results)

    Returns:
        - {status: "success", alert: {alert_id, name, keywords, location, experience, ...}}
        - {status: "error", message}
    """
    data = await api_get(f"{ALERT_DETAIL_API}/{alert_id}")
    alerts = data.get("list", [data] if isinstance(data, dict) else data)
    if not alerts:
        return {"status": "error", "message": f"Alert {alert_id} not found"}

    a = alerts[0] if isinstance(alerts, list) else alerts
    if not isinstance(a, dict):
        return {"status": "error", "message": f"Unexpected response for alert {alert_id}"}

    return {
        "status": "success",
        "alert": {
            "alert_id": a.get("alertId", alert_id),
            "name": a.get("name", ""),
            "keywords": a.get("keywords", ""),
            "location": a.get("location", ""),
            "experience": a.get("experience"),
            "min_ctc": a.get("minCTC"),
            "max_ctc": a.get("maxCTC"),
            "alert_type": a.get("alertType", ""),
            "function_area_id": a.get("functionAreaId"),
            "role_id": a.get("roleId"),
            "industry_type_id": a.get("industryTypeId"),
        },
    }


@mcp.tool()
async def naukri_delete_job_alert(alert_name: str) -> dict:
    """Delete a job alert by name. Uses browser automation since no REST API exists.

    Naukri has no REST DELETE endpoint for alerts (Akamai CDN returns 501).
    This tool navigates to the legacy alert management page, finds the alert
    by name, and submits the delete confirmation form.

    Args:
        alert_name: The name of the alert to delete (case-insensitive partial match)

    Returns:
        - {status: "success", alert_name, message}
        - {status: "error", message}
    """
    async with browser.page_pool.acquire() as page:
        try:
            # Step 1: Navigate to the legacy alert management page
            await page_goto(page, f"{NAUKRI_BASE}/alert/manage")
            await asyncio.sleep(3)

            # Check if logged in
            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first."}

            # Step 2: Scrape alert rows — extract alert names and their delete link hrefs
            alerts_data = await page.evaluate("""() => {
                const results = [];
                // Look for links that contain '/alert/delete' in href
                const deleteLinks = document.querySelectorAll('a[href*="/alert/delete"]');
                for (const link of deleteLinks) {
                    // Walk up to find the row/container that has the alert name
                    const row = link.closest('tr') || link.closest('div') || link.parentElement;
                    if (!row) continue;

                    // The alert name is typically in the row text (excluding the link text)
                    const rowText = row.textContent || '';
                    const href = link.getAttribute('href') || '';
                    results.push({
                        name: rowText.trim(),
                        href: href
                    });
                }

                // Fallback: also look for table rows with alert info
                if (results.length === 0) {
                    const rows = document.querySelectorAll('table tr, .alert-row, [class*="alert"]');
                    for (const row of rows) {
                        const link = row.querySelector('a[href*="delete"]');
                        if (link) {
                            results.push({
                                name: row.textContent.trim(),
                                href: link.getAttribute('href') || ''
                            });
                        }
                    }
                }

                return results;
            }""")

            if not alerts_data:
                # Maybe the page structure is different — check if there are any alerts at all
                page_text_content = await page.evaluate("() => document.body.innerText")
                logger.warning("No alert delete links found. Page text: %s", page_text_content[:500])
                return {
                    "status": "error",
                    "message": "No alerts found on the manage page. The page may have no alerts, "
                               "or the page structure may have changed.",
                }

            # Step 3: Match the user's alert_name (case-insensitive partial match)
            alert_name_lower = alert_name.lower()
            matched = None
            for alert in alerts_data:
                if alert_name_lower in alert.get("name", "").lower():
                    matched = alert
                    break

            if not matched:
                available = [a.get("name", "")[:80] for a in alerts_data]
                return {
                    "status": "error",
                    "message": f"No alert matching '{alert_name}' found. "
                               f"Available alerts: {available}",
                }

            # Step 4: Extract the aId from the delete link URL
            delete_href = matched["href"]
            if not delete_href:
                return {"status": "error", "message": "Delete link found but href is empty."}

            # Parse aId from the URL (e.g., /alert/delete?aId=<96-hex-chars>)
            parsed = urlparse(delete_href)
            query_params = parse_qs(parsed.query)
            aid_values = query_params.get("aId", [])
            if not aid_values:
                return {
                    "status": "error",
                    "message": f"Could not extract aId from delete link: {delete_href}",
                }
            a_id = aid_values[0]

            logger.info("Deleting alert '%s' with aId=%s...", alert_name, a_id[:16] + "...")

            # Step 5: Navigate to the delete confirmation page
            delete_url = f"{NAUKRI_BASE}/alert/delete?aId={a_id}"
            await page_goto(page, delete_url)
            await asyncio.sleep(2)

            # Step 6: Submit the delete confirmation via fetch POST
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
                return {"status": "error", "message": "Delete fetch returned no result."}

            if delete_result.get("error"):
                return {
                    "status": "error",
                    "message": f"Delete request failed: {delete_result['error']}",
                }

            # Also try clicking the submit button as a fallback if fetch didn't work cleanly
            if not delete_result.get("ok"):
                logger.info("Fetch POST returned status %s, trying button click fallback...",
                            delete_result.get("status"))
                button_clicked = await page.evaluate("""() => {
                    // Look for confirm/submit/delete buttons
                    const buttons = Array.from(document.querySelectorAll(
                        'input[type="submit"], button[type="submit"], button, input[value*="Delete"], input[value*="Confirm"]'
                    ));
                    for (const btn of buttons) {
                        const text = (btn.textContent || btn.value || '').toLowerCase();
                        if (text.includes('delete') || text.includes('confirm') || text.includes('yes')) {
                            btn.click();
                            return text;
                        }
                    }
                    // Try submitting the form directly
                    const form = document.querySelector('form');
                    if (form) { form.submit(); return 'form_submit'; }
                    return null;
                }""")

                if button_clicked:
                    logger.info("Clicked button/form: %s", button_clicked)
                    await asyncio.sleep(2)
                else:
                    return {
                        "status": "error",
                        "message": f"Delete POST returned status {delete_result.get('status')} "
                                   f"and no confirm button found on page.",
                    }

            matched_name = matched.get("name", alert_name)[:80]
            logger.info("Alert '%s' deleted successfully.", matched_name)
            return {
                "status": "success",
                "alert_name": matched_name,
                "message": f"Job alert matching '{alert_name}' has been deleted.",
            }

        except Exception as e:
            logger.error("Failed to delete job alert: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to delete job alert: {type(e).__name__}: {e}",
            }
