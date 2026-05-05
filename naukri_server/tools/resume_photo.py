"""Profile media management — unified tool for resume and photo operations.

DDD: API-response reads in _resume_info now route through ``safe_get``.
Browser-driven mutations (upload/delete) stay on plain ``page.evaluate(...)``
results because those are local DOM scrapes, not external API responses.
"""

import asyncio
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import api_tool
from naukri_server.interfaces import api_client
from naukri_server.browser import browser, page_goto
from naukri_server.config import (
    PROFILE_API, PHOTO_API, RESUME_DOWNLOAD_API, NAUKRI_BASE, RESUME_MAX_SIZE_MB,
    BROWSER_OPERATION_TIMEOUT, logger,
    BROWSER_PAGE_LOAD, BROWSER_MODAL_APPEAR, BROWSER_UPLOAD_COMPLETE, BROWSER_FORM_SAVE,
    BROWSER_PAGE_SETTLE, BROWSER_DOM_SETTLE,
)
from naukri_server.domain import safe_get
from naukri_server.tools.profile import _profile_ttl_cache, _dashboard_ttl_cache

PHOTO_ALLOWED_FORMATS = {".png", ".jpg", ".jpeg", ".gif"}
RESUME_ALLOWED_FORMATS = {".pdf", ".doc", ".docx"}



# ---------------------------------------------------------------------------
# Private helpers — resume
# ---------------------------------------------------------------------------


@api_tool("Get resume info")
async def _resume_info() -> dict:
    data = await api_client.get(PROFILE_API, params={"expand_level": "4"})

    # 'profile' may be a list (v3+ shape) or a dict (older shape) — handle both.
    profiles = safe_get(data, "profile", field_name="profile", warn=True, default=[])
    if isinstance(profiles, list) and profiles:
        profile = profiles[0]
    elif isinstance(profiles, dict):
        profile = profiles
    else:
        profile = {}
    if not isinstance(profile, dict):
        profile = {}

    cv_info = safe_get(profile, "cvInfo", field_name="cvInfo", warn=False, default={})
    if not isinstance(cv_info, dict):
        cv_info = {}

    return {
        "status": "success",
        "resume_headline": safe_get(profile, "resumeHeadline", field_name="resumeHeadline", warn=False, default=""),
        # cv_* and *_old key duplication is API-versioning churn (v3 vs v4) —
        # safe_get's multi-key form picks the first present key.
        "file_name": safe_get(cv_info, "cvName", "fileName", field_name="cv.file_name", warn=False, default=""),
        "upload_date": safe_get(cv_info, "cvUploadDate", "uploadDate", field_name="cv.upload_date", warn=False, default=""),
        "file_size": safe_get(cv_info, "cvSize", "fileSize", field_name="cv.file_size", warn=False, default=""),
        "download_url": f"{NAUKRI_BASE}{RESUME_DOWNLOAD_API}",
        "cv_id": safe_get(cv_info, "cvId", "id", field_name="cv.id", warn=False, default=""),
    }


async def _resume_download(save_path: str) -> dict:
    from naukri_server.config import API_HEADERS

    try:
        token = await browser.token_manager.ensure_token()
        cookie_str = browser.token_manager.get_cookies()

        session = await api_client.get_session()
        headers = {
            **API_HEADERS,
            "Authorization": f"Bearer {token}",
            "cookie": cookie_str,
        }

        url = f"{NAUKRI_BASE}{RESUME_DOWNLOAD_API}"
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return {"status": "error", "message": f"Download failed with HTTP {resp.status}", "error_code": "API_ERROR"}

            data = await resp.read()
            if not data:
                return {"status": "error", "message": "Empty response — no resume data received.", "error_code": "NOT_FOUND"}

            save = Path(save_path)
            save.parent.mkdir(parents=True, exist_ok=True)
            save.write_bytes(data)

            return {
                "status": "success",
                "file_path": str(save.resolve()),
                "file_size_bytes": len(data),
                "message": f"Resume saved to {save.resolve()} ({len(data)} bytes).",
            }
    except Exception as e:
        return {"status": "error", "message": f"Resume download failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


async def _resume_upload(file_path: str) -> dict:
    path = Path(file_path).resolve()

    # Validate file exists
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}", "error_code": "NOT_FOUND"}

    # Validate path — only allow files from user's home directory or current working directory
    if not (str(path).startswith(str(Path.home())) or str(path).startswith(str(Path.cwd()))):
        return {"status": "error", "message": "File access denied — only files in home or working directory allowed", "error_code": "VALIDATION_ERROR"}

    # Validate format
    if path.suffix.lower() not in RESUME_ALLOWED_FORMATS:
        return {"status": "error", "message": f"Unsupported format '{path.suffix}'. Use: {', '.join(RESUME_ALLOWED_FORMATS)}", "error_code": "VALIDATION_ERROR"}

    # Validate size
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > RESUME_MAX_SIZE_MB:
        return {"status": "error", "message": f"File too large ({size_mb:.1f}MB). Max: {RESUME_MAX_SIZE_MB}MB", "error_code": "VALIDATION_ERROR"}

    async with browser.page_pool.acquire() as page:
        try:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(BROWSER_PAGE_LOAD)

            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

            # Find the resume upload input
            file_input = await page.query_selector('input[type="file"]')
            if not file_input:
                # Try clicking an upload button first to reveal the file input
                upload_btn = await page.query_selector(
                    '[class*="upload"] button, button:has-text("Upload"), '
                    '[class*="resume"] button, [class*="Resume"] [class*="edit"]'
                )
                if upload_btn:
                    await upload_btn.click()
                    await asyncio.sleep(BROWSER_MODAL_APPEAR)
                    file_input = await page.query_selector('input[type="file"]')

            if not file_input:
                return {"status": "error", "message": "Could not find file upload input on profile page", "error_code": "BROWSER_ERROR"}

            # Upload the file
            await file_input.set_input_files(str(path.resolve()))
            await asyncio.sleep(BROWSER_UPLOAD_COMPLETE)

            _profile_ttl_cache.invalidate()
            _dashboard_ttl_cache.invalidate()

            try:
                from naukri_server.events import event_bus, ResumeUploaded
                await event_bus.emit(ResumeUploaded(file_name=path.name, size_mb=round(size_mb, 2)))
            except Exception:
                pass

            return {
                "status": "success",
                "action": "uploaded",
                "file": path.name,
                "size_mb": round(size_mb, 2),
                "message": f"Resume '{path.name}' uploaded to Naukri profile.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Upload failed: {type(e).__name__}: {e}", "error_code": "BROWSER_ERROR"}


# ---------------------------------------------------------------------------
# Private helpers — photo
# ---------------------------------------------------------------------------


@api_tool("Get photo info")
async def _photo_info() -> dict:
    data = await api_client.get(PROFILE_API, params={"expand_level": "4"})

    profiles = data.get("profile", [])
    profile = profiles[0] if isinstance(profiles, list) and profiles else data.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}

    photo_info = profile.get("photoInfo", {})
    if not isinstance(photo_info, dict):
        photo_info = {}

    photo_url = photo_info.get("photoURL", photo_info.get("photoUrl", photo_info.get("url", "")))
    has_photo = photo_info.get("isAvailable", bool(photo_url))

    return {
        "status": "success",
        "has_photo": has_photo,
        "photo_url": photo_url,
        "format": photo_info.get("photoFormat", ""),
        "status_label": photo_info.get("status", ""),
        "upload_date": photo_info.get("uploadDate", ""),
        "download_api": f"{NAUKRI_BASE}{PHOTO_API}",
    }


async def _photo_upload(file_path: str) -> dict:
    path = Path(file_path).resolve()

    # Validate file exists
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}", "error_code": "NOT_FOUND"}

    # Validate path — only allow files from user's home directory or current working directory
    if not (str(path).startswith(str(Path.home())) or str(path).startswith(str(Path.cwd()))):
        return {"status": "error", "message": "File access denied — only files in home or working directory allowed", "error_code": "VALIDATION_ERROR"}

    # Validate format
    if path.suffix.lower() not in PHOTO_ALLOWED_FORMATS:
        return {
            "status": "error",
            "message": f"Unsupported format '{path.suffix}'. Use: {', '.join(PHOTO_ALLOWED_FORMATS)}",
            "error_code": "VALIDATION_ERROR",
        }

    async with browser.page_pool.acquire() as page:
        try:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(BROWSER_PAGE_LOAD)

            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

            # Click photo area to open upload modal (JS click to bypass visibility checks)
            await page.evaluate("""() => {
                // Try clicking the photo/avatar area or "Add photo" link
                const selectors = [
                    '.photoWrap', '.photoWrap img',
                    '[class*="photoWrapper"]', '[class*="profilePhoto"]',
                    '.avatarEdit',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { el.click(); return; }
                }
                // Fallback: look for "Add photo" or "Replace photo" text
                const links = Array.from(document.querySelectorAll('a, button, span, div'));
                for (const el of links) {
                    const text = el.textContent.trim().toLowerCase();
                    if (text === 'add photo' || text === 'replace photo' || text === 'upload photo') {
                        el.click(); return;
                    }
                }
            }""")
            await asyncio.sleep(BROWSER_FORM_SAVE)

            # Set the file on the hidden file input
            file_input = await page.query_selector('#fileUpload')
            if not file_input:
                # Fallback: look for any file input inside the photo cropper area
                file_input = await page.query_selector(
                    '#photoCropper input[type="file"], '
                    '[class*="photo"] input[type="file"], '
                    'input[type="file"][accept*="image"]'
                )
            if not file_input:
                # Last resort: any file input on the page
                file_input = await page.query_selector('input[type="file"]')

            if not file_input:
                return {"status": "error", "message": "Could not find photo upload input on profile page", "error_code": "BROWSER_ERROR"}

            await file_input.set_input_files(str(path.resolve()))
            logger.info("Photo file set: %s", path.name)

            # Wait for the image to load in the cropper preview
            await asyncio.sleep(BROWSER_FORM_SAVE)

            # Click the save/submit button
            save_btn = await page.query_selector(
                '#submit, button.save-photo, #photoCropper button[type="submit"], '
                '#photoCropper button:has-text("Save"), '
                'button:has-text("Save photo"), button:has-text("Apply")'
            )
            if save_btn:
                await save_btn.click()
                logger.info("Clicked save button for photo upload")
                await asyncio.sleep(BROWSER_UPLOAD_COMPLETE)
            else:
                # Some flows auto-upload without a save button; wait for network idle
                logger.info("No save button found, waiting for auto-upload")
                await asyncio.sleep(BROWSER_UPLOAD_COMPLETE)

            try:
                from naukri_server.events import event_bus, PhotoUploaded
                await event_bus.emit(PhotoUploaded(file_name=path.name))
            except Exception:
                pass

            return {
                "status": "success",
                "action": "uploaded",
                "file": path.name,
                "message": f"Photo '{path.name}' uploaded to Naukri profile.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Photo upload failed: {type(e).__name__}: {e}", "error_code": "BROWSER_ERROR"}


async def _photo_delete() -> dict:
    async with browser.page_pool.acquire() as page:
        try:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(BROWSER_PAGE_LOAD)

            if "/nlogin" in page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first.", "error_code": "AUTH_ERROR"}

            # Check if a photo exists on the profile page
            has_photo = await page.evaluate("""() => {
                const img = document.querySelector('.photoWrap img, [class*="photo"] img, [class*="avatar"] img, [class*="profilePhoto"] img');
                if (img && img.src && !img.src.includes('placeholder') && !img.src.includes('default')) {
                    return true;
                }
                const photoInfo = document.querySelector('[class*="photoInfo"], [class*="PhotoInfo"]');
                if (photoInfo) return true;
                return false;
            }""")

            if not has_photo:
                return {"status": "no_photo", "message": "No profile photo found to delete."}

            # Click on the photo area to open the photo cropper modal
            photo_clicked = await page.evaluate("""() => {
                const selectors = [
                    '.photoWrap', '.photoWrap img',
                    '[class*="photo"] img', '[class*="avatar"] img',
                    '[class*="profilePhoto"]', '[class*="ProfilePhoto"]',
                    '[class*="photoEdit"]', '[class*="editPhoto"]',
                    '[class*="photo-wrap"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { el.click(); return sel; }
                }
                return null;
            }""")

            if not photo_clicked:
                return {"status": "error", "message": "Could not find the profile photo element to click.", "error_code": "BROWSER_ERROR"}

            logger.info("Clicked photo element: %s", photo_clicked)

            # Wait for the photo cropper modal to appear
            modal_appeared = False
            for _ in range(10):
                await asyncio.sleep(BROWSER_PAGE_SETTLE)
                modal_appeared = await page.evaluate("""() => {
                    const modal = document.querySelector('#photoCropper, [class*="photoCropper"], [class*="PhotoCropper"], [class*="cropModal"], [class*="photo-modal"], [class*="modal"][class*="photo"], [role="dialog"]');
                    return !!modal;
                }""")
                if modal_appeared:
                    break

            if not modal_appeared:
                return {"status": "error", "message": "Photo cropper modal did not appear after clicking photo.", "error_code": "BROWSER_ERROR"}

            await asyncio.sleep(BROWSER_PAGE_SETTLE)  # Let modal fully render

            # Step 1: Click the initial "Delete" option (.delBtn) in the photo modal
            delete_clicked = await page.evaluate("""() => {
                const el = document.querySelector('button.delBtn, .typ-14Bold.delBtn, .delBtn');
                if (el) { el.click(); return el.className; }
                const buttons = Array.from(document.querySelectorAll('button, a, span'));
                for (const btn of buttons) {
                    const text = btn.textContent.trim().toLowerCase();
                    if ((text === 'delete' || text === 'delete photo') && !btn.classList.contains('btn-dark-ot')) {
                        btn.click();
                        return 'text:' + btn.textContent.trim();
                    }
                }
                return null;
            }""")

            if not delete_clicked:
                return {"status": "error", "message": "Could not find the delete button in the photo modal.", "error_code": "BROWSER_ERROR"}

            logger.info("Clicked initial delete option: %s", delete_clicked)

            # Step 2: Click the confirmation "Delete" button
            await asyncio.sleep(BROWSER_MODAL_APPEAR)
            confirm_clicked = await page.evaluate("""() => {
                // Find all visible Delete buttons, click the one in the confirmation overlay
                const buttons = Array.from(document.querySelectorAll('button'));
                const delBtns = buttons.filter(b => {
                    const text = b.textContent.trim().toLowerCase();
                    return text === 'delete' && !b.classList.contains('delBtn') && b.offsetParent !== null;
                });
                // The confirmation Delete is the non-.delBtn one
                if (delBtns.length > 0) {
                    delBtns[delBtns.length - 1].click();
                    return delBtns[delBtns.length - 1].className;
                }
                return null;
            }""")

            if confirm_clicked is None:
                return {"status": "error", "message": "Could not find confirmation dialog to delete photo.", "error_code": "BROWSER_ERROR"}

            logger.info("Clicked confirmation button: %s", confirm_clicked)
            await asyncio.sleep(BROWSER_UPLOAD_COMPLETE)  # Wait for deletion to complete

            try:
                from naukri_server.events import event_bus, PhotoDeleted
                await event_bus.emit(PhotoDeleted())
            except Exception:
                pass

            return {
                "status": "success",
                "action": "deleted",
                "message": "Profile photo deleted successfully.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Photo deletion failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


# ---------------------------------------------------------------------------
# Individual tools (preferred over naukri_profile_media)
# ---------------------------------------------------------------------------


@mcp.tool()
async def naukri_resume_info() -> dict:
    """Get resume metadata — filename, upload date, download URL, cv_id.

    Returns:
        {status, resume_headline, file_name, upload_date, file_size, download_url, cv_id}
    """
    return await _resume_info()


@mcp.tool()
async def naukri_upload_resume(file_path: str) -> dict:
    """Upload a new resume to Naukri profile (PDF/DOC/DOCX, max 5MB).

    Uses browser automation to upload the file via the profile page.

    Args:
        file_path: Local path to the resume file.

    Returns:
        {status, file, size_mb, message}
    """
    try:
        return await asyncio.wait_for(
            _resume_upload(file_path),
            timeout=BROWSER_OPERATION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {"status": "error", "message": f"Resume upload timed out after {BROWSER_OPERATION_TIMEOUT}s", "error_code": "TIMEOUT"}


@mcp.tool()
async def naukri_download_resume(save_path: Optional[str] = None) -> dict:
    """Download current resume from Naukri profile.

    Args:
        save_path: Local path to save the downloaded resume file.
                   If not provided, returns an error prompting for a path.

    Returns:
        {status, file_path, file_size_bytes, message}
    """
    if not save_path:
        return {"status": "error", "message": "save_path is required — provide a local file path to save the resume.", "error_code": "VALIDATION_ERROR"}
    return await _resume_download(save_path)


@mcp.tool()
async def naukri_photo_info() -> dict:
    """Get profile photo metadata — URL, format, upload date.

    Returns:
        {status, has_photo, photo_url, format, status_label, upload_date, download_api}
    """
    return await _photo_info()


@mcp.tool()
async def naukri_upload_photo(file_path: str) -> dict:
    """Upload a new profile photo to Naukri (PNG/JPG/JPEG/GIF).

    Uses browser automation to upload the file via the profile page photo cropper.

    Args:
        file_path: Local path to the photo file.

    Returns:
        {status, file, message}
    """
    try:
        return await asyncio.wait_for(
            _photo_upload(file_path),
            timeout=BROWSER_OPERATION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {"status": "error", "message": f"Photo upload timed out after {BROWSER_OPERATION_TIMEOUT}s", "error_code": "TIMEOUT"}


@mcp.tool()
async def naukri_delete_photo() -> dict:
    """Delete the current profile photo from Naukri.

    Uses browser automation to remove the photo via the profile page photo cropper.

    Returns:
        {status, message}
    """
    try:
        return await asyncio.wait_for(
            _photo_delete(),
            timeout=BROWSER_OPERATION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {"status": "error", "message": f"Photo delete timed out after {BROWSER_OPERATION_TIMEOUT}s", "error_code": "TIMEOUT"}
