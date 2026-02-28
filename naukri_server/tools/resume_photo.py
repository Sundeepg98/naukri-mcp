"""Resume and photo tools — download URLs, metadata, and photo upload."""

import asyncio
from pathlib import Path

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.browser import browser
from naukri_server.config import PROFILE_API, PHOTO_API, RESUME_DOWNLOAD_API, NAUKRI_BASE, logger

PHOTO_ALLOWED_FORMATS = {".png", ".jpg", ".jpeg", ".gif"}


@mcp.tool()
async def naukri_get_resume_info() -> dict:
    """Get your uploaded resume details — file name, upload date, headline, and download URL.

    Returns:
        - {status: "success", resume_headline, file_name, upload_date, download_url, cv_id}
        - {status: "error", message}
    """
    try:
        data = await api_get(PROFILE_API, params={"expand_level": "4"})

        # Extract profile info
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
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get resume info: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_photo_info() -> dict:
    """Get your profile photo details — URL, dimensions, and upload status.

    Returns:
        - {status: "success", has_photo, photo_url, ...}
        - {status: "error", message}
    """
    try:
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
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get photo info: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_upload_photo(file_path: str) -> dict:
    """Upload a profile photo to Naukri. Supports PNG, JPG, JPEG, GIF formats.

    Uses browser automation to upload through Naukri's profile page.

    Args:
        file_path: Absolute path to the photo file

    Returns:
        - {status: "uploaded", file, message}
        - {status: "error", message}
    """
    path = Path(file_path)

    # Validate file exists
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    # Validate format
    if path.suffix.lower() not in PHOTO_ALLOWED_FORMATS:
        return {
            "status": "error",
            "message": f"Unsupported format '{path.suffix}'. Use: {', '.join(PHOTO_ALLOWED_FORMATS)}",
        }

    async with browser._lock:
        try:
            await browser.goto(f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(3)

            if "/nlogin" in browser.page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first."}

            # Try clicking on the photo area to reveal upload controls
            photo_area = await browser.page.query_selector(
                '#photoCropper, [class*="photoWrapper"], [class*="photo-wrapper"], '
                '[class*="profilePhoto"], [class*="profile-photo"], .avatarEdit'
            )
            if photo_area:
                await photo_area.click()
                await asyncio.sleep(2)

            # Try clicking a "Replace photo" / "Update photo" / edit button
            edit_btn = await browser.page.query_selector(
                'button:has-text("Replace"), button:has-text("Update photo"), '
                'button:has-text("Edit photo"), button:has-text("Upload photo"), '
                '[class*="photo"] button, [class*="photo"] [class*="edit"], '
                '.avatarEdit button'
            )
            if edit_btn:
                await edit_btn.click()
                await asyncio.sleep(2)

            # Set the file on the hidden file input
            file_input = await browser.page.query_selector('#fileUpload')
            if not file_input:
                # Fallback: look for any file input inside the photo cropper area
                file_input = await browser.page.query_selector(
                    '#photoCropper input[type="file"], '
                    '[class*="photo"] input[type="file"], '
                    'input[type="file"][accept*="image"]'
                )
            if not file_input:
                # Last resort: any file input on the page
                file_input = await browser.page.query_selector('input[type="file"]')

            if not file_input:
                return {"status": "error", "message": "Could not find photo upload input on profile page"}

            await file_input.set_input_files(str(path.resolve()))
            logger.info("Photo file set: %s", path.name)

            # Wait for the image to load in the cropper preview
            await asyncio.sleep(3)

            # Click the save/submit button
            save_btn = await browser.page.query_selector(
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
                "status": "uploaded",
                "file": path.name,
                "message": f"Photo '{path.name}' uploaded to Naukri profile.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Photo upload failed: {type(e).__name__}: {e}"}


# ============================================================================
# Tool: Delete Profile Photo (Browser automation)
# ============================================================================


@mcp.tool()
async def naukri_delete_photo() -> dict:
    """Delete your profile photo from Naukri. Uses browser automation.

    Navigates to the profile page, opens the photo cropper modal,
    and clicks the delete button to remove the current profile photo.

    Returns:
        - {status: "deleted", message}
        - {status: "no_photo", message}
        - {status: "error", message}
    """
    async with browser._lock:
        try:
            await browser.goto(f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(3)

            if "/nlogin" in browser.page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first."}

            # Check if a photo exists on the profile page
            has_photo = await browser.page.evaluate("""() => {
                const img = document.querySelector(
                    '.photoWrap img, [class*="photo"] img, [class*="avatar"] img, '
                    '[class*="profilePhoto"] img'
                );
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
            photo_clicked = await browser.page.evaluate("""() => {
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
                return {"status": "error", "message": "Could not find the profile photo element to click."}

            logger.info("Clicked photo element: %s", photo_clicked)

            # Wait for the photo cropper modal to appear
            modal_appeared = False
            for _ in range(10):
                await asyncio.sleep(1)
                modal_appeared = await browser.page.evaluate("""() => {
                    const modal = document.querySelector(
                        '#photoCropper, [class*="photoCropper"], [class*="PhotoCropper"], '
                        '[class*="cropModal"], [class*="photo-modal"], '
                        '[class*="modal"][class*="photo"], [role="dialog"]'
                    );
                    return !!modal;
                }""")
                if modal_appeared:
                    break

            if not modal_appeared:
                return {"status": "error", "message": "Photo cropper modal did not appear after clicking photo."}

            await asyncio.sleep(1)  # Let modal fully render

            # Click the delete button inside the modal
            delete_clicked = await browser.page.evaluate("""() => {
                const selectors = [
                    'button.delBtn',
                    '.typ-14Bold.delBtn',
                    '.delBtn',
                    'button.btn-dark-ot',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { el.click(); return sel; }
                }
                // Fallback: find any button with delete-related text
                const buttons = Array.from(document.querySelectorAll('button, a, span'));
                for (const btn of buttons) {
                    const text = btn.textContent.trim().toLowerCase();
                    if (text === 'delete' || text === 'delete photo' || text === 'remove' || text === 'remove photo') {
                        btn.click();
                        return 'text:' + btn.textContent.trim();
                    }
                }
                return null;
            }""")

            if not delete_clicked:
                return {"status": "error", "message": "Could not find the delete button in the photo modal."}

            logger.info("Clicked delete button: %s", delete_clicked)

            # Handle confirmation dialog (if one appears)
            await asyncio.sleep(2)
            await browser.page.evaluate("""() => {
                const confirmTexts = ['yes', 'confirm', 'delete', 'ok', 'yes, delete'];
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const text = btn.textContent.trim().toLowerCase();
                    if (confirmTexts.includes(text) && btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")

            await asyncio.sleep(3)  # Wait for deletion to complete

            return {
                "status": "deleted",
                "message": "Profile photo deleted successfully.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Photo deletion failed: {type(e).__name__}: {e}"}
