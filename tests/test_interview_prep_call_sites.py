"""naukri_interview_prep must actually reach the tools it imports.

MEASURED 2026-08-24 in the repo venv:

    >>> from naukri_server.tools.mock_interview import naukri_mock_interview
    ImportError: cannot import name 'naukri_mock_interview' from
    'naukri_server.tools.mock_interview'

``naukri_mock_interview`` was one of the consolidated action-parameter
dispatchers. The de-consolidation split it into atomic tools
(``naukri_mock_interview_topics``, ``_history``, ``_prep``, ...) and deleted
the dispatcher, but ``application_service._safe_fetch_mock_topics`` kept
naming the old one. Its ``except Exception`` caught the ImportError and turned
it into an error dict, so every single call to ``naukri_interview_prep``
returned ``partial_success`` with ``mock_topics`` permanently missing --
loudly, at least, but permanently.

The last class here is the rename-proofing: it walks the ``_safe_fetch_*``
helpers with ``ast`` and asserts every tool they import by name really exists.
A future rename that breaks any of the three goes red at import time instead
of shipping as a permanent partial_success.
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.services import application_service

TOPICS_PAYLOAD = {
    "status": "success",
    "total": 2,
    "count": 2,
    "topics": [{"name": "Node.js", "id": "T1"}, {"name": "System Design", "id": "T2"}],
    "roles": [],
}


class TestTheMockTopicsHelperReachesARealTool:

    @pytest.mark.asyncio
    async def test_helper_returns_the_atomic_tools_payload(self):
        with patch("naukri_server.tools.mock_interview.naukri_mock_interview_topics",
                   new=AsyncMock(return_value=TOPICS_PAYLOAD)) as tool:
            result = await application_service._safe_fetch_mock_topics()
        tool.assert_awaited_once()
        assert result == TOPICS_PAYLOAD

    @pytest.mark.asyncio
    async def test_helper_does_not_report_an_import_error(self):
        """The pre-fix signature, stated as the thing that must not happen."""
        with patch("naukri_server.tools.mock_interview.naukri_mock_interview_topics",
                   new=AsyncMock(return_value=TOPICS_PAYLOAD)):
            result = await application_service._safe_fetch_mock_topics()
        assert "ImportError" not in str(result)

    @pytest.mark.asyncio
    async def test_CONTROL_a_genuinely_broken_tool_still_surfaces(self):
        """The helper must keep REPORTING a failure, not start swallowing one --
        a check that cannot fail certifies nothing."""
        with patch("naukri_server.tools.mock_interview.naukri_mock_interview_topics",
                   new=AsyncMock(side_effect=ImportError("boom"))):
            result = await application_service._safe_fetch_mock_topics()
        assert result["status"] == "error"
        assert "ImportError" in result["message"]


class TestInterviewPrepEndToEnd:

    async def _run_prep(self):
        app_row = {"company": "ExampleCo", "title": "Backend Engineer",
                   "applied_at": "2026-08-01T00:00:00", "ars_score": 70}
        intel = {"status": "success", "overall_rating": 3.5,
                 "difficulty_breakdown": {}, "interview_experiences": []}
        fit = {"status": "success",
               "fit_assessment": {"skill_match": {"matched": ["Node.js"], "missing": []}}}
        with patch("naukri_server.database.get_application",
                   new=AsyncMock(return_value=app_row)), \
             patch("naukri_server.tools.ambitionbox.naukri_company_intel",
                   new=AsyncMock(return_value=intel)), \
             patch("naukri_server.tools.smart_apply.naukri_assess_fit",
                   new=AsyncMock(return_value=fit)), \
             patch("naukri_server.tools.mock_interview.naukri_mock_interview_topics",
                   new=AsyncMock(return_value=TOPICS_PAYLOAD)):
            return await application_service.interview_prep("J1")

    @pytest.mark.asyncio
    async def test_mock_topics_section_is_populated(self):
        prep = await self._run_prep()
        assert prep["mock_topics"] == TOPICS_PAYLOAD["topics"]

    @pytest.mark.asyncio
    async def test_no_permanent_partial_success(self):
        prep = await self._run_prep()
        assert prep["status"] == "success"
        assert "errors" not in prep
        assert "missing_sections" not in prep


class TestEveryImportedToolNameExists:
    """Rename-proofing. The three ``_safe_fetch_*`` helpers import their tools
    lazily inside a ``try/except Exception``, which means a wrong name is
    invisible until runtime and then arrives dressed as an API error."""

    HELPERS = ("_safe_fetch_company_intel", "_safe_fetch_mock_topics",
               "_safe_fetch_fit_score")

    def _lazy_imports(self):
        """Yield (helper, module, name) for every ``from X import Y`` inside
        the helpers."""
        source = Path(inspect.getfile(application_service)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name not in self.HELPERS:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    for alias in sub.names:
                        found.append((node.name, sub.module, alias.name))
        return found

    def test_CONTROL_the_scanner_finds_all_three_helpers(self):
        helpers = {h for h, _, _ in self._lazy_imports()}
        assert helpers == set(self.HELPERS), (
            "the scanner must actually see every helper, or it certifies "
            "nothing; saw %s" % sorted(helpers))

    def test_every_lazily_imported_tool_resolves(self):
        broken = []
        for helper, module_name, attr in self._lazy_imports():
            module = importlib.import_module(module_name)
            if not hasattr(module, attr):
                broken.append("%s imports %s from %s, which does not exist"
                              % (helper, attr, module_name))
        assert not broken, "\n".join(broken)
