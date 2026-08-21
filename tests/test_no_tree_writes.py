"""Running the suite must not modify the working tree.

CAUGHT THE HARD WAY. `agent.POLICY_STATE_PATH` defaults to
`DATA_DIR/agent_policy_state.json`, and `DATA_DIR` is the repo root. A test that
exercised an agent cycle therefore wrote a real runtime-state file into the
checkout — and a `git add -A` committed it, twice, before anyone noticed.

`conftest.py` already isolated `DB_PATH` for exactly this reason; the state file
needed the same treatment and did not have it. This test is the general form, so
the NEXT state file cannot repeat it: it enumerates every path the package
derives from `DATA_DIR` and asserts each is either isolated during the run or
gitignored.

IT EARNED ITS PLACE ON THE FIRST RUN. Written to cover the one file that had
just gone wrong, it went red on TWO MORE that were already wrong:

    FAILED test_file_is_gitignored[reminders.json]
    FAILED test_file_is_gitignored[interview_rounds.json]

`reminders.json` was **tracked**, 12.8 KB of live follow-up rows, committed long
before this session while every sibling data file (`applications.json`,
`saved_jobs.json`, `questions.json`) was correctly ignored. The repo is private,
so nothing leaked — but it was one visibility flip away from mattering, and no
check existed that could have said so. Both are now ignored; `reminders.json` is
untracked and untouched on disk. It remains in git HISTORY, which is a rewrite
decision for the operator, not for a test.

PURE: filesystem + gitignore inspection, no subprocess, no network.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GITIGNORE = REPO / ".gitignore"


def _ignored_names() -> set[str]:
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


#: Every DATA_DIR-relative artifact the package writes at runtime. Any file put
#: here at import time or during a cycle lands in the checkout unless ignored.
RUNTIME_FILES = [
    "applications.json",
    "saved_jobs.json",
    "reminders.json",
    "questions.json",
    "sync_state.json",
    "early_access_tracking.json",
    "interview_rounds.json",
    "healing_state.json",
    "kill_switch_state.json",
    "agent_config.json",
    "agent_policy_state.json",
]


class TestRuntimeStateIsNeverTracked:
    @pytest.mark.parametrize("name", RUNTIME_FILES)
    def test_file_is_gitignored(self, name):
        assert name in _ignored_names(), (
            f"{name} is written at runtime into DATA_DIR (the repo root) and is "
            f"not in .gitignore — running the suite or the server will dirty "
            f"the working tree, and the next `git add -A` will commit it."
        )

    def test_the_ignore_check_can_fail(self):
        """CONTROL: a name that is genuinely not ignored must not pass."""
        assert "naukri_server" not in _ignored_names()

    def test_the_list_covers_what_config_actually_declares(self):
        """CONTROL in the other direction: if config.py grows a new
        DATA_DIR-relative file, this list must grow with it."""
        src = (REPO / "naukri_server" / "config.py").read_text(encoding="utf-8")
        declared = set(re.findall(r'DATA_DIR / "([^"]+\.json)"', src))
        missing = declared - set(RUNTIME_FILES)
        assert not missing, (
            f"config.py declares DATA_DIR-relative files this list does not "
            f"cover: {sorted(missing)}"
        )
        assert len(declared) >= 6, declared


class TestTheSuiteIsolatesTheStateFile:
    def test_the_agent_policy_state_path_is_redirected_during_tests(self, tmp_path):
        """The autouse fixture must actually have moved it off the repo root."""
        from naukri_server.agent import POLICY_STATE_PATH

        assert REPO not in POLICY_STATE_PATH.parents, (
            f"POLICY_STATE_PATH is inside the checkout during the test run: "
            f"{POLICY_STATE_PATH}"
        )

    def test_the_unredirected_default_really_is_in_the_repo(self):
        """CONTROL. The test above is only meaningful because the SHIPPED
        default lands in the checkout — which is why isolating it matters."""
        from naukri_server.config import DATA_DIR

        assert (DATA_DIR / "agent_policy_state.json").parent == DATA_DIR
        assert DATA_DIR == REPO
