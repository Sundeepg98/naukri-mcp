"""Deep unit tests for naukri_server.tools.resume_photo — naukri_profile_media.

Every test is PURE: no network, no browser, no file I/O.
Uses unittest.mock.patch with AsyncMock, patched at the source module.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from contextlib import asynccontextmanager


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _fake_page_pool(mock_page):
    """Return an async context manager that yields mock_page."""
    @asynccontextmanager
    async def _acquire():
        yield mock_page
    return _acquire


def _setup_path_validation(mock_path_cls, mock_path):
    """Configure mock Path class so the path validation check passes.

    The source code calls Path.home() and Path.cwd() and checks that
    str(path) starts with one of them. Set up consistent string representations.
    """
    mock_path.resolve.return_value = mock_path
    mock_path.__str__ = lambda self: "/home/user/some/file"
    home_mock = MagicMock()
    home_mock.__str__ = lambda self: "/home/user"
    mock_path_cls.home.return_value = home_mock
    cwd_mock = MagicMock()
    cwd_mock.__str__ = lambda self: "/home/user/cwd"
    mock_path_cls.cwd.return_value = cwd_mock


# ===========================================================================
# 1. media_type validation
# ===========================================================================

class TestMediaTypeValidation:
    """Invalid media_type values must produce VALIDATION_ERROR immediately."""

    @pytest.mark.asyncio
    async def test_unknown_media_type_returns_validation_error(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="video")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "video" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_media_type_returns_validation_error(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_uppercase_media_type_returns_validation_error(self):
        """media_type matching is case-sensitive; 'RESUME' is not valid."""
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="RESUME")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_valid_types_listed_in_error_message(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="document")
        assert "photo" in result["message"]
        assert "resume" in result["message"]


# ===========================================================================
# 2. action validation (valid media_type, invalid action)
# ===========================================================================

class TestActionValidation:
    """Valid media_type but unknown action must produce VALIDATION_ERROR."""

    @pytest.mark.asyncio
    async def test_resume_invalid_action_returns_validation_error(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "delete" in result["message"]
        assert "resume" in result["message"]

    @pytest.mark.asyncio
    async def test_photo_invalid_action_returns_validation_error(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="download")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "download" in result["message"]
        assert "photo" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_action_message_lists_valid_actions(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="bogus")
        # Valid resume actions are info, download, upload
        assert "info" in result["message"] or "download" in result["message"] or "upload" in result["message"]


# ===========================================================================
# 3. resume/info routing
# ===========================================================================

class TestResumeInfoRouting:
    """resume/info must delegate to _resume_info and return its result."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._resume_info", new_callable=AsyncMock)
    async def test_resume_info_routes_to_helper(self, mock_info):
        mock_info.return_value = {
            "status": "success",
            "file_name": "my_cv.pdf",
            "cv_id": "abc123",
        }
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="info")
        mock_info.assert_awaited_once()
        assert result["status"] == "success"
        assert result["file_name"] == "my_cv.pdf"


# ===========================================================================
# 4. resume/download validation and routing
# ===========================================================================

class TestResumeDownload:
    """resume/download requires save_path; missing it returns VALIDATION_ERROR."""

    @pytest.mark.asyncio
    async def test_resume_download_missing_save_path_returns_validation_error(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="download")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "save_path" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._resume_download", new_callable=AsyncMock)
    async def test_resume_download_routes_to_helper(self, mock_download):
        mock_download.return_value = {
            "status": "success",
            "file_path": "/tmp/cv.pdf",
            "file_size_bytes": 12345,
        }
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(
            media_type="resume", action="download", save_path="/tmp/cv.pdf"
        )
        mock_download.assert_awaited_once_with("/tmp/cv.pdf")
        assert result["status"] == "success"
        assert result["file_path"] == "/tmp/cv.pdf"


# ===========================================================================
# 5. resume/upload validation — missing file_path, not found, bad format, too large
# ===========================================================================

class TestResumeUpload:
    """resume/upload validates file_path, existence, format, and size."""

    @pytest.mark.asyncio
    async def test_resume_upload_missing_file_path_returns_validation_error(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="upload")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "file_path" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_upload_file_not_found_returns_not_found(self):
        from naukri_server.tools.resume_photo import _resume_upload
        # Patch Path.exists to return False so no real FS access
        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_path.suffix.lower.return_value = ".pdf"
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)
            result = await _resume_upload("/nonexistent/cv.pdf")
        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_resume_upload_unsupported_format_returns_validation_error(self):
        from naukri_server.tools.resume_photo import _resume_upload
        # Use type(mock).suffix = PropertyMock so path.suffix is a real string,
        # meaning .lower() returns the correct lowercase value for the set check
        # and the f-string interpolates a readable value.
        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            type(mock_path).suffix = PropertyMock(return_value=".txt")
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)
            result = await _resume_upload("/some/file.txt")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert ".txt" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_upload_file_too_large_returns_validation_error(self):
        from naukri_server.tools.resume_photo import _resume_upload, RESUME_ALLOWED_FORMATS
        from naukri_server.config import RESUME_MAX_SIZE_MB

        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.suffix.lower.return_value = ".pdf"
            # Simulate file larger than max allowed
            mock_stat = MagicMock()
            mock_stat.st_size = int((RESUME_MAX_SIZE_MB + 1) * 1024 * 1024)
            mock_path.stat.return_value = mock_stat
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)
            result = await _resume_upload("/big/file.pdf")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "too large" in result["message"].lower() or "MB" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_upload_routes_to_browser_when_valid(self):
        """When all validations pass, _resume_upload enters page_pool."""
        from naukri_server.tools.resume_photo import _resume_upload
        from naukri_server.config import RESUME_MAX_SIZE_MB

        mock_page = MagicMock()
        mock_page.url = "https://www.naukri.com/mnjuser/profile"
        mock_page.query_selector = AsyncMock(return_value=MagicMock(
            set_input_files=AsyncMock()
        ))

        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls, \
             patch("naukri_server.tools.resume_photo.browser") as mock_browser, \
             patch("naukri_server.tools.resume_photo.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.resume_photo.asyncio.sleep", new_callable=AsyncMock):

            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.suffix.lower.return_value = ".pdf"
            mock_stat = MagicMock()
            mock_stat.st_size = int(0.5 * 1024 * 1024)  # 0.5 MB — under limit
            mock_path.stat.return_value = mock_stat
            mock_path.name = "cv.pdf"
            mock_path.resolve.return_value = mock_path
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)

            mock_browser.page_pool.acquire = _fake_page_pool(mock_page)

            result = await _resume_upload("/resolved/cv.pdf")

        assert result["status"] == "success"
        assert result["file"] == "cv.pdf"

    @pytest.mark.asyncio
    async def test_resume_upload_auth_error_when_redirected_to_login(self):
        """If page redirects to /nlogin, return AUTH_ERROR."""
        from naukri_server.tools.resume_photo import _resume_upload
        from naukri_server.config import RESUME_MAX_SIZE_MB

        mock_page = MagicMock()
        mock_page.url = "https://www.naukri.com/nlogin/login"

        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls, \
             patch("naukri_server.tools.resume_photo.browser") as mock_browser, \
             patch("naukri_server.tools.resume_photo.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.resume_photo.asyncio.sleep", new_callable=AsyncMock):

            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.suffix.lower.return_value = ".pdf"
            mock_stat = MagicMock()
            mock_stat.st_size = int(1 * 1024 * 1024)
            mock_path.stat.return_value = mock_stat
            mock_path.name = "cv.pdf"
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)

            mock_browser.page_pool.acquire = _fake_page_pool(mock_page)

            result = await _resume_upload("/some/cv.pdf")

        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR"
        assert "not logged in" in result["message"].lower()


# ===========================================================================
# 6. photo/info routing
# ===========================================================================

class TestPhotoInfoRouting:
    """photo/info must delegate to _photo_info and return its result."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._photo_info", new_callable=AsyncMock)
    async def test_photo_info_routes_to_helper(self, mock_info):
        mock_info.return_value = {
            "status": "success",
            "has_photo": True,
            "photo_url": "https://img.naukri.com/photo.jpg",
        }
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="info")
        mock_info.assert_awaited_once()
        assert result["status"] == "success"
        assert result["has_photo"] is True


# ===========================================================================
# 7. photo/upload validation — missing file_path, not found, unsupported format
# ===========================================================================

class TestPhotoUpload:
    """photo/upload validates file_path, existence, and format."""

    @pytest.mark.asyncio
    async def test_photo_upload_missing_file_path_returns_validation_error(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="upload")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "file_path" in result["message"]

    @pytest.mark.asyncio
    async def test_photo_upload_file_not_found_returns_not_found(self):
        from naukri_server.tools.resume_photo import _photo_upload
        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)
            result = await _photo_upload("/missing/photo.jpg")
        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_photo_upload_unsupported_format_returns_validation_error(self):
        from naukri_server.tools.resume_photo import _photo_upload
        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            type(mock_path).suffix = PropertyMock(return_value=".bmp")
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)
            result = await _photo_upload("/some/image.bmp")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert ".bmp" in result["message"]

    @pytest.mark.asyncio
    async def test_photo_upload_auth_error_when_redirected_to_login(self):
        """If browser lands on /nlogin after navigation, return AUTH_ERROR."""
        from naukri_server.tools.resume_photo import _photo_upload

        mock_page = MagicMock()
        mock_page.url = "https://www.naukri.com/nlogin/login"
        mock_page.evaluate = AsyncMock(return_value=None)

        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls, \
             patch("naukri_server.tools.resume_photo.browser") as mock_browser, \
             patch("naukri_server.tools.resume_photo.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.resume_photo.asyncio.sleep", new_callable=AsyncMock):

            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.suffix.lower.return_value = ".jpg"
            mock_path.name = "me.jpg"
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)

            mock_browser.page_pool.acquire = _fake_page_pool(mock_page)

            result = await _photo_upload("/some/me.jpg")

        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR"

    @pytest.mark.asyncio
    async def test_photo_upload_routes_to_browser_when_valid(self):
        """Valid file goes through browser; successful upload returns success."""
        from naukri_server.tools.resume_photo import _photo_upload

        mock_file_input = MagicMock()
        mock_file_input.set_input_files = AsyncMock()

        mock_page = MagicMock()
        mock_page.url = "https://www.naukri.com/mnjuser/profile"
        mock_page.evaluate = AsyncMock(return_value=None)
        mock_page.query_selector = AsyncMock(return_value=mock_file_input)

        save_btn = MagicMock()
        save_btn.click = AsyncMock()
        # First query_selector call for #fileUpload returns the input;
        # second for the save button returns the save btn.
        mock_page.query_selector = AsyncMock(side_effect=[
            mock_file_input,  # #fileUpload
            save_btn,         # save button
        ])

        with patch("naukri_server.tools.resume_photo.Path") as mock_path_cls, \
             patch("naukri_server.tools.resume_photo.browser") as mock_browser, \
             patch("naukri_server.tools.resume_photo.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.resume_photo.asyncio.sleep", new_callable=AsyncMock):

            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.suffix.lower.return_value = ".jpg"
            mock_path.name = "me.jpg"
            mock_path.resolve.return_value = mock_path
            mock_path_cls.return_value = mock_path
            _setup_path_validation(mock_path_cls, mock_path)

            mock_browser.page_pool.acquire = _fake_page_pool(mock_page)

            result = await _photo_upload("/resolved/me.jpg")

        assert result["status"] == "success"
        assert result["file"] == "me.jpg"


# ===========================================================================
# 8. photo/delete routing
# ===========================================================================

class TestPhotoDelete:
    """photo/delete routes correctly and handles no-photo edge case."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._photo_delete", new_callable=AsyncMock)
    async def test_photo_delete_routes_to_helper(self, mock_delete):
        mock_delete.return_value = {
            "status": "success",
            "action": "deleted",
            "message": "Profile photo deleted successfully.",
        }
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="delete")
        mock_delete.assert_awaited_once()
        assert result["status"] == "success"
        assert result["action"] == "deleted"

    @pytest.mark.asyncio
    async def test_photo_delete_returns_no_photo_when_none_found(self):
        """If the browser finds no photo, return no_photo status."""
        from naukri_server.tools.resume_photo import _photo_delete

        mock_page = MagicMock()
        mock_page.url = "https://www.naukri.com/mnjuser/profile"
        # First evaluate call = has_photo check → False (no photo on page)
        mock_page.evaluate = AsyncMock(return_value=False)

        with patch("naukri_server.tools.resume_photo.browser") as mock_browser, \
             patch("naukri_server.tools.resume_photo.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.resume_photo.asyncio.sleep", new_callable=AsyncMock):

            mock_browser.page_pool.acquire = _fake_page_pool(mock_page)
            result = await _photo_delete()

        assert result["status"] == "no_photo"
        assert "no profile photo" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_photo_delete_auth_error_when_not_logged_in(self):
        """Redirect to /nlogin during delete returns AUTH_ERROR."""
        from naukri_server.tools.resume_photo import _photo_delete

        mock_page = MagicMock()
        mock_page.url = "https://www.naukri.com/nlogin/login"

        with patch("naukri_server.tools.resume_photo.browser") as mock_browser, \
             patch("naukri_server.tools.resume_photo.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.resume_photo.asyncio.sleep", new_callable=AsyncMock):

            mock_browser.page_pool.acquire = _fake_page_pool(mock_page)
            result = await _photo_delete()

        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR"


# ===========================================================================
# 9. _resume_info — API data extraction
# ===========================================================================

class TestResumeInfoHelper:
    """_resume_info correctly extracts fields from the profile API response."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo.api_client.get", new_callable=AsyncMock)
    async def test_resume_info_extracts_fields(self, mock_api_get):
        mock_api_get.return_value = {
            "profile": [
                {
                    "resumeHeadline": "Senior Engineer",
                    "cvInfo": {
                        "cvName": "My_CV.pdf",
                        "cvUploadDate": "2024-01-15",
                        "cvSize": "512KB",
                        "cvId": "cv-999",
                    },
                }
            ]
        }
        from naukri_server.tools.resume_photo import _resume_info
        result = await _resume_info()
        assert result["status"] == "success"
        assert result["file_name"] == "My_CV.pdf"
        assert result["upload_date"] == "2024-01-15"
        assert result["cv_id"] == "cv-999"
        assert result["resume_headline"] == "Senior Engineer"
        assert "download_url" in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo.api_client.get", new_callable=AsyncMock)
    async def test_resume_info_handles_empty_profile(self, mock_api_get):
        mock_api_get.return_value = {"profile": []}
        from naukri_server.tools.resume_photo import _resume_info
        result = await _resume_info()
        assert result["status"] == "success"
        # Fields default to empty strings when no cv data
        assert result["file_name"] == ""
        assert result["cv_id"] == ""


# ===========================================================================
# 10. _photo_info — API data extraction
# ===========================================================================

class TestPhotoInfoHelper:
    """_photo_info correctly extracts photo fields from the profile API response."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo.api_client.get", new_callable=AsyncMock)
    async def test_photo_info_extracts_fields(self, mock_api_get):
        mock_api_get.return_value = {
            "profile": [
                {
                    "photoInfo": {
                        "isAvailable": True,
                        "photoURL": "https://img.naukri.com/photo.jpg",
                        "photoFormat": "JPEG",
                        "status": "APPROVED",
                        "uploadDate": "2024-02-10",
                    }
                }
            ]
        }
        from naukri_server.tools.resume_photo import _photo_info
        result = await _photo_info()
        assert result["status"] == "success"
        assert result["has_photo"] is True
        assert result["photo_url"] == "https://img.naukri.com/photo.jpg"
        assert result["format"] == "JPEG"
        assert result["status_label"] == "APPROVED"
        assert "download_api" in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo.api_client.get", new_callable=AsyncMock)
    async def test_photo_info_no_photo(self, mock_api_get):
        mock_api_get.return_value = {
            "profile": [{"photoInfo": {"isAvailable": False}}]
        }
        from naukri_server.tools.resume_photo import _photo_info
        result = await _photo_info()
        assert result["status"] == "success"
        assert result["has_photo"] is False
        assert result["photo_url"] == ""


# ===========================================================================
# 11. Action routing — all valid combinations
# ===========================================================================

class TestAllValidRoutingCombinations:
    """Each (media_type, action) pair is dispatched to the correct private helper."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._resume_download", new_callable=AsyncMock)
    async def test_resume_download_combination(self, mock_fn):
        mock_fn.return_value = {"status": "success", "file_path": "/tmp/r.pdf"}
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(
            media_type="resume", action="download", save_path="/tmp/r.pdf"
        )
        mock_fn.assert_awaited_once()
        assert result["status"] == "success"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._resume_upload", new_callable=AsyncMock)
    async def test_resume_upload_combination(self, mock_fn):
        mock_fn.return_value = {"status": "success", "file": "cv.pdf"}
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(
            media_type="resume", action="upload", file_path="/local/cv.pdf"
        )
        mock_fn.assert_awaited_once_with("/local/cv.pdf")
        assert result["status"] == "success"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._photo_upload", new_callable=AsyncMock)
    async def test_photo_upload_combination(self, mock_fn):
        mock_fn.return_value = {"status": "success", "file": "me.jpg"}
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(
            media_type="photo", action="upload", file_path="/local/me.jpg"
        )
        mock_fn.assert_awaited_once_with("/local/me.jpg")
        assert result["status"] == "success"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.resume_photo._photo_delete", new_callable=AsyncMock)
    async def test_photo_delete_combination(self, mock_fn):
        mock_fn.return_value = {
            "status": "success",
            "action": "deleted",
            "message": "Photo deleted.",
        }
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="delete")
        mock_fn.assert_awaited_once()
        assert result["action"] == "deleted"


# =====================================================================
# From test_consolidation.py — profile media action routing & validation
# =====================================================================

class TestProfileMediaConsolidation:
    """Tests for naukri_server.tools.resume_photo.naukri_profile_media."""

    @pytest.mark.asyncio
    async def test_invalid_media_type(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="video")
        assert result["status"] == "error"
        assert "Unknown media_type" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_invalid_action(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_photo_invalid_action(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="download")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_download_requires_save_path(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="download")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "save_path" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_upload_requires_file_path(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="upload")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "file_path" in result["message"]

    @pytest.mark.asyncio
    async def test_photo_upload_requires_file_path(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="upload")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "file_path" in result["message"]
