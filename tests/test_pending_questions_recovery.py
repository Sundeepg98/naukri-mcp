"""Pending screening questions: persistence, backward compatibility, recovery.

Covers the defect where a needs_input row persisted `len(pending)` instead of
`pending` -- the questions were handed to the caller and only their COUNT
reached the database, so an unanswered application forgot what it was asked.

Every test is PURE: no network, no browser, no file I/O. The answer cache and
the apply endpoint are mocked at every site that would touch a real one.
"""

import inspect
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

#: The distribution measured on the live naukri.db 2026-08-25: 17 stalled
#: applications blocked on 61 questions, questions -> number of applications.
REAL_DISTRIBUTION = {1: 4, 2: 3, 3: 1, 4: 4, 5: 3, 7: 1, 10: 1}


def legacy_row(job_id, count, company="Some Co", title="Some Role",
               applied_at="2026-03-03T05:28:01+00:00"):
    """A row as it exists TODAY: extra.pending_questions is a bare int."""
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "status": "needs_input",
        "applied_at": applied_at,
        "extra": json.dumps({"pending_questions": count}),
    }


def modern_row(job_id, questions, company="Some Co", title="Some Role",
               applied_at="2026-03-03T05:28:01+00:00"):
    """A row written AFTER the fix: extra.pending_questions is the question list."""
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "status": "needs_input",
        "applied_at": applied_at,
        "extra": json.dumps({"pending_questions": questions}),
    }


def q(text, qtype="text", options=None):
    return {"question": text, "type": qtype, "options": options or {}}


def real_distribution_rows():
    """17 legacy rows matching the measured live distribution."""
    rows, n = [], 0
    for count, apps in sorted(REAL_DISTRIBUTION.items()):
        for _ in range(apps):
            n += 1
            rows.append(legacy_row("job%03d" % n, count))
    return rows


def patch_report_db(rows, cache=None):
    """Patch the two things _pending_questions_report reads. Nothing else."""
    return (
        patch("naukri_server.database.list_applications",
              new=AsyncMock(return_value=(rows, len(rows)))),
        patch("naukri_server.cache._load_cache", return_value=cache or {}),
    )


# ---------------------------------------------------------------------------
# 1. The write side: persist the questions, not their length
# ---------------------------------------------------------------------------

QUESTIONNAIRE = [
    {"questionId": "1", "questionName": "What is your expected CTC in Lacs per annum?",
     "questionType": "textbox", "answerOption": {}},
    {"questionId": "2", "questionName": "What is your notice period?",
     "questionType": "radio", "answerOption": {"0": "15 Days or less", "1": "1 Month"}},
    {"questionId": "3", "questionName": "How many years of experience do you have in Node.Js?",
     "questionType": "textbox", "answerOption": {}},
]


def apply_single_env(post_response, cache=None):
    """Every seam _apply_single would use to reach the network, disk or clock."""
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    return [
        patch("naukri_server.tools.apply.api_client.post",
              new=AsyncMock(return_value=post_response)),
        patch("naukri_server.tools.apply._load_cache", return_value=cache or {}),
        patch("naukri_server.tools.apply._save_cache"),
        patch("naukri_server.tools.apply.get_apply_rate_limiter", return_value=limiter),
        patch("naukri_server.tools.apply._daily_quota_exceeded",
              new=AsyncMock(return_value=(False, 0))),
        patch("naukri_server.tools.apply.kill_switch.guard"),
    ]


class TestQuestionsArePersisted:
    @pytest.mark.asyncio
    async def test_pending_questions_persists_the_text_not_the_count(self):
        """THE FIX. The row must store the questions, not len(pending)."""
        from naukri_server.tools.apply import _apply_single

        response = {"jobs": [{"status": 400, "questionnaire": QUESTIONNAIRE}]}
        recorded = AsyncMock()
        patches = apply_single_env(response) + [
            patch("naukri_server.tools.apply.record_application", new=recorded)]
        for p in patches:
            p.start()
        try:
            result = await _apply_single("230226006625")
        finally:
            for p in patches:
                p.stop()

        assert result["status"] == "needs_input"
        stored = recorded.await_args.kwargs["extra"]["pending_questions"]

        # The defect, stated as a test: this used to be the integer 3.
        assert not isinstance(stored, int), "regressed to persisting a count"
        assert isinstance(stored, list)
        assert len(stored) == 3
        assert [s["question"] for s in stored] == [x["questionName"] for x in QUESTIONNAIRE]
        assert stored[1]["type"] == "radio"
        assert stored[1]["options"] == {"0": "15 Days or less", "1": "1 Month"}

    @pytest.mark.asyncio
    async def test_the_caller_still_receives_the_full_question_list(self):
        """Persisting must not change what the caller gets back."""
        from naukri_server.tools.apply import _apply_single

        response = {"jobs": [{"status": 400, "questionnaire": QUESTIONNAIRE}]}
        patches = apply_single_env(response) + [
            patch("naukri_server.tools.apply.record_application", new=AsyncMock())]
        for p in patches:
            p.start()
        try:
            result = await _apply_single("230226006625")
        finally:
            for p in patches:
                p.stop()

        assert len(result["questions"]) == 3
        assert result["questions"][0]["question_id"] == "1"

    def test_a_persisted_question_never_carries_an_answer(self):
        """Answers are personal data; they belong only in the answer cache."""
        from naukri_server.tools.tracking import project_pending_questions

        projected = project_pending_questions([{
            "question_id": "1",
            "question": "What is your expected CTC in Lacs per annum?",
            "type": "textbox",
            "options": {},
            "answer": "16",             # must not survive
            "cached_answer": "16",      # nor this
        }])

        assert projected == [{
            "question": "What is your expected CTC in Lacs per annum?",
            "type": "textbox",
            "options": {},
        }]
        assert "answer" not in projected[0]
        assert "16" not in json.dumps(projected)

    def test_projection_survives_the_extra_column_round_trip(self):
        from naukri_server.tools.tracking import project_pending_questions, read_pending_questions

        projected = project_pending_questions([q("CTC?"), q("Notice?", "radio", {"0": "1 Month"})])
        # This is exactly what database._application_extra does to it.
        revived = json.loads(json.dumps({"pending_questions": projected}))["pending_questions"]
        assert read_pending_questions(revived)["questions"] == projected


# ---------------------------------------------------------------------------
# 2. Backward compatibility: a legacy int row must say what it cannot show
# ---------------------------------------------------------------------------

class TestLegacyRowsAreReadHonestly:
    def test_legacy_int_reports_text_not_recoverable(self):
        from naukri_server.tools.tracking import read_pending_questions

        read = read_pending_questions(3)
        assert read["count"] == 3
        assert read["questions"] == []
        assert read["text_recoverable"] is False
        assert read["note"] == "3 questions, text not recoverable, re-fetch to see them"

    def test_legacy_singular_reads_as_one_question(self):
        from naukri_server.tools.tracking import read_pending_questions

        assert read_pending_questions(1)["note"] == (
            "1 question, text not recoverable, re-fetch to see it")

    def test_modern_list_reports_text_recoverable_with_no_note(self):
        from naukri_server.tools.tracking import read_pending_questions

        read = read_pending_questions([q("CTC?"), q("Notice?")])
        assert read["count"] == 2
        assert read["text_recoverable"] is True
        assert read["note"] is None

    def test_true_is_not_read_as_one_question(self):
        """bool subclasses int -- it must not slip through as a count of 1."""
        from naukri_server.tools.tracking import read_pending_questions

        assert read_pending_questions(True)["count"] == 0

    def test_garbage_never_raises(self):
        from naukri_server.tools.tracking import read_pending_questions

        for raw in (None, "3", {"a": 1}, -5, 3.5):
            read = read_pending_questions(raw)
            assert read["text_recoverable"] is False
            assert read["questions"] == []
            assert read["count"] >= 0

    @pytest.mark.asyncio
    async def test_legacy_row_is_not_rendered_as_zero_questions(self):
        """THE REQUIRED TEST. An empty list would read as "no questions"."""
        from naukri_server.tools.tracking import _pending_questions_report

        p1, p2 = patch_report_db([legacy_row("240226046865", 3)])
        with p1, p2:
            report = await _pending_questions_report()

        app = report["applications"][0]
        assert app["question_count"] == 3, "the count must survive"
        assert app["text_recoverable"] is False
        assert "text not recoverable" in app["note"]
        assert app["questions"] == []
        # and the report must not describe it as having question text
        assert report["with_question_text"] == 0
        assert report["text_not_recoverable"] == 1
        assert report["total_questions"] == 3


# ---------------------------------------------------------------------------
# 3. The read side: cheapest wins first
# ---------------------------------------------------------------------------

class TestPendingQuestionsReport:
    @pytest.mark.asyncio
    async def test_real_distribution_sorts_one_question_above_ten(self):
        """17 rows / 61 questions, as measured. The 1 must precede the 10."""
        from naukri_server.tools.tracking import _pending_questions_report

        p1, p2 = patch_report_db(real_distribution_rows())
        with p1, p2:
            report = await _pending_questions_report()

        apps = report["applications"]
        assert len(apps) == 17
        assert report["total_questions"] == 61
        assert report["total_needs_input"] == 17

        counts = [a["question_count"] for a in apps]
        assert counts[0] == 1, "cheapest win must be first"
        assert counts[-1] == 10, "most expensive must be last"
        assert counts == sorted(counts), "must be ordered by cost"
        assert counts.count(1) == 4

        # Every one of them is legacy today, and every one says so.
        assert report["text_not_recoverable"] == 17
        assert all("text not recoverable" in a["note"] for a in apps)
        # A legacy row's outstanding count is an upper bound, and is flagged.
        assert all(a["unanswered_is_estimate"] for a in apps)

    @pytest.mark.asyncio
    async def test_an_already_answered_question_sorts_above_an_unanswered_one(self):
        """One action from done must not sit below one that needs new input."""
        from naukri_server.tools.tracking import _pending_questions_report
        from naukri_server.cache import _cache_key

        answered_q = q("What is your expected CTC in Lacs per annum?")
        unanswered_q = q("How many years of Rust do you have?")
        cache = {_cache_key(answered_q["question"], {}): {"answer": "16"}}

        rows = [
            modern_row("unanswered-1", [unanswered_q]),
            modern_row("answered-1", [answered_q]),
            legacy_row("legacy-10", 10),
        ]
        p1, p2 = patch_report_db(rows, cache=cache)
        with p1, p2:
            report = await _pending_questions_report()

        apps = report["applications"]
        assert apps[0]["job_id"] == "answered-1"
        assert apps[0]["unanswered_count"] == 0
        assert apps[0]["questions"][0]["answered"] is True
        assert apps[0]["unanswered_is_estimate"] is False

        assert apps[1]["job_id"] == "unanswered-1"
        assert apps[1]["unanswered_count"] == 1
        assert apps[1]["questions"][0]["answered"] is False

        assert apps[2]["job_id"] == "legacy-10"
        assert report["total_unanswered"] == 11

    @pytest.mark.asyncio
    async def test_null_company_and_title_do_not_crash(self):
        """9 of the 17 live rows carry NULL company AND NULL title."""
        from naukri_server.tools.tracking import _pending_questions_report

        p1, p2 = patch_report_db([legacy_row("150126032994", 3, company=None, title=None)])
        with p1, p2:
            report = await _pending_questions_report()

        app = report["applications"][0]
        assert app["company"] is None
        assert app["title"] is None
        assert app["question_count"] == 3

    @pytest.mark.asyncio
    async def test_report_makes_no_writes_and_no_network(self):
        """Read-only. It must not call the apply endpoint or record anything."""
        from naukri_server.tools.tracking import _pending_questions_report

        p1, p2 = patch_report_db(real_distribution_rows())
        post = AsyncMock()
        record = AsyncMock()
        with p1, p2, \
                patch("naukri_server.tools.apply.api_client.post", new=post), \
                patch("naukri_server.tools.apply.record_application", new=record), \
                patch("naukri_server.cache._save_cache") as save:
            await _pending_questions_report()

        post.assert_not_called()
        record.assert_not_called()
        save.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_cache_degrades_instead_of_failing(self):
        from naukri_server.tools.tracking import _pending_questions_report

        rows = [modern_row("j1", [q("CTC?")])]
        with patch("naukri_server.database.list_applications",
                   new=AsyncMock(return_value=(rows, 1))), \
                patch("naukri_server.cache._load_cache", side_effect=OSError("boom")):
            report = await _pending_questions_report()

        assert report["applications"][0]["questions"][0]["answered"] is False

    @pytest.mark.asyncio
    async def test_mixed_shapes_coexist_in_one_report(self):
        from naukri_server.tools.tracking import _pending_questions_report

        rows = [legacy_row("legacy", 4), modern_row("modern", [q("CTC?"), q("Notice?")])]
        p1, p2 = patch_report_db(rows)
        with p1, p2:
            report = await _pending_questions_report()

        assert report["with_question_text"] == 1
        assert report["text_not_recoverable"] == 1
        assert report["total_questions"] == 6
        by_id = {a["job_id"]: a for a in report["applications"]}
        assert by_id["modern"]["questions"][0]["question"] == "CTC?"
        assert "note" not in by_id["modern"]
        assert by_id["legacy"]["questions"] == []


# ---------------------------------------------------------------------------
# 4. Recovery: gated, and structurally unable to answer anything
# ---------------------------------------------------------------------------

def recover_env(post_response=None, post=None):
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    return [
        patch("naukri_server.tools.apply.api_client.post",
              new=post or AsyncMock(return_value=post_response)),
        patch("naukri_server.tools.apply.get_apply_rate_limiter", return_value=limiter),
        patch("naukri_server.tools.apply.kill_switch.guard"),
    ]


class TestRecoveryIsGated:
    @pytest.mark.asyncio
    async def test_confirm_false_performs_nothing(self):
        """THE REQUIRED TEST. Default must preview and touch nothing."""
        from naukri_server.tools.apply import _recover_pending_questions

        rows = [legacy_row("240226046865", 1), legacy_row("171024011665", 10)]
        post = AsyncMock()
        record = AsyncMock()
        with patch("naukri_server.database.list_applications",
                   new=AsyncMock(return_value=(rows, 2))), \
                patch("naukri_server.tools.apply.api_client.post", new=post), \
                patch("naukri_server.tools.apply.record_application", new=record):
            result = await _recover_pending_questions()

        post.assert_not_called()
        record.assert_not_called()
        assert result["status"] == "preview"
        assert result["confirm_required"] is True
        assert result["performed"] == "nothing"
        # It must NAME exactly which job ids would be re-fetched.
        assert result["would_refetch_count"] == 2
        assert {t["job_id"] for t in result["would_refetch"]} == {"240226046865", "171024011665"}

    @pytest.mark.asyncio
    async def test_a_row_is_recovered_once_and_never_again(self):
        from naukri_server.tools.apply import _recover_pending_questions

        rows = [legacy_row("legacy", 2), modern_row("done", [q("CTC?")])]
        with patch("naukri_server.database.list_applications",
                   new=AsyncMock(return_value=(rows, 2))), \
                patch("naukri_server.tools.apply.api_client.post", new=AsyncMock()):
            result = await _recover_pending_questions()

        assert [t["job_id"] for t in result["would_refetch"]] == ["legacy"]
        assert result["skipped_already_recovered"] == ["done"]

    @pytest.mark.asyncio
    async def test_job_ids_restricts_the_target_set(self):
        from naukri_server.tools.apply import _recover_pending_questions

        rows = [legacy_row("a", 1), legacy_row("b", 2), legacy_row("c", 3)]
        with patch("naukri_server.database.list_applications",
                   new=AsyncMock(return_value=(rows, 3))), \
                patch("naukri_server.tools.apply.api_client.post", new=AsyncMock()):
            result = await _recover_pending_questions(job_ids=["a", "zzz"])

        assert [t["job_id"] for t in result["would_refetch"]] == ["a"]
        assert result["not_needs_input"] == ["zzz"]


class TestRecoveryCannotCarryAnswers:
    def test_the_probe_has_no_answers_parameter(self):
        """THE REQUIRED TEST, part 1: structural, not intentional."""
        from naukri_server.tools.apply import _refetch_questions_only

        params = inspect.signature(_refetch_questions_only).parameters
        assert list(params) == ["job_id"]
        assert "answers" not in params

    def test_the_probe_body_never_constructs_applyData(self):
        """Part 2: applyData is the ONLY field answers can travel through."""
        from naukri_server.tools.apply import _refetch_questions_only

        source = inspect.getsource(_refetch_questions_only)
        body = source.split('"""', 2)[2]  # the docstring names applyData; the code must not
        assert "applyData" not in body
        assert "_apply_single" not in body
        assert "_load_cache" not in body

    @pytest.mark.asyncio
    async def test_the_posted_body_carries_no_answers(self):
        """Part 3: assert it on the wire, not just in the source."""
        from naukri_server.tools.apply import _refetch_questions_only

        post = AsyncMock(return_value={"jobs": [{"status": 400, "questionnaire": QUESTIONNAIRE}]})
        patches = recover_env(post=post)
        for p in patches:
            p.start()
        try:
            result = await _refetch_questions_only("240226046865")
        finally:
            for p in patches:
                p.stop()

        sent = post.await_args.args[1]
        assert "applyData" not in sent
        assert sent["strJobsarr"] == ["240226046865"]
        assert "answer" not in json.dumps(sent).lower()
        assert result["status"] == "recovered"
        assert len(result["questions"]) == 3

    @pytest.mark.asyncio
    async def test_the_probe_never_reads_the_answer_cache(self):
        """Reading the cache is what would let auto-answers be submitted."""
        from naukri_server.tools.apply import _refetch_questions_only

        def explode(*a, **k):
            raise AssertionError("the recovery probe must never read the answer cache")

        post = AsyncMock(return_value={"jobs": [{"status": 400, "questionnaire": QUESTIONNAIRE}]})
        patches = recover_env(post=post) + [
            patch("naukri_server.tools.apply._load_cache", side_effect=explode),
            patch("naukri_server.cache._load_cache", side_effect=explode),
        ]
        for p in patches:
            p.start()
        try:
            result = await _refetch_questions_only("240226046865")
        finally:
            for p in patches:
                p.stop()

        assert result["status"] == "recovered"

    @pytest.mark.asyncio
    async def test_a_fully_cached_questionnaire_still_submits_nothing(self):
        """The exact case _apply_single would have auto-submitted.

        Every question below is already answered in the cache. Through
        _apply_single that fills auto_answers and POSTs them. The probe must
        return the questions and submit nothing.
        """
        from naukri_server.tools.apply import _refetch_questions_only
        from naukri_server.cache import _cache_key

        full_cache = {_cache_key(x["questionName"], x["answerOption"]): {"answer": "16"}
                      for x in QUESTIONNAIRE}
        post = AsyncMock(return_value={"jobs": [{"status": 400, "questionnaire": QUESTIONNAIRE}]})
        patches = recover_env(post=post) + [
            patch("naukri_server.tools.apply._load_cache", return_value=full_cache)]
        for p in patches:
            p.start()
        try:
            result = await _refetch_questions_only("240226046865")
        finally:
            for p in patches:
                p.stop()

        assert post.await_count == 1
        assert "applyData" not in post.await_args.args[1]
        assert len(result["questions"]) == 3


class TestRecoveryPersistsAndReportsHonestly:
    @pytest.mark.asyncio
    async def test_confirm_true_persists_the_new_shape(self):
        from naukri_server.tools.apply import _recover_pending_questions

        rows = [legacy_row("240226046865", 3)]
        record = AsyncMock()
        post = AsyncMock(return_value={"jobs": [{"status": 400, "questionnaire": QUESTIONNAIRE}]})
        patches = recover_env(post=post) + [
            patch("naukri_server.database.list_applications",
                  new=AsyncMock(return_value=(rows, 1))),
            patch("naukri_server.tools.apply.record_application", new=record),
        ]
        for p in patches:
            p.start()
        try:
            result = await _recover_pending_questions(confirm=True)
        finally:
            for p in patches:
                p.stop()

        assert result["status"] == "success"
        assert result["recovered_count"] == 1
        stored = record.await_args.kwargs["extra"]["pending_questions"]
        assert isinstance(stored, list) and len(stored) == 3
        assert record.await_args.kwargs["status"] == "needs_input"
        assert "answer" not in json.dumps(stored).lower()

    @pytest.mark.asyncio
    async def test_an_unexpected_apply_is_reported_not_hidden(self):
        """No read-only questionnaire endpoint exists; a 200 means applied."""
        from naukri_server.tools.apply import _recover_pending_questions

        rows = [legacy_row("240226046865", 3)]
        record = AsyncMock()
        post = AsyncMock(return_value={"jobs": [{"status": 200}]})
        patches = recover_env(post=post) + [
            patch("naukri_server.database.list_applications",
                  new=AsyncMock(return_value=(rows, 1))),
            patch("naukri_server.tools.apply.record_application", new=record),
        ]
        for p in patches:
            p.start()
        try:
            result = await _recover_pending_questions(confirm=True)
        finally:
            for p in patches:
                p.stop()

        assert result["recovered_count"] == 0
        assert len(result["applied_unexpectedly"]) == 1
        # and the row is told the truth, so recovery never re-opens it
        assert record.await_args.kwargs["status"] == "applied"

    @pytest.mark.asyncio
    async def test_a_tripped_kill_switch_halts_recovery(self):
        """The apply-path rail is called, not bypassed."""
        from naukri_server.tools.apply import _recover_pending_questions
        from naukri_server import kill_switch

        rows = [legacy_row("240226046865", 3)]
        post = AsyncMock()
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        tripped = kill_switch.KillSwitchTrippedError("blocked")
        tripped.block_kind = "captcha"

        with patch("naukri_server.database.list_applications",
                   new=AsyncMock(return_value=(rows, 1))), \
                patch("naukri_server.tools.apply.api_client.post", new=post), \
                patch("naukri_server.tools.apply.get_apply_rate_limiter", return_value=limiter), \
                patch("naukri_server.tools.apply.kill_switch.guard", side_effect=tripped), \
                patch("naukri_server.tools.apply.record_application", new=AsyncMock()):
            result = await _recover_pending_questions(confirm=True)

        assert result["status"] == "halted"
        assert result["error_code"] == "KILL_SWITCH_TRIPPED"
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_failed_job_does_not_stop_the_sweep(self):
        from naukri_server.tools.apply import _recover_pending_questions

        rows = [legacy_row("bad", 1), legacy_row("good", 3)]
        record = AsyncMock()
        post = AsyncMock(side_effect=[
            RuntimeError("transport blew up"),
            {"jobs": [{"status": 400, "questionnaire": QUESTIONNAIRE}]},
        ])
        patches = recover_env(post=post) + [
            patch("naukri_server.database.list_applications",
                  new=AsyncMock(return_value=(rows, 2))),
            patch("naukri_server.tools.apply.record_application", new=record),
        ]
        for p in patches:
            p.start()
        try:
            result = await _recover_pending_questions(confirm=True)
        finally:
            for p in patches:
                p.stop()

        assert result["status"] == "partial_success"
        assert result["recovered_count"] == 1
        assert result["failed"][0]["job_id"] == "bad"


# ---------------------------------------------------------------------------
# 5. Row reading: the extra column round trip
# ---------------------------------------------------------------------------

class TestRowExtraReading:
    def test_reads_pending_questions_out_of_the_extra_json_column(self):
        from naukri_server.tools.tracking import _row_pending_raw

        assert _row_pending_raw(legacy_row("j", 4)) == 4
        assert _row_pending_raw(modern_row("j", [q("CTC?")])) == [q("CTC?")]

    def test_falls_back_to_a_flat_row(self):
        from naukri_server.tools.tracking import _row_pending_raw

        assert _row_pending_raw({"job_id": "j", "pending_questions": 2}) == 2

    def test_corrupt_extra_json_does_not_raise(self):
        from naukri_server.tools.tracking import _row_pending_raw

        assert _row_pending_raw({"job_id": "j", "extra": "{not json"}) is None

    def test_other_extra_fields_are_untouched_by_reading(self):
        from naukri_server.tools.tracking import _row_pending_raw

        row = {"job_id": "j",
               "extra": json.dumps({"pending_questions": 4, "location": "Pune", "salary": "10L"})}
        assert _row_pending_raw(row) == 4
        assert json.loads(row["extra"])["location"] == "Pune"


# ---------------------------------------------------------------------------
# 6. The third shape: the JSON->SQLite migration stores bare question strings
# ---------------------------------------------------------------------------

class TestMigratedStringShape:
    def test_a_list_of_bare_strings_keeps_its_text(self):
        """tests/test_json_migration.py round-trips ["Notice period?"].

        Dropping non-dicts would report text_recoverable=True over an EMPTY
        list -- "no questions" for a row that has one. The string is the text.
        """
        from naukri_server.tools.tracking import read_pending_questions

        read = read_pending_questions(["Notice period?", "Expected CTC?"])
        assert read["count"] == 2
        assert read["text_recoverable"] is True
        assert [x["question"] for x in read["questions"]] == ["Notice period?", "Expected CTC?"]

    @pytest.mark.asyncio
    async def test_a_migrated_row_reports_its_questions(self):
        from naukri_server.tools.tracking import _pending_questions_report

        p1, p2 = patch_report_db([modern_row("migrated", ["Notice period?"])])
        with p1, p2:
            report = await _pending_questions_report()

        app = report["applications"][0]
        assert app["question_count"] == 1
        assert app["questions"][0]["question"] == "Notice period?"
        assert app["questions"][0]["answered"] is False
        assert report["total_questions"] == 1


class TestTruncationIsDisclosed:
    @pytest.mark.asyncio
    async def test_has_more_is_false_when_everything_fits(self):
        from naukri_server.tools.tracking import _pending_questions_report

        p1, p2 = patch_report_db(real_distribution_rows())
        with p1, p2:
            report = await _pending_questions_report()
        assert report["has_more"] is False
        assert report["count"] == report["total_needs_input"] == 17

    @pytest.mark.asyncio
    async def test_has_more_is_true_when_limit_truncates(self):
        """Cheapest-first over a truncated set is not a global ranking."""
        from naukri_server.tools.tracking import _pending_questions_report

        rows = real_distribution_rows()[:5]
        with patch("naukri_server.database.list_applications",
                   new=AsyncMock(return_value=(rows, 40))), \
                patch("naukri_server.cache._load_cache", return_value={}):
            report = await _pending_questions_report(limit=5)

        assert report["has_more"] is True
        assert report["count"] == 5
        assert report["total_needs_input"] == 40
