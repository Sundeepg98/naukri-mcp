"""Invariants for naukri_search_impressions, from a live sweep on 2026-08-20.

An external reviewer flagged `daily_average: 2314` as impossible next to a
total of 5660 over a 30-day window. It is impossible -- but the arithmetic was
never wrong. Two FIELD NAMES were, and the live payload proves which:

    total_appearances : 5660   <- cumulative, ALL TIME, not the window
    daily_average     : 2314   <- appearances IN the 30-day window
    timeline          : {344, 614, 829, 527}  -> sums to exactly 2314

The timeline buckets summing to the "daily_average" figure is the decisive
evidence: 2314 is a window TOTAL. The upstream field is literally named
`dayWiseSearchAppearance`, and probing/analytics-report.md documented it in
March as "int - appearances in the requested time window". The mapping read
that as an average and the label stuck.

The old test suite could not catch this because it asserted the bug: a mock
returned `dayWiseSearchAppearance: 21.4` -- a fraction, chosen to look like an
average -- and the test asserted `daily_average == 21.4`. Pinning a value tells
you nothing when the value is a relabelled passthrough. These tests pin
RELATIONSHIPS instead, which a mislabel cannot satisfy.

All tests are PURE -- no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch


# A faithful slice of the 2026-08-20 live payload: the real ratios, the real
# Node/AWS spellings, and the real dirty keys (curly quotes, U+00A0 spaces).
LIVE_SHAPED_PAYLOAD = {
    "totalSearchAppearances": 5660,
    "recruiterActions": 332,
    "dayWiseSearchAppearance": 2314,
    "percentageChange": 12,
    "searchAppearanceTimeline": {
        "2026-08-19 - 2026-08-12": 344,
        "2026-08-11 - 2026-08-04": 614,
        "2026-08-03 - 2026-07-28": 829,
        "2026-07-27 - 2026-07-21": 527,
    },
    "searchKeyWords": {
        "Aws": 96,
        "Python": 94,
        "Node.js": 90,
        "Typescript": 72,
        "Node": 35,
        "Javascript": 32,
        "Node Js": 21,
        "Nodejs": 14,
        "Node-js": 1,
        "Type Script": 4,
        "“aws”": 1,
        "“java”": 1,
        " next.js": 1,
        "Criblclick House Monitoring Tool": 1,
    },
}


async def _call(days=30, **kwargs):
    from naukri_server.tools.performance import _get_search_impressions
    with patch("naukri_server.tools.performance.api_client.get",
               new_callable=AsyncMock, return_value=LIVE_SHAPED_PAYLOAD):
        return await _get_search_impressions(days=days, **kwargs)


# =====================================================================
# BUG 1 - the numbers must agree with their own names
# =====================================================================


class TestImpressionArithmetic:

    async def test_daily_average_times_window_equals_window_total(self):
        """The invariant the reviewer applied, and the tool must satisfy.

        RED before the fix: `daily_average` was the window total, so this came
        out 30x too large.
        """
        r = await _call(days=30)
        assert r["daily_average"] * 30 == pytest.approx(r["window_appearances"], rel=0.02)

    async def test_timeline_buckets_sum_to_the_window_total(self):
        """The strongest available check, and it is on the payload itself.

        Whatever the API calls it, the field that the timeline sums to IS the
        window total. Nothing else can be.
        """
        r = await _call(days=30)
        assert sum(r["timeline"].values()) == r["window_appearances"]

    async def test_window_total_never_exceeds_the_all_time_total(self):
        """A 30-day slice cannot be larger than every day ever recorded."""
        r = await _call(days=30)
        assert r["window_appearances"] <= r["total_appearances_all_time"]

    async def test_the_all_time_total_does_not_move_with_the_window(self):
        """It is cumulative, so asking for 7 days must not shrink it.

        This is what makes the name honest: a field that ignores `days` must
        not be sitting next to `days` wearing a window-shaped label.
        """
        seven = await _call(days=7)
        thirty = await _call(days=30)
        assert seven["total_appearances_all_time"] == thirty["total_appearances_all_time"]

    async def test_daily_average_scales_with_the_window(self):
        """Same window total over 7 days is a bigger daily rate than over 30."""
        seven = await _call(days=7)
        thirty = await _call(days=30)
        assert seven["daily_average"] > thirty["daily_average"]

    async def test_no_field_called_daily_average_holds_a_window_total(self):
        """The regression guard, stated as the reviewer would state it."""
        r = await _call(days=30)
        assert r["daily_average"] != r["window_appearances"], (
            "daily_average is the window total again -- this is the 2026-08-20 bug"
        )

    async def test_missing_upstream_fields_do_not_crash_the_arithmetic(self):
        """An absent dayWiseSearchAppearance must yield None, not an exception."""
        from naukri_server.tools.performance import _get_search_impressions
        with patch("naukri_server.tools.performance.api_client.get",
                   new_callable=AsyncMock, return_value={}):
            r = await _get_search_impressions(days=30)
        assert r["status"] == "success"
        assert r["daily_average"] is None
        assert r["window_appearances"] is None


# =====================================================================
# BUG 2 - keywords must be normalized, capped, and honest about the cap
# =====================================================================


class TestKeywordNormalization:

    async def test_node_variants_collapse_to_one_entry(self):
        """Five spellings of one runtime were five rows: 90/35/21/14/1.

        Collapsed they are the single largest keyword the operator has, which
        is the whole point of the field.
        """
        r = await _call(days=30, top_n=50)
        kw = r["top_keywords"]
        node_rows = [k for k in kw if "node" in k]
        assert len(node_rows) == 1, "node is still split across %r" % node_rows
        assert kw[node_rows[0]] == 90 + 35 + 21 + 14 + 1

    async def test_typographic_quotes_do_not_create_separate_entries(self):
        """The curly-quoted copies must merge into their plain twins."""
        r = await _call(days=30, top_n=50)
        assert not [k for k in r["top_keywords"] if "“" in k or "”" in k]

    async def test_no_key_carries_stray_whitespace_or_non_ascii_punctuation(self):
        r = await _call(days=30, top_n=50)
        for k in r["top_keywords"]:
            assert k == k.strip(), "key %r has stray whitespace" % k
            assert " " not in k, "key %r has a no-break space" % k

    async def test_top_n_caps_the_payload(self):
        r = await _call(days=30, top_n=3)
        assert len(r["top_keywords"]) == 3

    async def test_default_is_small_enough_to_be_cheap(self):
        """704 entries per call was the reported cost, in every response.

        Asserted against a keyword set LARGER than the default, so the cap is
        what bounds the answer -- against the 14-key fixture above this would
        pass without a cap existing at all.
        """
        from naukri_server.services.performance_service import DEFAULT_TOP_KEYWORDS
        from naukri_server.tools.performance import _get_search_impressions

        assert DEFAULT_TOP_KEYWORDS <= 20
        wide = dict(LIVE_SHAPED_PAYLOAD)
        wide["searchKeyWords"] = {"skill number %d" % i: 500 - i for i in range(300)}
        with patch("naukri_server.tools.performance.api_client.get",
                   new_callable=AsyncMock, return_value=wide):
            r = await _get_search_impressions(days=30)

        assert len(r["top_keywords"]) == DEFAULT_TOP_KEYWORDS
        assert r["keyword_stats"]["distinct_normalized"] == 300

    async def test_results_are_ordered_by_count_descending(self):
        r = await _call(days=30, top_n=10)
        counts = list(r["top_keywords"].values())
        assert counts == sorted(counts, reverse=True)

    async def test_nothing_is_hidden_silently_by_the_cap(self):
        """A cap that does not report what it dropped is a lie of omission."""
        r = await _call(days=30, top_n=3)
        stats = r["keyword_stats"]
        assert stats["returned"] == 3
        assert stats["distinct_raw"] == len(LIVE_SHAPED_PAYLOAD["searchKeyWords"])
        assert stats["distinct_normalized"] < stats["distinct_raw"]
        assert stats["distinct_normalized"] > stats["returned"]

    async def test_top_n_is_clamped_not_rejected(self):
        """An out-of-range top_n clamps, matching this codebase's convention
        that limits never raise VALIDATION_ERROR (see validation.py)."""
        r = await _call(days=30, top_n=100000)
        assert r["status"] == "success"
        assert len(r["top_keywords"]) <= r["keyword_stats"]["distinct_normalized"]

        r0 = await _call(days=30, top_n=0)
        assert r0["status"] == "success"
        assert len(r0["top_keywords"]) >= 1

    async def test_mashed_upstream_keywords_are_passed_through_not_invented_apart(self):
        """"Criblclick House Monitoring Tool" is Naukri's own mangling.

        This module does zero splitting: the raw field is a keyword->count map
        and we only normalize and rank it. Guessing where to cut a mashed string
        would invent data. It survives as one entry, lowercased -- visible, and
        honestly attributed upstream.
        """
        r = await _call(days=30, top_n=50)
        assert "criblclick house monitoring tool" in r["top_keywords"]
