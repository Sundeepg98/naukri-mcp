"""Naukri work-mode codes, settled against the live API on 2026-08-22.

Two bugs, one root cause: `wfhType` is a numeric code, and the codebase had two
DISAGREEING opinions about what the numbers meant.

1. THE SCORER NEVER SAW A WORD. `jobcore`'s ScoringEngine classifies work mode
   by name ("remote"/"hybrid"/"office") and treats anything unrecognised as
   office, worth 0. Raw codes reached it from two of the three parse paths, so
   every remote job scored work_mode 0 instead of 5. Measured: his InApp
   "MCP Developer (Remote)" came back as work_mode "2" and scored 80 where it
   should read 85. Remote roles -- the ones he most wants -- were the ones
   losing the bonus. A map already existed but was applied only in
   parse_job_detail_v1.

2. THE SEARCH FILTER SENT THE WRONG CODE. WORK_MODE_MAP said office="2" while
   WORK_MODE_CODES said "2" means remote. Driven live:

       work_mode="office" -> wfhType=2 -> first result located "Remote"
       work_mode="wfh"    -> wfhType=1 -> 2 results ("Temp. WFH - Ahmedabad")
       work_mode="hybrid" -> wfhType=3 -> "Hybrid - Bengaluru"  (already right)

   Corroborated from the response side: the job recommendations labelled
   "Hybrid - Chennai" carries wfhType 3, and the one the inbox called
   "(Remote)" carries 2.
"""

import pytest

from naukri_server.domain.job import WORK_MODE_CODES, normalize_work_mode
from naukri_server.services.search_service import WORK_MODE_MAP


class TestDecodingTheResponse:
    def test_the_two_codes_seen_live_decode_to_what_naukri_displayed(self):
        # 190826014594 Bounteous -- recommendations showed "Hybrid - Chennai"
        assert normalize_work_mode("3") == "hybrid"
        # 210826815108 InApp -- inbox subject was "MCP Developer (Remote)"
        assert normalize_work_mode("2") == "remote"
        assert normalize_work_mode("0") == "office"

    def test_text_values_pass_through_untouched(self):
        """`workMode` is already a word on some endpoints; decoding must be
        idempotent rather than mangling it."""
        assert normalize_work_mode("Hybrid") == "Hybrid"
        assert normalize_work_mode("Remote") == "Remote"

    def test_empty_becomes_none_not_a_default_office(self):
        assert normalize_work_mode(None) is None
        assert normalize_work_mode("") is None
        assert normalize_work_mode("   ") is None

    def test_an_unknown_code_is_returned_not_guessed(self):
        """It will not score, but it must not silently claim to be office."""
        assert normalize_work_mode("9") == "9"


class TestTheScoringConsequence:
    """THE regression, at the level that actually cost him points."""

    def test_a_remote_job_scores_the_remote_bonus_not_zero(self):
        engine = pytest.importorskip("jobcore.scoring").ScoringEngine()

        raw_code_score = engine.score_work_mode("2")          # what used to arrive
        decoded_score = engine.score_work_mode(normalize_work_mode("2"))

        assert raw_code_score == 0, (
            "if a bare code now scores, jobcore learned the codes and this "
            "decode may be redundant -- re-check before deleting it"
        )
        assert decoded_score == 5, "a remote job must earn the remote bonus"

    def test_a_hybrid_job_scores_the_hybrid_bonus(self):
        engine = pytest.importorskip("jobcore.scoring").ScoringEngine()
        assert engine.score_work_mode(normalize_work_mode("3")) == 3

    def test_every_response_code_maps_to_something_the_scorer_knows(self):
        """CONTROL: a decode the scorer cannot categorise is not a decode."""
        engine = pytest.importorskip("jobcore.scoring").ScoringEngine()
        for code in WORK_MODE_CODES:
            word = normalize_work_mode(code)
            assert engine.work_mode_category(word) is not None, (code, word)


class TestTheRequestAndResponseAgree:
    def test_the_filter_sends_the_code_the_response_uses(self):
        """The bug in one line: office used to send the remote code."""
        for word, code in WORK_MODE_MAP.items():
            decoded = WORK_MODE_CODES[code]
            expected = "remote" if word in ("wfh", "remote") else word
            assert decoded == expected, (
                "search filter %r sends wfhType=%r, which the response map "
                "reads back as %r" % (word, code, decoded)
            )

    def test_office_no_longer_sends_the_remote_code(self):
        assert WORK_MODE_MAP["office"] != "2", "this is the exact live failure"
        assert WORK_MODE_CODES[WORK_MODE_MAP["office"]] == "office"

    def test_wfh_asks_for_remote_rather_than_the_narrow_temp_wfh_code(self):
        assert WORK_MODE_MAP["wfh"] == "2"

    def test_hybrid_was_already_right_and_stays_right(self):
        assert WORK_MODE_MAP["hybrid"] == "3"


class TestBothParsePathsDecode:
    """The map existed and was applied in one of three places. Pin all three."""

    def test_job_list_parser_decodes(self):
        from naukri_server.tools.job_parsing import _parse_job_list

        out = _parse_job_list([{"jobId": "1", "title": "T", "wfhType": "2"}], 10)
        assert out[0]["work_mode"] == "remote"

    def test_job_detail_parser_decodes(self):
        from naukri_server.services.search_service import parse_job_detail

        out = parse_job_detail(
            {"jobDetails": {"jobId": "1", "wfhType": "2"}}, "1", "http://x")
        assert out.get("work_mode") == "remote", out.get("work_mode")

    def test_v1_detail_parser_still_decodes(self):
        from naukri_server.services.search_service import parse_job_detail_v1

        out = parse_job_detail_v1({"job": {"jobId": "1", "wfhType": "2"}})
        assert out.get("work_mode") == "remote"
