"""Resume and photo tools — download URLs and metadata."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.config import PROFILE_API, PHOTO_API, RESUME_DOWNLOAD_API, NAUKRI_BASE


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
