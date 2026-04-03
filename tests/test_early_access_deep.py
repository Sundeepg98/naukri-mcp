"""Deep unit tests for naukri_server.tools.early_access.

Covers:
  - Action routing: list, share, unknown
  - Validation: missing job_id for share
  - List: API error, page/limit clamping, pagination metadata, job field parsing,
    skips malformed jobs
  - Share: quota extraction, status 200 success, non-200 error, empty jobs list error
  - New role detection: _detect_new_roles side effect tracking
  - NaukriAPIError caught with http_status
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ===========================================================================
# 1. Action routing — unknown action
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_action_returns_validation_error():
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="apply")
    assert result["status"] == "error"
    assert "apply" in result["message"]
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_unknown_action_mentions_valid_actions():
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="bogus")
    assert "list" in result["message"]
    assert "share" in result["message"]


# ===========================================================================
# 2. Validation — missing job_id for share
# ===========================================================================

@pytest.mark.asyncio
async def test_share_without_job_id_returns_validation_error():
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id=None)
    assert result["status"] == "error"
    assert "job_id" in result["message"]
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_share_with_empty_job_id_returns_validation_error():
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id="")
    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"


# ===========================================================================
# 3. List action — API error (NaukriAPIError)
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_naukri_api_error_returns_http_status(mock_api_get):
    from naukri_server.api import NaukriAPIError
    mock_api_get.side_effect = NaukriAPIError(status=503, message="Service Unavailable")
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list")
    assert result["status"] == "error"
    assert result["http_status"] == 503
    assert result["error_code"] == "API_ERROR"


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_generic_exception_returns_api_error(mock_api_get):
    mock_api_get.side_effect = RuntimeError("connection reset")
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list")
    assert result["status"] == "error"
    assert result["error_code"] == "INTERNAL_ERROR"
    assert "RuntimeError" in result["message"]


# ===========================================================================
# 4. List action — page/limit clamping
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_clamps_page_below_one(mock_api_get):
    """Page values below 1 should be clamped to 1."""
    mock_api_get.return_value = {"jobDetails": [], "noOfJobs": 0}
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list", page=0, limit=10)
    assert result["status"] == "success"
    assert result["page"] == 1


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_clamps_limit_above_max(mock_api_get):
    """Limit values above 50 should be clamped to 50."""
    mock_api_get.return_value = {"jobDetails": [], "noOfJobs": 0}
    from naukri_server.tools.early_access import naukri_early_access
    # Limit=200 should be clamped — the api_get call should use "50" as noOfResults
    await naukri_early_access(action="list", page=1, limit=200)
    call_kwargs = mock_api_get.call_args
    params = call_kwargs[1]["params"] if call_kwargs[1] else call_kwargs[0][1]
    assert params["noOfResults"] == "50"


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_clamps_limit_below_one(mock_api_get):
    """Limit values below 1 should be clamped to 1."""
    mock_api_get.return_value = {"jobDetails": [], "noOfJobs": 0}
    from naukri_server.tools.early_access import naukri_early_access
    await naukri_early_access(action="list", page=1, limit=0)
    call_kwargs = mock_api_get.call_args
    params = call_kwargs[1]["params"] if call_kwargs[1] else call_kwargs[0][1]
    assert params["noOfResults"] == "1"


# ===========================================================================
# 5. List action — pagination metadata
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_has_more_true_when_more_pages_exist(mock_api_get):
    """has_more should be True when page * limit < total."""
    mock_api_get.return_value = {
        "jobDetails": [{"jobId": "1", "title": "Dev", "companyName": "ACME"}],
        "noOfJobs": 100,
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list", page=1, limit=20)
    assert result["has_more"] is True
    assert result["total"] == 100
    assert result["page"] == 1


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_has_more_false_on_last_page(mock_api_get):
    """has_more should be False when page * limit >= total."""
    mock_api_get.return_value = {
        "jobDetails": [{"jobId": "1", "title": "Dev", "companyName": "ACME"}],
        "noOfJobs": 5,
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list", page=1, limit=20)
    assert result["has_more"] is False


# ===========================================================================
# 6. List action — job field parsing
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_parses_jobdetails_fields(mock_api_get):
    """All parsed job fields are correctly extracted from jobDetails."""
    mock_api_get.return_value = {
        "jobDetails": [
            {
                "jobId": "JOB123",
                "title": "Senior Python Developer",
                "companyName": "TechCorp",
                "placeholders": [
                    {"type": "experience", "label": "3-5 years"},
                    {"type": "salary", "label": "12-18 LPA"},
                    {"type": "location", "label": "Bangalore"},
                ],
                "jobType": "Full Time",
                "tags": [{"value": "Python"}, {"value": "Django"}],
                "jdURL": "https://naukri.com/job/JOB123",
            }
        ],
        "noOfJobs": 1,
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list")
    assert result["status"] == "success"
    assert result["count"] == 1
    role = result["roles"][0]
    assert role["job_id"] == "JOB123"
    assert role["title"] == "Senior Python Developer"
    assert role["company_hint"] == "TechCorp"
    assert role["location"] == "Bangalore"
    assert role["experience"] == "3-5 years"
    assert role["salary"] == "12-18 LPA"
    assert role["job_type"] == "Full Time"
    assert "Python" in role["tags"]
    assert role["url"] == "https://naukri.com/job/JOB123"


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_falls_back_to_jobs_key(mock_api_get):
    """Should read from 'jobs' key if 'jobDetails' is absent."""
    mock_api_get.return_value = {
        "jobs": [{"jobId": "ALT1", "title": "Engineer", "companyHint": "Startup"}],
        "totalJobs": 1,
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list")
    assert result["status"] == "success"
    assert result["roles"][0]["job_id"] == "ALT1"
    assert result["roles"][0]["company_hint"] == "Startup"


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_get", new_callable=AsyncMock)
async def test_list_skips_malformed_non_dict_jobs(mock_api_get):
    """Non-dict entries in jobDetails should be silently skipped."""
    mock_api_get.return_value = {
        "jobDetails": [
            "bad_string_entry",
            None,
            42,
            {"jobId": "VALID1", "title": "QA Engineer"},
        ],
        "noOfJobs": 4,
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="list")
    assert result["status"] == "success"
    # Only the valid dict should appear
    assert result["count"] == 1
    assert result["roles"][0]["job_id"] == "VALID1"


# ===========================================================================
# 7. Share action — status 200 success
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_post", new_callable=AsyncMock)
async def test_share_success_with_quota(mock_api_post):
    """Successful share returns job_id, message, and quota details."""
    mock_api_post.return_value = {
        "jobs": [{"status": 200, "message": "Interest shared!"}],
        "quotaDetails": {"dailyApplied": 5, "dailyQuota": 50},
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id="JOB999")
    assert result["status"] == "success"
    assert result["job_id"] == "JOB999"
    assert result["message"] == "Interest shared!"
    assert result["quota"]["daily_applied"] == 5
    assert result["quota"]["daily_quota"] == 50


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_post", new_callable=AsyncMock)
async def test_share_success_quota_defaults_when_absent(mock_api_post):
    """Quota fields default to 0/50 when quotaDetails is missing."""
    mock_api_post.return_value = {
        "jobs": [{"status": 200, "message": "Done"}],
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id="JOB888")
    assert result["status"] == "success"
    assert result["quota"]["daily_applied"] == 0
    assert result["quota"]["daily_quota"] == 50


# ===========================================================================
# 8. Share action — non-200 error from API
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_post", new_callable=AsyncMock)
async def test_share_non_200_status_returns_error(mock_api_post):
    """A non-200 job status in the response should return status=error."""
    mock_api_post.return_value = {
        "jobs": [{"status": 429, "message": "Daily limit reached"}],
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id="JOB777")
    assert result["status"] == "error"
    assert "Daily limit reached" in result["message"]
    assert result["error_code"] == "API_ERROR"


@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_post", new_callable=AsyncMock)
async def test_share_non_200_no_message_uses_fallback(mock_api_post):
    """If the API returns a non-200 status without a message, a fallback is generated."""
    mock_api_post.return_value = {
        "jobs": [{"status": 403}],
    }
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id="JOB666")
    assert result["status"] == "error"
    assert "403" in result["message"]


# ===========================================================================
# 9. Share action — empty jobs list error
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_post", new_callable=AsyncMock)
async def test_share_empty_jobs_returns_api_error(mock_api_post):
    """An empty 'jobs' list in the response should produce status=error."""
    mock_api_post.return_value = {"jobs": []}
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id="JOB555")
    assert result["status"] == "error"
    assert result["error_code"] == "API_ERROR"
    assert "No response" in result["message"]


# ===========================================================================
# 10. Share action — NaukriAPIError caught with http_status
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.early_access.api_post", new_callable=AsyncMock)
async def test_share_naukri_api_error_returns_http_status(mock_api_post):
    """NaukriAPIError raised during share must be caught and expose http_status."""
    from naukri_server.api import NaukriAPIError
    mock_api_post.side_effect = NaukriAPIError(status=401, message="Unauthorized")
    from naukri_server.tools.early_access import naukri_early_access
    result = await naukri_early_access(action="share", job_id="JOB444")
    assert result["status"] == "error"
    assert result["http_status"] == 401
    assert result["error_code"] == "API_ERROR"


# ===========================================================================
# 11. _detect_new_roles — side effect tracking
# ===========================================================================

@patch("naukri_server.tools.early_access._load_seen_roles")
@patch("naukri_server.tools.early_access._save_seen_roles")
def test_detect_new_roles_identifies_new_entries(mock_save, mock_load):
    """Roles not in the seen set should be returned as new_roles."""
    mock_load.return_value = {"OLDROLE1"}
    from naukri_server.tools.early_access import _detect_new_roles
    current_roles = [
        {"job_id": "OLDROLE1", "title": "Old"},
        {"job_id": "NEWROLE2", "title": "New"},
    ]
    new_roles, total = _detect_new_roles(current_roles)
    assert total == 2
    assert len(new_roles) == 1
    assert new_roles[0]["job_id"] == "NEWROLE2"


@patch("naukri_server.tools.early_access._load_seen_roles")
@patch("naukri_server.tools.early_access._save_seen_roles")
def test_detect_new_roles_saves_merged_set(mock_save, mock_load):
    """_save_seen_roles must be called with the union of old and current IDs."""
    mock_load.return_value = {"EXISTING1"}
    from naukri_server.tools.early_access import _detect_new_roles
    current_roles = [{"job_id": "NEW2", "title": "New Role"}]
    _detect_new_roles(current_roles)
    mock_save.assert_called_once()
    saved_ids = mock_save.call_args[0][0]
    assert "EXISTING1" in saved_ids
    assert "NEW2" in saved_ids


@patch("naukri_server.tools.early_access._load_seen_roles")
@patch("naukri_server.tools.early_access._save_seen_roles")
def test_detect_new_roles_skips_entries_without_job_id(mock_save, mock_load):
    """Entries with no job_id must be ignored (not counted or saved)."""
    mock_load.return_value = set()
    from naukri_server.tools.early_access import _detect_new_roles
    current_roles = [
        {"job_id": "", "title": "Missing ID"},
        {"title": "No ID key at all"},
        {"job_id": "VALID3", "title": "Valid"},
    ]
    new_roles, total = _detect_new_roles(current_roles)
    # Only VALID3 should be counted
    assert total == 1
    assert len(new_roles) == 1
    saved_ids = mock_save.call_args[0][0]
    assert "VALID3" in saved_ids
    assert "" not in saved_ids


@patch("naukri_server.tools.early_access._load_seen_roles")
@patch("naukri_server.tools.early_access._save_seen_roles")
def test_detect_new_roles_all_seen_returns_empty_new(mock_save, mock_load):
    """When all roles are already seen, new_roles should be empty."""
    mock_load.return_value = {"ROLE1", "ROLE2"}
    from naukri_server.tools.early_access import _detect_new_roles
    current_roles = [
        {"job_id": "ROLE1", "title": "First"},
        {"job_id": "ROLE2", "title": "Second"},
    ]
    new_roles, total = _detect_new_roles(current_roles)
    assert total == 2
    assert new_roles == []
