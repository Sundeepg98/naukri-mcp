"""`ctc_lpa` meant two different quantities depending on which tool you asked.

The disagreement was measured on 2026-08-22. The figures below are synthetic
stand-ins that preserve the relationship that matters: the targeting bucket
rounds the real CTC up to the next whole lakh, so the two differ by 0.5.

    naukri_get_profile.current_ctc   1250000        <- the profile field
    naukri_dashboard.ctc_lpa         "12.50"        <- rawCtc, agrees
    naukri_profile_targeting.ctc_lpa "13.0"         <- Profile-CTC, does NOT

Two tools published the same field name for different quantities, so "what is
my CTC" answered 12.50 or 13.0 depending on which one was asked, with nothing
in either response saying they were not the same thing. Half a lakh is real
money in a salary negotiation, and this is exactly the shape that surfaces
there rather than in a test.

`Profile-CTC` is Naukri's ROUNDED ad-targeting bucket. It is the right value
for a tool about targeting -- it is what recruiters' filters match him against
-- so it is renamed to `targeting_ctc_bucket` rather than corrected, and the
response points at the authoritative sources.
"""

from unittest.mock import AsyncMock, patch

import pytest

# Same field set and value shapes as a live targeting-params response;
# the values themselves are synthetic.
LIVE_PARAMS = {
    "Profile-CTC": "13.0",
    "Profile-Experience": "4.00",
    "Profile-Location": "Example City",
    "Profile-Company": "Acme Technology",
    "Profile-Designation": "Software Engineer, Backend",
}


async def _run():
    from naukri_server.tools.profile_targeting import _do_targeting

    with patch("naukri_server.interfaces.api_client.get",
               new_callable=AsyncMock, return_value={"params": LIVE_PARAMS}):
        return await _do_targeting()


@pytest.mark.asyncio
async def test_the_targeting_value_no_longer_claims_to_be_his_ctc():
    """THE regression: this key used to be `ctc_lpa`."""
    result = await _run()
    profile = result["profile"]

    assert "ctc_lpa" not in profile, (
        "the rounded targeting bucket must not wear the same name as the real "
        "CTC that naukri_dashboard and naukri_get_profile report")
    assert profile["targeting_ctc_bucket"] == "13.0"


@pytest.mark.asyncio
async def test_the_response_says_where_the_real_figure_lives():
    """Renaming alone would leave a caller guessing which tool to ask."""
    result = await _run()
    note = result["profile"]["ctc_lpa_note"]

    assert "naukri_get_profile" in note
    assert "naukri_dashboard" in note
    assert "not your CTC" in note


@pytest.mark.asyncio
async def test_the_other_targeting_fields_are_untouched():
    """Only the colliding name changed; this tool is about targeting."""
    profile = (await _run())["profile"]
    assert profile["experience_years"] == "4.00"
    assert profile["location"] == "Example City"
    assert profile["company"] == "Acme Technology"


def test_the_two_sources_are_genuinely_different_numbers():
    """CONTROL, so this file is not pinning a coincidence: 12.50 from the
    profile's own current_ctc, 13.0 from the targeting bucket."""
    real_ctc_rupees = 1250000            # naukri_get_profile.current_ctc
    real_lpa = real_ctc_rupees / 100000
    targeting = float(LIVE_PARAMS["Profile-CTC"])

    assert real_lpa == 12.5
    assert targeting != real_lpa, "if these ever agree the bug is invisible again"
    assert abs(targeting - real_lpa) == 0.5
