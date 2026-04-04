"""Job domain object — consolidates job parsing from all API sources."""

import logging
from dataclasses import dataclass
from typing import Optional

from naukri_server.config import LAKHS_MULTIPLIER

logger = logging.getLogger(__name__)


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
