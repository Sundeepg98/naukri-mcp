"""Resume upload tool."""

import asyncio
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser
from naukri_server.config import NAUKRI_BASE, logger

ALLOWED_FORMATS = {".pdf", ".doc", ".docx"}
MAX_SIZE_MB = 5


@mcp.tool()
async def naukri_upload_resume(file_path: str) -> dict:
    """Upload a resume file to your Naukri profile.

    Supports PDF, DOC, DOCX formats up to 5MB.
    Uses browser automation to upload through Naukri's profile page.

    Args:
        file_path: Absolute path to the resume file

    Returns:
        - {status: "uploaded", file, message}
        - {status: "error", message}
    """
    path = Path(file_path)

    # Validate file exists
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    # Validate format
    if path.suffix.lower() not in ALLOWED_FORMATS:
        return {"status": "error", "message": f"Unsupported format '{path.suffix}'. Use: {', '.join(ALLOWED_FORMATS)}"}

    # Validate size
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_SIZE_MB:
        return {"status": "error", "message": f"File too large ({size_mb:.1f}MB). Max: {MAX_SIZE_MB}MB"}

    async with browser._lock:
        try:
            await browser.goto(f"{NAUKRI_BASE}/mnjuser/profile")
            await asyncio.sleep(3)

            if "/nlogin" in browser.page.url:
                return {"status": "error", "message": "Not logged in. Call naukri_login first."}

            # Find the resume upload input
            file_input = await browser.page.query_selector('input[type="file"]')
            if not file_input:
                # Try clicking an upload button first to reveal the file input
                upload_btn = await browser.page.query_selector(
                    '[class*="upload"] button, button:has-text("Upload"), '
                    '[class*="resume"] button, [class*="Resume"] [class*="edit"]'
                )
                if upload_btn:
                    await upload_btn.click()
                    await asyncio.sleep(2)
                    file_input = await browser.page.query_selector('input[type="file"]')

            if not file_input:
                return {"status": "error", "message": "Could not find file upload input on profile page"}

            # Upload the file
            await file_input.set_input_files(str(path.resolve()))
            await asyncio.sleep(5)  # Wait for upload to complete

            return {
                "status": "uploaded",
                "file": path.name,
                "size_mb": round(size_mb, 2),
                "message": f"Resume '{path.name}' uploaded to Naukri profile.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Upload failed: {type(e).__name__}: {e}"}
