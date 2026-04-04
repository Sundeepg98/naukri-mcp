"""Deep unit tests for naukri_server.tools.resume_builder.

Covers:
  - Action routing: templates, status, tailor, unknown
  - Validation: missing job_id for tailor
  - Templates: data envelope unwrapping, free/pro counting, type 1 = free
  - Status: data envelope unwrapping, key field extraction
  - Tailor: delegation to _tailor_resume, error propagation
  - API error handling: NaukriAPIError caught for templates/status

All tests are pure — no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_api_error(status: int = 500, message: str = "Server error"):
    """Create a NaukriAPIError instance without importing the class in module scope."""
    from naukri_server.api import NaukriAPIError
    return NaukriAPIError(status=status, message=message)


# ===========================================================================
# 1. Action routing — unknown action
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_action_returns_error():
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="nonexistent")
    assert result["status"] == "error"
    assert "Unknown action" in result["message"]
    assert "nonexistent" in result["message"]
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_unknown_action_mentions_valid_actions():
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="refresh")
    assert "templates" in result["message"]
    assert "status" in result["message"]
    assert "tailor" in result["message"]


# ===========================================================================
# 2. Validation — tailor requires job_id
# ===========================================================================

@pytest.mark.asyncio
async def test_tailor_missing_job_id_returns_validation_error():
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="tailor")
    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"
    assert "job_id" in result["message"]


@pytest.mark.asyncio
async def test_tailor_empty_string_job_id_returns_validation_error():
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="tailor", job_id="")
    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"


# ===========================================================================
# 3. Templates — data envelope unwrapping & counts
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_templates_unwraps_data_envelope(mock_api_get):
    """Response wrapped as {data: {templateConfiguration: {templateDetails: [...]}}}."""
    mock_api_get.return_value = {
        "statusCode": 200,
        "data": {
            "templateConfiguration": {
                "templateDetails": [
                    {"id": "t1", "name": "Classic", "type": 1, "icons": {"variant1": "https://cdn/t1.png"}},
                    {"id": "t2", "name": "Modern",  "type": 2, "icons": {"variant1": "https://cdn/t2.png"}},
                ]
            }
        }
    }
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="templates")
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["free_count"] == 1   # type 1 → free
    assert result["pro_count"] == 1    # type 2 → pro


@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_templates_type1_is_free_type2_is_pro(mock_api_get):
    """Type == 1 must map to 'free'; anything else maps to 'pro'."""
    mock_api_get.return_value = {
        "data": {
            "templateConfiguration": {
                "templateDetails": [
                    {"id": "a", "name": "A", "type": 1, "icons": {}},
                    {"id": "b", "name": "B", "type": 1, "icons": {}},
                    {"id": "c", "name": "C", "type": 3, "icons": {}},
                ]
            }
        }
    }
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="templates")
    assert result["free_count"] == 2
    assert result["pro_count"] == 1
    types = [t["type"] for t in result["templates"]]
    assert types == ["free", "free", "pro"]


@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_templates_preview_url_extracted_from_icons_variant1(mock_api_get):
    """preview_url should come from icons.variant1."""
    mock_api_get.return_value = {
        "data": {
            "templateConfiguration": {
                "templateDetails": [
                    {
                        "id": "x",
                        "name": "X",
                        "type": 1,
                        "icons": {"variant1": "https://cdn/preview.png", "variant2": "other"},
                    }
                ]
            }
        }
    }
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="templates")
    assert result["templates"][0]["preview_url"] == "https://cdn/preview.png"


@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_templates_empty_list_returns_zero_counts(mock_api_get):
    """When templateDetails is empty, all counts should be 0."""
    mock_api_get.return_value = {
        "data": {
            "templateConfiguration": {
                "templateDetails": []
            }
        }
    }
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="templates")
    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["free_count"] == 0
    assert result["pro_count"] == 0
    assert result["templates"] == []


# ===========================================================================
# 4. Templates — API error handling
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_templates_naukri_api_error_caught(mock_api_get):
    """NaukriAPIError from api_get must be caught and returned as status=error."""
    mock_api_get.side_effect = _make_api_error(status=401, message="Unauthorized")
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="templates")
    assert result["status"] == "error"
    assert result["error_code"] == "API_ERROR"
    assert result["http_status"] == 401


@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_templates_generic_exception_caught(mock_api_get):
    """Generic exceptions must also be caught and returned as status=error."""
    mock_api_get.side_effect = RuntimeError("network timeout")
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="templates")
    assert result["status"] == "error"
    assert result["error_code"] == "INTERNAL_ERROR"
    assert "RuntimeError" in result["message"]


# ===========================================================================
# 5. Status — data envelope unwrapping & key fields
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_status_unwraps_data_resume_builder_envelope(mock_api_get):
    """Response wrapped as {data: {resumeBuilder: {...}}}."""
    mock_api_get.return_value = {
        "data": {
            "resumeBuilder": {
                "attemptsLeft": 3,
                "isPaid": True,
                "showGenAIFeatures": True,
                "showRewrite": False,
                "attachResumeExperimentVariant": "control",
            }
        }
    }
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="status")
    assert result["status"] == "success"
    assert result["attempts_left"] == 3
    assert result["is_paid"] is True
    assert result["show_genai_features"] is True
    assert result["show_rewrite"] is False
    assert result["experiment_variant"] == "control"


@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_status_defaults_when_fields_missing(mock_api_get):
    """Missing fields must fall back to safe defaults (0, False, '')."""
    mock_api_get.return_value = {"data": {"resumeBuilder": {}}}
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="status")
    assert result["status"] == "success"
    assert result["attempts_left"] == 0
    assert result["is_paid"] is False
    assert result["show_genai_features"] is False
    assert result["show_rewrite"] is False
    assert result["experiment_variant"] == ""


# ===========================================================================
# 6. Status — API error handling
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.resume_builder.api_client.get", new_callable=AsyncMock)
async def test_status_naukri_api_error_caught(mock_api_get):
    """NaukriAPIError from api_get must be caught for the status action."""
    mock_api_get.side_effect = _make_api_error(status=403, message="Forbidden")
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="status")
    assert result["status"] == "error"
    assert result["error_code"] == "API_ERROR"
    assert result["http_status"] == 403


# ===========================================================================
# 7. Tailor — delegates to _tailor_resume and propagates result/errors
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.resume_tailor._tailor_resume", new_callable=AsyncMock)
async def test_tailor_delegates_to_helper(mock_tailor):
    """naukri_resume_builder(tailor) must call _tailor_resume with job_id and timeout."""
    mock_tailor.return_value = {
        "status": "success",
        "job_title": "Python Developer",
        "company": "Acme Corp",
        "suggestions": {},
    }
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="tailor", job_id="JOB123", timeout_seconds=60)
    mock_tailor.assert_awaited_once_with(job_id="JOB123", timeout_seconds=60)
    assert result["status"] == "success"
    assert result["job_title"] == "Python Developer"


@pytest.mark.asyncio
@patch("naukri_server.tools.resume_tailor._tailor_resume", new_callable=AsyncMock)
async def test_tailor_propagates_helper_error_as_internal_error(mock_tailor):
    """Exceptions raised by _tailor_resume must be caught and returned as status=error."""
    mock_tailor.side_effect = RuntimeError("browser crashed")
    from naukri_server.tools.resume_builder import naukri_resume_builder
    result = await naukri_resume_builder(action="tailor", job_id="JOB456")
    assert result["status"] == "error"
    assert result["error_code"] == "INTERNAL_ERROR"
    assert "RuntimeError" in result["message"]
    assert "browser crashed" in result["message"]
