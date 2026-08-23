"""The three apply-route keys the 37-key whitelist used to drop.

Every test is PURE: no network, no browser, no file I/O.

WHY THESE THREE, AND WHY NOW
----------------------------
`_parse_job_list` whitelists 37 keys and discards the rest. Three of the
discarded ones describe HOW a job is applied to: `mode`, `companyApplyJob`
and `clientCareersUrl`. A previous pass declined to surface them, and was
right to at the time: it had measured `companyApplyJob: false` on 20 of 20
jobs and had never obtained a `mode: "crawled"` job at all, so adding them
would have meant shipping three fields whose meaningful values nobody had
ever seen.

A wider census overturns that. Measured 2026-08-23 over N = 124 distinct
jobs from five surfaces that answered 200 (recommendations 63, early-access
50, applied history 11):

    mode              jp 54 | resdexSearch 50 | airex 5 | crawled 4 | absent 11
    companyApplyJob   false 58 | TRUE 5 | absent 61
    clientCareersUrl  9 non-null | absent 115

and the structure is clean: all 4 `mode: "crawled"` jobs carry
`companyApplyJob: true`, and a 5th `jp` job does too.

`clientCareersUrl` turns out to be a DIFFERENT signal, and its name invites
the wrong reading. All 9 sit on `mode: "jp"`, `consultant: true`,
`companyApplyJob: false` listings. The value is a Naukri careers-page slug,
not an external site: fetching `seedling-labs-jobs-careers-25659` resolves on
naukri.com and REDIRECTS to `pylon-management-consulting-jobs-careers-25659`
-- the consultancy that posted it, whose companyId is the 25659 in the slug.
So it is the client-branded route to the POSTER's Naukri page, and these
tests pin that it is rendered as a naukri.com URL and described as one.

ABSENT IS NOT FALSE. 61 of 124 jobs carry no `companyApplyJob` key at all
(every `resdexSearch` row, among others). Defaulting those to False would
manufacture a measurement -- it would say "this job does not use company
apply" about a payload that says nothing either way.
"""

import pytest

from naukri_server.tools.job_parsing import _parse_job_list


CRAWLED = {
    "jobId": "170826503985",
    "title": "AI Full stack Architect",
    "companyName": "Moodys Analytics",
    "mode": "crawled",
    "companyApplyJob": True,
}

CONSULTANCY = {
    "jobId": "220826010340",
    "title": "Senior Software Engineer",
    "companyName": "Pylon Management Consulting",
    "mode": "jp",
    "companyApplyJob": False,
    "consultant": True,
    "clientTitleString": "Seedling Labs",
    "clientCareersUrl": "seedling-labs-jobs-careers-25659",
}

RESDEX = {
    "jobId": "210826035150",
    "title": "GenAI + Nodejs Developer",
    "companyName": "Clover Infotech",
    "mode": "resdexSearch",
}

BARE = {"jobId": "1", "title": "T", "companyName": "C"}


def _one(job):
    parsed = _parse_job_list([job], limit=10)
    assert len(parsed) == 1
    return parsed[0]


class TestTheCrawledCompanyApplySignal:

    def test_crawled_job_surfaces_its_mode(self):
        assert _one(CRAWLED)["listing_mode"] == "crawled"

    def test_crawled_job_surfaces_company_apply(self):
        """The value the previous pass could never obtain: True."""
        assert _one(CRAWLED)["is_company_apply"] is True

    def test_ordinary_job_reports_company_apply_false(self):
        assert _one(CONSULTANCY)["is_company_apply"] is False

    def test_listing_mode_is_not_the_same_field_as_work_mode(self):
        """`mode` (jp/crawled/resdexSearch/airex) is a SOURCING channel.

        `work_mode` (remote/hybrid/office) already exists and means something
        entirely different. Naming this one `mode` would have collided.
        """
        parsed = _one(CONSULTANCY)
        assert parsed["listing_mode"] == "jp"
        assert "work_mode" in parsed
        assert parsed["work_mode"] != parsed["listing_mode"]


class TestAbsentIsNotFalse:
    """The whole point of surfacing a field is that it reports what is there."""

    def test_a_payload_with_no_companyApplyJob_reports_None_not_False(self):
        """61 of 124 measured jobs carry no such key. None is the honest answer.

        Defaulting to False would say "this job does not use company apply"
        about a payload that says nothing either way -- and `resdexSearch`,
        the single largest mode in the census at 50 jobs, never carries it.
        """
        assert _one(RESDEX)["is_company_apply"] is None

    def test_a_payload_with_no_mode_reports_None(self):
        assert _one(BARE)["listing_mode"] is None

    def test_a_payload_with_no_clientCareersUrl_reports_None(self):
        assert _one(RESDEX)["client_careers_url"] is None

    def test_a_bare_payload_still_parses(self):
        """The three new reads must not make a minimal payload throw."""
        parsed = _one(BARE)
        assert parsed["job_id"] == "1"
        assert parsed["listing_mode"] is None
        assert parsed["is_company_apply"] is None
        assert parsed["client_careers_url"] is None


class TestClientCareersUrlIsANaukriPage:

    def test_the_slug_is_rendered_as_a_full_naukri_url(self):
        """Same treatment `jdURL` already gets -- a caller can open it."""
        from naukri_server.config import NAUKRI_BASE
        assert _one(CONSULTANCY)["client_careers_url"] == (
            "%s/seedling-labs-jobs-careers-25659" % NAUKRI_BASE
        )

    def test_it_points_at_naukri_and_not_an_external_company_site(self):
        """The name says "careers url"; the value is a Naukri page.

        Measured: that slug resolves on naukri.com and redirects to the
        posting consultancy's own page. Anyone reading this field as an
        off-site apply link would be wrong, so the rendered value must be
        unambiguously a naukri.com URL.
        """
        url = _one(CONSULTANCY)["client_careers_url"]
        assert url.startswith("https://www.naukri.com/")

    def test_an_already_absolute_value_is_not_double_prefixed(self):
        """Defensive: if Naukri ever sends a full URL, do not mangle it."""
        job = dict(CONSULTANCY, clientCareersUrl="https://example.com/careers")
        assert _one(job)["client_careers_url"] == "https://example.com/careers"

    def test_it_sits_beside_the_client_company_fields_it_belongs_with(self):
        """It is the third leg of a set the parser already surfaced."""
        parsed = _one(CONSULTANCY)
        assert parsed["client_company"] == "Seedling Labs"
        assert parsed["is_consultant"] is True
        assert parsed["client_careers_url"].endswith("seedling-labs-jobs-careers-25659")
