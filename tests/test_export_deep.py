"""Deep unit tests for naukri_server/tools/export.py.

Tests cover format validation, data_type validation, file-not-found paths,
read errors, JSON/CSV write paths, CSV flattening logic, default/custom output
paths, search_results routing, and empty-records guard.

Every test is PURE: no network, no browser, no file I/O.
"""

import io
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open


# ===========================================================================
# 1. _flatten_for_csv — synchronous unit tests (no mocking needed)
# ===========================================================================


class TestFlattenForCsv:
    """Tests for the _flatten_for_csv helper."""

    def test_plain_values_pass_through(self):
        from naukri_server.tools.export import _flatten_for_csv

        records = [{"title": "SDE", "company": "Google", "salary": 1500000}]
        result = _flatten_for_csv(records)
        assert result == [{"title": "SDE", "company": "Google", "salary": 1500000}]

    def test_list_values_joined_with_comma(self):
        from naukri_server.tools.export import _flatten_for_csv

        records = [{"skills": ["Python", "Django", "REST"]}]
        result = _flatten_for_csv(records)
        assert result[0]["skills"] == "Python, Django, REST"

    def test_nested_dict_uses_dot_notation(self):
        from naukri_server.tools.export import _flatten_for_csv

        records = [{"location": {"city": "Bangalore", "state": "Karnataka"}}]
        result = _flatten_for_csv(records)
        assert "location" not in result[0]
        assert result[0]["location.city"] == "Bangalore"
        assert result[0]["location.state"] == "Karnataka"

    def test_nested_dict_sub_list_stringified(self):
        """Sub-values that are lists inside a nested dict are converted to str."""
        from naukri_server.tools.export import _flatten_for_csv

        records = [{"meta": {"tags": ["a", "b"]}}]
        result = _flatten_for_csv(records)
        assert result[0]["meta.tags"] == "['a', 'b']"

    def test_nested_dict_sub_dict_stringified(self):
        """Sub-values that are dicts inside a nested dict are converted to str."""
        from naukri_server.tools.export import _flatten_for_csv

        records = [{"meta": {"inner": {"x": 1}}}]
        result = _flatten_for_csv(records)
        assert result[0]["meta.inner"] == "{'x': 1}"

    def test_multiple_records_all_flattened(self):
        from naukri_server.tools.export import _flatten_for_csv

        records = [
            {"title": "SDE", "skills": ["Python"]},
            {"title": "QA", "skills": ["Selenium", "pytest"]},
        ]
        result = _flatten_for_csv(records)
        assert result[0]["skills"] == "Python"
        assert result[1]["skills"] == "Selenium, pytest"

    def test_empty_list_value(self):
        from naukri_server.tools.export import _flatten_for_csv

        records = [{"tags": []}]
        result = _flatten_for_csv(records)
        assert result[0]["tags"] == ""

    def test_empty_records_returns_empty_list(self):
        from naukri_server.tools.export import _flatten_for_csv

        assert _flatten_for_csv([]) == []


# ===========================================================================
# 2. Format validation
# ===========================================================================


class TestFormatValidation:
    """_export_data rejects formats other than json/csv."""

    @pytest.mark.asyncio
    async def test_invalid_format_xml(self):
        from naukri_server.tools.export import _export_data

        result = await _export_data(data_type="applications", format="xml")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "xml" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_invalid_format_pdf(self):
        from naukri_server.tools.export import _export_data

        result = await _export_data(data_type="applications", format="pdf")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_format_case_insensitive_csv(self):
        """'CSV' (uppercase) should be accepted after lower()."""
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        fake_root = _make_fake_root_with_apps([{"title": "SDE"}])
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = fake_root
        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                # Provide a fake file path that accepts write_text
                fake_file = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                result = await _export_data(data_type="applications", format="CSV")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        # Should NOT be a VALIDATION_ERROR — format accepted
        assert result.get("error_code") != "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_format_case_insensitive_json(self):
        """'JSON' (uppercase) should be accepted after lower()."""
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        fake_root = _make_fake_root_with_apps([{"title": "SDE"}])
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = fake_root
        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                fake_file = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                result = await _export_data(data_type="applications", format="JSON")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result.get("error_code") != "VALIDATION_ERROR"


# ===========================================================================
# 3. Data-type validation
# ===========================================================================


class TestDataTypeValidation:
    """_export_data rejects unknown data_type values."""

    @pytest.mark.asyncio
    async def test_invalid_data_type_resumes(self):
        from naukri_server.tools.export import _export_data

        result = await _export_data(data_type="resumes")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "resumes" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_data_type_empty_string(self):
        from naukri_server.tools.export import _export_data

        result = await _export_data(data_type="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_data_type_case_insensitive_applications(self):
        """'Applications' (mixed case) should be accepted after lower()."""
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        fake_root = _make_fake_root_with_apps([{"title": "SDE"}])
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = fake_root
        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                fake_file = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                result = await _export_data(data_type="Applications", format="json")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result.get("error_code") != "VALIDATION_ERROR"


# ===========================================================================
# 4. Keywords guard for search_results
# ===========================================================================


class TestSearchResultsKeywordsGuard:
    """keywords is required when data_type == search_results."""

    @pytest.mark.asyncio
    async def test_search_results_no_keywords(self):
        from naukri_server.tools.export import _export_data

        result = await _export_data(data_type="search_results")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keywords" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_search_results_empty_string_keywords(self):
        """Empty string is falsy — should still return VALIDATION_ERROR."""
        from naukri_server.tools.export import _export_data

        result = await _export_data(data_type="search_results", keywords="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"


# ===========================================================================
# 5. File-not-found paths
# ===========================================================================


class TestFileNotFound:
    """_export_data returns NOT_FOUND when data files are missing."""

    @pytest.mark.asyncio
    async def test_applications_file_not_found(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_file_missing("applications.json")
        try:
            result = await _export_data(data_type="applications")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        assert "applications" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_saved_jobs_file_not_found(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_file_missing("saved_jobs.json")
        try:
            result = await _export_data(data_type="saved_jobs")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        assert "saved jobs" in result["message"].lower()


# ===========================================================================
# 6. File read errors
# ===========================================================================


class TestFileReadError:
    """_export_data returns API_ERROR when reading a file raises an exception."""

    @pytest.mark.asyncio
    async def test_applications_read_error(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_read_error("applications.json")
        try:
            result = await _export_data(data_type="applications")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "applications" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_saved_jobs_read_error(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_read_error("saved_jobs.json")
        try:
            result = await _export_data(data_type="saved_jobs")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"


# ===========================================================================
# 7. No records to export
# ===========================================================================


class TestNoRecords:
    """_export_data returns NOT_FOUND when the file exists but holds [] ."""

    @pytest.mark.asyncio
    async def test_applications_empty_list(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps([])
        try:
            result = await _export_data(data_type="applications")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        assert "no" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_search_results_returns_empty_jobs(self):
        """When naukri_search_jobs returns jobs=[] the export returns NOT_FOUND."""
        from naukri_server.tools.export import _export_data

        with patch(
            "naukri_server.tools.search.naukri_search_jobs",
            new_callable=AsyncMock,
        ) as mock_search:
            mock_search.return_value = {"status": "success", "jobs": []}
            result = await _export_data(data_type="search_results", keywords="python")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"


# ===========================================================================
# 8. JSON export
# ===========================================================================


class TestJsonExport:
    """_export_data writes JSON with indent=2 and returns success metadata."""

    @pytest.mark.asyncio
    async def test_json_export_success_returns_metadata(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        records = [{"title": "SDE", "company": "Google"}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps(records)
        fake_file = MagicMock()
        fake_file.__str__ = lambda self: "/fake/exports/applications_2026-01-01.json"

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                result = await _export_data(data_type="applications", format="json")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "success"
        assert result["record_count"] == 1
        assert result["data_type"] == "applications"
        assert result["format"] == "json"

    @pytest.mark.asyncio
    async def test_json_export_writes_with_indent(self):
        """file_path.write_text must be called once with indent=2 JSON."""
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        records = [{"title": "SDE"}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps(records)
        fake_file = MagicMock()

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                await _export_data(data_type="applications", format="json")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        fake_file.write_text.assert_called_once()
        written_text = fake_file.write_text.call_args[0][0]
        # Verify the output is valid JSON and has indentation
        parsed = json.loads(written_text)
        assert parsed == records
        assert "\n  " in written_text  # indent=2 produces two-space indentation


# ===========================================================================
# 9. CSV export
# ===========================================================================


class TestCsvExport:
    """_export_data writes CSV with header row and correct rows."""

    @pytest.mark.asyncio
    async def test_csv_export_success_returns_metadata(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        records = [{"title": "SDE", "company": "Google"}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps(records)
        fake_file = MagicMock()

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                result = await _export_data(data_type="applications", format="csv")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "success"
        assert result["format"] == "csv"
        assert result["record_count"] == 1

    @pytest.mark.asyncio
    async def test_csv_export_writes_header_and_rows(self):
        """The written CSV text must contain the header and data row."""
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        records = [{"title": "SDE", "company": "Google"}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps(records)
        fake_file = MagicMock()

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                await _export_data(data_type="applications", format="csv")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        fake_file.write_text.assert_called_once()
        csv_text = fake_file.write_text.call_args[0][0]
        lines = csv_text.strip().splitlines()
        assert len(lines) >= 2  # header + at least 1 data row
        assert "title" in lines[0]
        assert "company" in lines[0]
        assert "SDE" in csv_text
        assert "Google" in csv_text

    @pytest.mark.asyncio
    async def test_csv_flattening_applied_to_nested_record(self):
        """Nested dicts in records result in dot-notation columns in CSV."""
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        records = [{"title": "SDE", "loc": {"city": "Bangalore"}}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps(records)
        fake_file = MagicMock()

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                await _export_data(data_type="applications", format="csv")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        csv_text = fake_file.write_text.call_args[0][0]
        assert "loc.city" in csv_text
        assert "Bangalore" in csv_text


# ===========================================================================
# 10. Output path logic
# ===========================================================================


class TestOutputPath:
    """_export_data uses exports/<type>_<date>.<ext> by default, or custom path."""

    @pytest.mark.asyncio
    async def test_default_output_path_contains_data_type_and_date(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        records = [{"title": "SDE"}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps(records)

        captured_name = []

        def fake_truediv(self, name):
            captured_name.append(name)
            return MagicMock()

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                mock_dir.__truediv__ = fake_truediv
                await _export_data(data_type="applications", format="json")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert len(captured_name) == 1
        generated_name = captured_name[0]
        assert "applications" in generated_name
        assert ".json" in generated_name
        # Date component should look like YYYY-MM-DD
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}", generated_name), (
            f"Expected date in filename, got: {generated_name}"
        )

    @pytest.mark.asyncio
    async def test_custom_output_path_used_verbatim(self):
        """When output_path is given, _export_data uses Path(output_path) directly."""
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod
        from pathlib import Path

        records = [{"title": "SDE"}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_apps(records)

        custom_path = "/tmp/my_custom_export.json"

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir, \
                 patch("naukri_server.tools.export.Path") as mock_path_cls:
                mock_dir.mkdir = MagicMock()
                fake_file = MagicMock()
                fake_file.__str__ = lambda self: custom_path
                mock_path_cls.return_value = fake_file
                result = await _export_data(
                    data_type="applications",
                    format="json",
                    output_path=custom_path,
                )
        finally:
            export_mod._PACKAGE_ROOT = original_root

        # Path() should have been called with the custom path string
        mock_path_cls.assert_called_with(custom_path)
        assert result["status"] == "success"
        assert result["file_path"] == custom_path


# ===========================================================================
# 11. Search results data source
# ===========================================================================


class TestSearchResultsDataSource:
    """_export_data calls naukri_search_jobs with limit=50 for search_results."""

    @pytest.mark.asyncio
    async def test_search_results_calls_search_with_limit_50(self):
        from naukri_server.tools.export import _export_data

        with patch(
            "naukri_server.tools.search.naukri_search_jobs",
            new_callable=AsyncMock,
        ) as mock_search:
            mock_search.return_value = {
                "status": "success",
                "jobs": [{"title": "Python Dev", "company": "Acme"}],
            }
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                fake_file = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                result = await _export_data(
                    data_type="search_results",
                    keywords="python developer",
                    format="json",
                )

        mock_search.assert_awaited_once_with(keywords="python developer", limit=50)
        assert result["status"] == "success"
        assert result["record_count"] == 1

    @pytest.mark.asyncio
    async def test_search_results_propagates_search_error(self):
        """When naukri_search_jobs returns an error dict, it is returned as-is."""
        from naukri_server.tools.export import _export_data

        with patch(
            "naukri_server.tools.search.naukri_search_jobs",
            new_callable=AsyncMock,
        ) as mock_search:
            mock_search.return_value = {
                "status": "error",
                "message": "Auth failed",
                "error_code": "AUTH_ERROR",
            }
            result = await _export_data(
                data_type="search_results",
                keywords="python",
            )

        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR"


# ===========================================================================
# 12. Saved-jobs happy path (JSON)
# ===========================================================================


class TestSavedJobsExport:
    """_export_data reads saved_jobs.json and exports it successfully."""

    @pytest.mark.asyncio
    async def test_saved_jobs_json_export_success(self):
        from naukri_server.tools.export import _export_data
        from naukri_server.tools import export as export_mod

        records = [{"title": "Data Scientist", "company": "Meta"}]
        original_root = export_mod._PACKAGE_ROOT
        export_mod._PACKAGE_ROOT = _make_fake_root_with_saved_jobs(records)
        fake_file = MagicMock()

        try:
            with patch("naukri_server.tools.export._EXPORTS_DIR") as mock_dir:
                mock_dir.mkdir = MagicMock()
                mock_dir.__truediv__ = MagicMock(return_value=fake_file)
                result = await _export_data(data_type="saved_jobs", format="json")
        finally:
            export_mod._PACKAGE_ROOT = original_root

        assert result["status"] == "success"
        assert result["data_type"] == "saved_jobs"
        assert result["record_count"] == 1
        fake_file.write_text.assert_called_once()


# ===========================================================================
# Helper factories
# ===========================================================================


def _make_fake_file(records: list, exists: bool = True, read_error: bool = False):
    """Return a MagicMock that mimics a pathlib.Path pointing at a JSON file."""
    fake = MagicMock()
    fake.exists.return_value = exists
    if read_error:
        fake.read_text.side_effect = OSError("Permission denied")
    else:
        fake.read_text.return_value = json.dumps(records)
    return fake


def _make_fake_root_with_apps(records: list):
    """Fake _PACKAGE_ROOT whose applications.json holds *records*."""
    fake_root = MagicMock()
    fake_apps = _make_fake_file(records, exists=True)
    fake_exports_dir = MagicMock()
    fake_exports_dir.mkdir = MagicMock()

    def _div(self, key):
        if key == "applications.json":
            return fake_apps
        if key == "exports":
            return fake_exports_dir
        return MagicMock()

    fake_root.__truediv__ = _div
    return fake_root


def _make_fake_root_with_saved_jobs(records: list):
    """Fake _PACKAGE_ROOT whose saved_jobs.json holds *records*."""
    fake_root = MagicMock()
    fake_saved = _make_fake_file(records, exists=True)
    fake_exports_dir = MagicMock()
    fake_exports_dir.mkdir = MagicMock()

    def _div(self, key):
        if key == "saved_jobs.json":
            return fake_saved
        if key == "exports":
            return fake_exports_dir
        return MagicMock()

    fake_root.__truediv__ = _div
    return fake_root


def _make_fake_root_file_missing(filename: str):
    """Fake _PACKAGE_ROOT where *filename* does not exist."""
    fake_root = MagicMock()
    fake_missing = _make_fake_file([], exists=False)

    def _div(self, key):
        if key == filename:
            return fake_missing
        return MagicMock()

    fake_root.__truediv__ = _div
    return fake_root


def _make_fake_root_read_error(filename: str):
    """Fake _PACKAGE_ROOT where *filename* exists but read_text raises OSError."""
    fake_root = MagicMock()
    fake_erroring = _make_fake_file([], exists=True, read_error=True)

    def _div(self, key):
        if key == filename:
            return fake_erroring
        return MagicMock()

    fake_root.__truediv__ = _div
    return fake_root
