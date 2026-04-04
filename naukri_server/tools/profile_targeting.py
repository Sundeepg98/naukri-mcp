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
            "ctc_lpa": params.get("Profile-CTC"),
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
