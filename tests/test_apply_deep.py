"""Deep tests for naukri_server.tools.apply — screening question retry logic.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier25.py.
"""

import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Screening Question Retry Bug Fix
# ---------------------------------------------------------------------------

class TestScreeningQuestionRetry:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_retry_needs_input_with_answers(self, mock_load, mock_apply):
        """When status=needs_input and answers provided, allow retry."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "needs_input", "pending_questions": 2}
        ]
        mock_apply.return_value = {"status": "applied", "job_id": "110326018660"}
        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660", answers={"q1": "yes"})
        assert result["status"] == "applied"
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_block_needs_input_without_answers(self, mock_load, mock_apply):
        """When status=needs_input but NO answers, still block."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "needs_input"}
        ]
        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660")
        assert result["status"] == "already_applied"
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_block_already_applied_with_answers(self, mock_load, mock_apply):
        """When status=applied, block even with answers."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "applied"}
        ]
        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660", answers={"q1": "yes"})
        assert result["status"] == "already_applied"
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_new_job_no_existing_record(self, mock_load, mock_apply):
        """When no existing record, proceed to apply."""
        mock_load.return_value = []
        mock_apply.return_value = {"status": "applied", "job_id": "990326000001"}
        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="990326000001")
        assert result["status"] == "applied"
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_block_error_status_without_answers(self, mock_load, mock_apply):
        """When status=error but no answers, block."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "error"}
        ]
        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660")
        assert result["status"] == "already_applied"
        mock_apply.assert_not_called()
