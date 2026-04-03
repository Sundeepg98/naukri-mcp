"""Profile media management — unified tool for resume and photo operations."""

import asyncio
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_tool
from naukri_server.browser import browser, page_goto
from naukri_server.config import PROFILE_API, PHOTO_API, RESUME_DOWNLOAD_API, NAUKRI_BASE, RESUME_MAX_SIZE_MB, logger
from naukri_server.tools.profile import _profile_ttl_cache, _dashboard_ttl_cache

PHOTO_ALLOWED_FORMATS = {".png", ".jpg", ".jpeg", ".gif"}
RESUME_ALLOWED_FORMATS = {".pdf", ".doc", ".docx"}

_VALID_ACTIONS = {
    "resume": {"info", "download", "upload"},
    "photo": {"info", "upload", "delete"},
}


@mcp.tool()
async def naukri_profile_media(
    media_type: str,
    action: str = "info",
    file_path: Optional[str] = None,
    save_path: Optional[str] = None,
) -> dict:
    """Unified resume and photo management — info, download, upload, delete.

    Note: Uses two-level dispatch — 'media_type' selects the resource (resume or photo),
    then 'action' selects the operation (info, upload, download, delete). This avoids
    combinatorial explosion of action values like "resume_info", "photo_upload", etc.

    Combines former naukri_resume + naukri_photo into one tool.

    media_type="resume" actions:
      - "info": Get resume filename, upload date, download URL
      - "download": Save resume to local file (requires save_path)
      - "upload": Upload new resume (requires file_path; PDF/DOC/DOCX, max 5MB)

    media_type="photo" actions:
      - "info": Get current photo URL and dimensions
      - "upload": Upload new profile photo (requires file_path; PNG/JPG/JPEG/GIF)
      - "delete": Remove current profile photo

    Args:
        media_type: "resume" | "photo"
        action: Depends on media_type — see above
        file_path: Local file to upload (for upload actions)
        save_path: Local path to save downloaded resume (for resume download)

    Returns:
        - resume/info: {status, resume_headline, file_name, upload_date, file_size, download_url, cv_id}
        - resume/download: {status, file_path, file_size_bytes, message}
        - resume/upload: {status, file, size_mb, message}
        - photo/info: {status, has_photo, photo_url, format, status_label, upload_date, download_api}
        - photo/upload: {status, file, message}
        - photo/delete: {status, message}
        - {status: "error", message} on failure
    """
    # ── validate media_type ───────────────────────────────────────────
    if media_type not in _VALID_ACTIONS:
        valid_types = ", ".join(sorted(_VALID_ACTIONS))
        return {"status": "error", "message": f"Unknown media_type '{media_type}'. Use: {valid_types}", "error_code": "VALIDATION_ERROR"}

    # ── validate action for the chosen media_type ─────────────────────
    valid = _VALID_ACTIONS[media_type]
    if action not in valid:
        return {"status": "error", "message": f"Unknown action '{action}' for media_type '{media_type}'. Use: {', '.join(sorted(valid))}", "error_code": "VALIDATION_ERROR"}

    # ── resume actions ────────────────────────────────────────────────
    if media_type == "resume":
        if action == "info":
            return await _resume_info()
        elif action == "download":
            if not save_path:
                return {"status": "error", "message": "download requires save_path.", "error_code": "VALIDATION_ERROR"}
            return await _resume_download(save_path)
        elif action == "upload":
            if not file_path:
                return {"status": "error", "message": "upload requires file_path.", "error_code": "VALIDATION_ERROR"}
            return await _resume_upload(file_path)

    # ── photo actions ─────────────────────────────────────────────────
    elif media_type == "photo":
        if action == "info":
            return await _photo_info()
        elif action == "upload":
            if not file_path:
                return {"status": "error", "message": "upload requires file_path.", "error_code": "VALIDATION_ERROR"}
            return await _photo_upload(file_path)
        elif action == "delete":
            return await _photo_delete()


# ---------------------------------------------------------------------------
# Private helpers — resume
# ---------------------------------------------------------------------------


@api_tool("Get resume info")
async def _resume_info() -> dict:
    data = await api_get(PROFILE_API, params={"expand_level": "4"})

    profiles = data.get("profile", [])
    profile = profiles[0] if isinstance(profiles, list) and profiles else data.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}

    cv_info = profile.get("cvInfo", {})
    if not isinstance(cv_info, dict):
        cv_info = {}

    return {
        "status": "success",
        "resume_headline": profile.get("resumeHeadline", ""),
        "file_name": cv_info.get("cvName", cv_info.get("fileName", "")),
        "upload_date": cv_info.get("cvUploadDate", cv_info.get("uploadDate", "")),
        "file_size": cv_info.get("cvSize", cv_info.get("fileSize", "")),
        "download_url": f"{NAUKRI_BASE}{RESUME_DOWNLOAD_API}",
        "cv_id": cv_info.get("cvId", cv_info.get("id", "")),
    }


async def _resume_download(save_path: str) -> dict:
    from naukri_server.api import get_session
    from naukri_server.config import API_HEADERS

    try:
        token = await browser.token_manager.ensure_token()
        cookie_str = browser.token_manager.get_cookies()

        session = await get_session()
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
            await asyncio.sleep(3)

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
                    await asyncio.sleep(2)
                    file_input = await page.query_selector('input[type="file"]')

            if not file_input:
                return {"status": "error", "message": "Could not find file upload input on profile page", "error_code": "BROWSER_ERROR"}

            # Upload the file
            await file_input.set_input_files(str(path.resolve()))
            await asyncio.sleep(5)  # Wait for upload to complete

            _profile_ttl_cache.invalidate()
            _dashboard_ttl_cache.invalidate()
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
    data = await api_get(PROFILE_API, params={"expand_level": "4"})

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
            await asyncio.sleep(3)

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
            await asyncio.sleep(3)

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
            await asyncio.sleep(3)

            # Click the save/submit button
            save_btn = await page.query_selector(
                '#submit, button.save-photo, #photoCropper button[type="submit"], '
                '#photoCropper button:has-text("Save"), '
                'button:has-text("Save photo"), button:has-text("Apply")'
            )
            if save_btn:
                await save_btn.click()
                logger.info("Clicked save button for photo upload")
                await asyncio.sleep(5)  # Wait for upload to complete
            else:
                # Some flows auto-upload without a save button; wait for network idle
                logger.info("No save button found, waiting for auto-upload")
                await asyncio.sleep(5)

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
            await asyncio.sleep(3)

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
                await asyncio.sleep(1)
                modal_appeared = await page.evaluate("""() => {
                    const modal = document.querySelector('#photoCropper, [class*="photoCropper"], [class*="PhotoCropper"], [class*="cropModal"], [class*="photo-modal"], [class*="modal"][class*="photo"], [role="dialog"]');
                    return !!modal;
                }""")
                if modal_appeared:
                    break

            if not modal_appeared:
                return {"status": "error", "message": "Photo cropper modal did not appear after clicking photo.", "error_code": "BROWSER_ERROR"}

            await asyncio.sleep(1)  # Let modal fully render

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
            await asyncio.sleep(2)
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
            await asyncio.sleep(5)  # Wait for deletion to complete

            return {
                "status": "success",
                "action": "deleted",
                "message": "Profile photo deleted successfully.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Photo deletion failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
