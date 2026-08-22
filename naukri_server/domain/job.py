"""Job domain object — consolidates job parsing from all API sources."""

import logging
from dataclasses import dataclass
from typing import Optional

from naukri_server.config import LAKHS_MULTIPLIER

logger = logging.getLogger(__name__)


# Naukri's `wfhType` codes. The scoring engine understands the WORDS
# ("remote"/"hybrid"/"office") and classifies anything it does not recognise as
# office, so a raw code costs the job its work-mode bonus silently: measured
# 2026-08-22, his InApp "MCP Developer (Remote)" arrived as wfhType "2" and
# scored work_mode 0 instead of 5 (fit 80 where it should read 85). Remote jobs
# -- the ones he most wants -- were the ones losing the bonus.
#
# The map existed but was applied in only ONE of the three parse paths
# (parse_job_detail_v1). Everything reaching assess_fit / compare_jobs /
# auto_hunt went through the other two, unmapped.
WORK_MODE_CODES = {"0": "office", "2": "remote", "3": "hybrid"}


# Work-mode words as Naukri prints them at the head of a location label.
# Mapped to the words the scoring engine categorises.
_LOCATION_MODE_PREFIXES = {
    "remote": "remote",
    "work from home": "remote",
    "wfh": "remote",
    "hybrid": "hybrid",
    "work from office": "office",
}


def work_mode_from_location(label):
    """Work mode read out of a location label, or None.

    LIST endpoints do not carry the mode as a field. Measured against the live
    recommendations payload 2026-08-22: a list item has 42 keys and NEITHER
    `wfhType` NOR `workMode` is among them. Its `mode` key is the listing
    source, not the work arrangement -- across 53 jobs it read jp 43 / airex 8
    / crawled 2. Only the job-detail endpoint carries `wfhType`.

    The mode IS in the list payload, embedded in the location placeholder:
    "Hybrid - Bengaluru, Gurugram, Pune", "Remote". Across all 21 distinct
    labels in that sample the word is ALWAYS the head -- standing alone, or
    ahead of " - " -- so this reads the head and matches it exactly rather than
    searching the string. No Indian city is named "Remote" or "Hybrid", and a
    substring search would misread e.g. a company or area name.

    ABSENT MEANS None, NEVER "office". Naukri prints the prefix only for remote
    and hybrid roles (16 of 53 here, 30%), so a missing prefix is genuinely no
    information -- and defaulting it to office is precisely the bug this whole
    line of work exists to fix, in a different costume. An honest None scores no
    bonus; a guessed "office" would silently deny a remote job its +5.
    """
    if not label:
        return None
    head = str(label).split(" - ")[0].strip().lower()
    return _LOCATION_MODE_PREFIXES.get(head)


def normalize_work_mode(value):
    """Naukri work mode as a WORD the scoring engine understands.

    Numeric `wfhType` codes are decoded; text values (`workMode` is already
    "Hybrid" on some endpoints) pass through unchanged; empty stays None. An
    unknown code is returned as-is rather than guessed at -- it will not score,
    but it also will not silently claim to be an office job.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return WORK_MODE_CODES.get(text, text)


@dataclass
class ParsedSalary:
    """Value object for parsed salary with logging for missing data."""

    label: str
    min_lakhs: Optional[float]
    max_lakhs: Optional[float]
    is_hidden: bool = False

    @classmethod
    def from_api(cls, salary_detail: dict) -> "ParsedSalary":
        """Single place for salary extraction — replaces duplicated implementations.

        Handles salary_detail dicts from both search results (jobDetails[].salaryDetail)
        and job detail API (jobDetails.salaryDetail).

        Logs warning if salary data is present but unparseable.
        """
        if not salary_detail or not isinstance(salary_detail, dict):
            logger.warning("Missing or invalid salary_detail: %s", type(salary_detail).__name__)
            return cls(label="Not Disclosed", min_lakhs=None, max_lakhs=None)

        sal_min = salary_detail.get("minimumSalary", 0)
        sal_max = salary_detail.get("maximumSalary", 0)
        sal_label = salary_detail.get("label", "")
        is_hidden = salary_detail.get("hideSalary", False)

        if sal_label:
            label = sal_label
        elif sal_max:
            label = f"{sal_min/LAKHS_MULTIPLIER:.1f}-{sal_max/LAKHS_MULTIPLIER:.1f} LPA"
        else:
            label = "Not Disclosed"

        min_lpa = round(sal_min / LAKHS_MULTIPLIER, 1) if sal_min else None
        max_lpa = round(sal_max / LAKHS_MULTIPLIER, 1) if sal_max else None

        if sal_min and sal_max and sal_min > sal_max:
            logger.warning("Salary min (%s) exceeds max (%s)", sal_min, sal_max)

        return cls(label=label, min_lakhs=min_lpa, max_lakhs=max_lpa, is_hidden=is_hidden)
