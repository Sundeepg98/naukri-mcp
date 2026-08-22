"""Profile targeting tools — DFP ad-system profile view + gap analysis."""

from naukri_server.interfaces import api_client
from naukri_server.config import DFP_PROFILE_API


async def _do_targeting() -> dict:
    """Fetch DFP targeting profile and identify completeness gaps.

    Returns how Naukri's ad system sees the profile: 35 targeting fields,
    completeness gaps, and ad slot count.
    """
    data = await api_client.get(DFP_PROFILE_API)
    params = data.get("params", {})

    # Identify completeness gaps (empty Profile-* fields)
    gaps = []
    for field, value in params.items():
        if field.startswith("Profile-") and not value and value != 0:
            clean_name = field.replace("Profile-", "").replace("-", " ").lower()
            gaps.append(clean_name)

    return {
        "status": "success",
        "targeting_fields": len(params),
        "profile": {
            # NOT his CTC, and no longer named as though it were.
            #
            # `Profile-CTC` is Naukri's ROUNDED ad-targeting bucket. Measured
            # 2026-08-22 against his live account: this returns "17.0" while
            # naukri_get_profile.current_ctc is 1250000 and naukri_dashboard
            # (rawCtc) is "12.50". Both wore the field name `ctc_lpa`, so
            # "what is his CTC" answered 12.50 or 17.0 depending on which tool
            # was asked, with nothing saying they were different quantities.
            # Half a lakh is real money in a negotiation.
            #
            # Renamed rather than corrected: the bucket is the right value for
            # a tool about TARGETING -- it is what recruiters' filters actually
            # match him against. It just is not his salary.
            "targeting_ctc_bucket": params.get("Profile-CTC"),
            "ctc_lpa_note": (
                "targeting_ctc_bucket is Naukri's rounded targeting value, not "
                "your CTC. For the real figure use naukri_get_profile "
                "(current_ctc) or naukri_dashboard (ctc_lpa)."
            ),
            "experience_years": params.get("Profile-Experience"),
            "age": params.get("Profile-Age"),
            "gender": params.get("Profile-Gender"),
            "location": params.get("Profile-Location"),
            "preferred_locations": params.get("Profile-Pref-Loc") or None,
            "company": params.get("Profile-Company"),
            "designation": params.get("Profile-Designation"),
            "functional_area": params.get("Profile-FAREA"),
            "role": params.get("Profile-Role"),
            "industry": params.get("Profile-Industry"),
            "key_skills": params.get("Profile-KeySkills"),
            "institute": params.get("Profile-Institute"),
            "ug_course": params.get("Profile-UG-Course"),
            "ug_specialization": params.get("Profile-UG-spl"),
            "ug_year": params.get("Profile-UG-yearpass"),
            "ug_cgpa": params.get("Profile-UG-percent"),
            "pg_course": params.get("Profile-PG-Course") or None,
            "pg_specialization": params.get("Profile-PG-Spl") or None,
            "account_age_days": params.get("Profile-Registerdays"),
            "activeness_score": params.get("Profile-Activeness"),
        },
        "completeness_gaps": gaps,
        "gap_count": len(gaps),
        "ad_slots": len(data.get("slots", [])),
        "raw_params": params,
    }
