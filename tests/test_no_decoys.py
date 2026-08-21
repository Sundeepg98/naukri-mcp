"""The decoy census — a value that looks configurable must actually be read.

A DECOY is a constant, config key or documented parameter that is defined,
validated, persisted and documented, and that changes nothing when you change
it. Eleven of them were found in this package on 2026-08-21, and they are worse
than a missing feature: a missing feature is visible, whereas a decoy reads as
a working knob and quietly makes the operator's edit a no-op.

These tests are the standing answer. They run over the source, not over a list
someone has to remember to update, so a knob wired to nothing cannot ship.

All PURE: AST + text scans over this repo. No network, no browser, no DB.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "naukri_server"
CONFIG_PY = PKG / "config.py"


def _production_files():
    return [p for p in sorted(PKG.rglob("*.py")) if "__pycache__" not in p.parts]


def _module_constants(path: Path) -> list[str]:
    """Every module-level UPPER_CASE assignment in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for t in targets:
            if t.id.isupper() and not t.id.startswith("_"):
                out.append(t.id)
    return out


def _reader_count(name: str, *, exclude: Path) -> int:
    """How many production lines outside *exclude* mention *name*."""
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    hits = 0
    for p in _production_files():
        if p == exclude:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if pattern.search(line):
                hits += 1
    return hits


# Constants whose only consumer is a name-alias in config.py itself.
_ALIASED = {
    # CACHE_FILE = QUESTIONS_FILE, and the alias is what the package imports.
    "QUESTIONS_FILE",
    # AB_GATEWAY is an f-string fragment of the AB_* endpoints beside it.
    "AB_GATEWAY",
}

CONFIG_CONSTANTS = sorted(set(_module_constants(CONFIG_PY)) - _ALIASED)


def _RETYPED(param: str) -> "re.Pattern":
    """Matches ``x = 14``, ``x: int = 14`` and ``x=14`` — but not ``x = NAME``.

    The annotation is optional and must be allowed for, because every drifted
    copy in this package used ``days_threshold: int = 14``.
    """
    return re.compile(rf"\b{re.escape(param)}\s*(?::\s*[\w\[\]., ]+?\s*)?=\s*\d")


class TestEveryConfigConstantHasAReader:
    """A constant in config.py that nothing imports is a decoy by definition."""

    @pytest.mark.parametrize("name", CONFIG_CONSTANTS)
    def test_constant_is_read_somewhere_in_the_package(self, name):
        assert _reader_count(name, exclude=CONFIG_PY) > 0, (
            f"{name} is defined in naukri_server/config.py and read by NOTHING "
            f"else in the package. Either wire it to the literal that shadows "
            f"it, or delete it — do not ship a knob that changes nothing."
        )

    def test_the_census_can_fail(self):
        """CONTROL. A planted unread constant must be caught.

        Without this the parametrized test above proves only that today's file
        happens to be clean; it does not prove the check works.
        """
        planted = "NAUKRI_A_CONSTANT_NOTHING_READS"
        assert _reader_count(planted, exclude=CONFIG_PY) == 0

    def test_the_census_actually_scanned_the_package(self):
        """And a census over zero files passes vacuously."""
        assert len(CONFIG_CONSTANTS) > 100, len(CONFIG_CONSTANTS)
        assert len(_production_files()) > 50, len(_production_files())


class TestTheFourDeletedDecoysStayDeleted:
    """Named, so a revert reads as a deliberate act rather than an accident."""

    @pytest.mark.parametrize("name", [
        "BATCH_APPLY_TOTAL_TIMEOUT",   # gather timeout for a gather that no longer exists
        "CCS_DASHBOARD_PAGE",          # page name referenced nowhere
    ])
    def test_deleted_constant_is_gone(self, name):
        from naukri_server import config

        assert not hasattr(config, name), (
            f"{name} came back. It had zero readers; if it now has one, give it "
            f"a doc line saying who reads it."
        )

    def test_reminder_days_is_not_in_the_agent_default_config(self):
        """It shipped in DEFAULT_CONFIG and the allowlist and was read by nothing.

        The reminder feature is driven by the caller's `set_reminder_days`
        argument on the apply tools, never by agent config.
        """
        from naukri_server.agent import DEFAULT_CONFIG

        assert "reminder_days" not in DEFAULT_CONFIG

    def test_reminder_days_is_not_in_the_update_allowlist(self):
        src = (PKG / "tools" / "agent_tool.py").read_text(encoding="utf-8")
        allowlist = re.search(r"known_keys = \{(.*?)\}", src, re.S).group(1)
        assert "reminder_days" not in allowlist

    def test_the_agent_config_shadow_file_is_not_inside_the_package(self):
        """`naukri_server/agent_config.json` was never loaded and disagreed with
        the live file about his autonomy mode (`approval` vs `dry_run`).

        The loader reads DATA_DIR/agent_config.json, and DATA_DIR defaults to the
        repo root — so a copy inside the package can only mislead.
        """
        assert not (PKG / "agent_config.json").exists()

    def test_the_agent_config_path_is_data_dir_relative(self):
        """Pins WHERE the live file is, which is what made the shadow possible."""
        from naukri_server.agent import CONFIG_PATH
        from naukri_server.config import DATA_DIR

        assert CONFIG_PATH == DATA_DIR / "agent_config.json"


class TestTheRetypedLiteralsNowPointAtOneHome:
    """Eight retyped `14`s and six retyped fit thresholds, reconciled."""

    def test_staleness_days_has_one_definition(self):
        from naukri_server.config import STALE_THRESHOLD_DAYS

        assert STALE_THRESHOLD_DAYS == 14

    def test_every_staleness_default_reads_the_constant(self):
        import inspect

        from naukri_server.config import STALE_MIN_SCORE, STALE_THRESHOLD_DAYS
        from naukri_server.database import get_stale_applications_raw
        from naukri_server.domain.application import StalenessReport
        from naukri_server.services.application_service import (
            application_follow_up, get_stale_applications,
        )
        from naukri_server.tools.tracking import (
            naukri_follow_up_priority, naukri_stale_applications,
        )

        for fn in (get_stale_applications, application_follow_up,
                   naukri_stale_applications, naukri_follow_up_priority):
            sig = inspect.signature(fn)
            assert sig.parameters["days_threshold"].default == STALE_THRESHOLD_DAYS, fn
            assert sig.parameters["min_stale_score"].default == STALE_MIN_SCORE, fn

        assert inspect.signature(
            get_stale_applications_raw).parameters["days_threshold"].default == STALE_THRESHOLD_DAYS
        assert inspect.signature(
            StalenessReport.compute).parameters["days_threshold"].default == STALE_THRESHOLD_DAYS

    @pytest.mark.parametrize("param", ["days_threshold", "min_stale_score"])
    def test_no_retyped_literal_survives_in_the_package(self, param):
        """The literal form is what drifted. Ban it, not just fix today's copies."""
        offenders = []
        for p in _production_files():
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if _RETYPED(param).search(line):
                    offenders.append(f"{p.relative_to(REPO)}:{i}: {line.strip()}")
        assert not offenders, f"retyped {param} literal:\n" + "\n".join(offenders)

    @pytest.mark.parametrize("sample", [
        "    days_threshold: int = 14,",                 # annotated — the drifted form
        "get_stale_applications(days_threshold=14)",     # keyword argument
        "        min_stale_score: int = 40,",
        "_get_stale_applications(min_stale_score=50)",
        "days_threshold = 14",                           # plain assignment
    ])
    def test_the_literal_scan_can_fail(self, sample):
        """CONTROL for the scan above, on every shape it has to catch.

        This control has already earned its place. The first version of the scan
        used ``[:=]`` and therefore could NOT match ``days_threshold: int = 14``
        — the annotated form every one of the six drifted copies actually used.
        It passed while being incapable of catching the thing it was written for,
        and only this control said so.
        """
        param = "days_threshold" if "days_threshold" in sample else "min_stale_score"
        assert _RETYPED(param).search(sample), sample

    @pytest.mark.parametrize("sample", [
        "    days_threshold: int = STALE_THRESHOLD_DAYS,",
        "min_stale_score=STALE_MIN_SCORE",
        "days_threshold: Consider apps older than N days stale (default 14)",
    ])
    def test_the_literal_scan_does_not_fire_on_the_fixed_form(self, sample):
        """And the other direction: a scan that flags everything is useless."""
        param = "days_threshold" if "days_threshold" in sample else "min_stale_score"
        assert not _RETYPED(param).search(sample), sample


class TestTheTwoFitThresholdsAreTwoDecisions:
    """H7: collapsing them to one value drops the apply threshold 70 -> 60."""

    def test_display_and_apply_defaults_are_todays_literals(self):
        from naukri_server.config import APPLY_MIN_FIT_SCORE, DISPLAY_MIN_FIT_SCORE

        assert DISPLAY_MIN_FIT_SCORE == 60
        assert APPLY_MIN_FIT_SCORE == 70

    def test_they_are_not_the_same_number_by_accident(self):
        from naukri_server.config import APPLY_MIN_FIT_SCORE, DISPLAY_MIN_FIT_SCORE

        assert APPLY_MIN_FIT_SCORE > DISPLAY_MIN_FIT_SCORE, (
            "the agent must never apply at a threshold looser than the one used "
            "merely to decide what to show him"
        )

    def test_the_display_tools_resolve_the_display_threshold_at_call_time(self):
        """They take `None` and resolve it from the config, so an edit to the
        file takes effect with no restart. A literal default would be frozen at
        import — which is the exact thing this whole change exists to escape.

        The resolved value is still today's literal, so behaviour is unchanged
        until he edits the file.
        """
        import inspect

        from naukri_server import policy
        from naukri_server.config import DISPLAY_MIN_FIT_SCORE
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        from naukri_server.tools.smart_apply import naukri_score_saved_jobs

        for fn in (naukri_auto_hunt, naukri_score_saved_jobs):
            assert inspect.signature(
                fn).parameters["min_fit_score"].default is None, fn
        assert policy.display_min_score() == DISPLAY_MIN_FIT_SCORE == 60

    def test_the_resolved_display_threshold_follows_the_file(self, tmp_path, monkeypatch):
        """CONTROL: if it did not move, `None` would just be a slower 60."""
        import json

        from naukri_server import policy

        cfg = tmp_path / "jobhunt.json"
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"display_min_score": 42}},
        }), encoding="utf-8")
        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        policy.invalidate()
        try:
            assert policy.display_min_score() == 42
        finally:
            policy.invalidate()

    def test_a_nonsense_display_threshold_falls_back_rather_than_propagating(
            self, tmp_path, monkeypatch):
        """A malformed value must not reach the comparison. Read is SAFE;
        validation is loud at WRITE."""
        import json

        from naukri_server import policy
        from naukri_server.config import DISPLAY_MIN_FIT_SCORE

        cfg = tmp_path / "jobhunt.json"
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"display_min_score": "sixty"}},
        }), encoding="utf-8")
        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        policy.invalidate()
        try:
            assert policy.display_min_score() == DISPLAY_MIN_FIT_SCORE
        finally:
            policy.invalidate()

    def test_the_apply_tools_default_to_the_apply_threshold(self):
        import inspect

        from naukri_server.config import APPLY_MIN_FIT_SCORE
        from naukri_server.tools.smart_apply import _apply_top_fits, naukri_apply_top_fits

        for fn in (naukri_apply_top_fits, _apply_top_fits):
            assert inspect.signature(
                fn).parameters["min_fit_score"].default == APPLY_MIN_FIT_SCORE, fn

    def test_the_agent_default_config_uses_the_apply_threshold(self):
        from naukri_server.agent import DEFAULT_CONFIG
        from naukri_server.config import APPLY_MIN_FIT_SCORE

        assert DEFAULT_CONFIG["min_fit_score"] == APPLY_MIN_FIT_SCORE == 70
        for search in DEFAULT_CONFIG["searches"]:
            assert search["min_fit_score"] == APPLY_MIN_FIT_SCORE


class TestDocumentedParametersTellTheTruth:
    def test_batch_apply_max_concurrent_is_documented_as_ignored(self):
        """`applies are always serial` in one file, `Max parallel applications
        (default 3)` in another, for the same argument."""
        for rel in ("tools/apply.py", "tools/tracking.py"):
            src = (PKG / rel).read_text(encoding="utf-8")
            if "max_concurrent" not in src:
                continue
            doc = re.findall(r"max_concurrent:.*", src)
            assert doc, rel
            assert any("IGNORED" in d or "DEPRECATED" in d for d in doc), (rel, doc)
