"""Salary value object — parsing, normalization, and market positioning."""

import re
from dataclasses import dataclass
from naukri_server.config import LAKHS_MULTIPLIER


@dataclass(frozen=True)
class Salary:
    """Immutable value object for salary in lakhs per annum.

    Encapsulates:
    - String parsing with unit detection (>200 = raw rupees)
    - Market positioning (below/at/above)
    - CTC comparison scoring
    """
    min_lakhs: float | None
    max_lakhs: float | None
    raw: str

    @classmethod
    def from_string(cls, salary_str: str) -> "Salary":
        """Parse salary strings like '10-15 Lacs', 'Not disclosed'.

        Unit detection rule: if any value > 200, divide by LAKHS_MULTIPLIER.
        """
        if not salary_str or not isinstance(salary_str, str):
            return cls(None, None, salary_str or "")
        s = salary_str.lower().strip()
        if "not disclosed" in s or "confidential" in s:
            return cls(None, None, salary_str)
        nums = re.findall(r'(\d+(?:\.\d+)?)', s.replace(",", ""))
        if not nums:
            return cls(None, None, salary_str)
        vals = [float(n) for n in nums[:2]]
        if any(v > 200 for v in vals):
            vals = [v / LAKHS_MULTIPLIER for v in vals]
        if len(vals) >= 2:
            return cls(round(vals[0], 1), round(vals[1], 1), salary_str)
        return cls(round(vals[0], 1), round(vals[0], 1), salary_str)

    @property
    def is_disclosed(self) -> bool:
        return self.min_lakhs is not None

    @property
    def midpoint(self) -> float | None:
        if self.min_lakhs is not None and self.max_lakhs is not None:
            return (self.min_lakhs + self.max_lakhs) / 2
        return None

    def compare_to_ctc(self, expected_ctc) -> int:
        """Score salary fit against expected CTC. Returns 0, 3, or 5.

        5 = meets expectation, 3 = within 20%, 0 = below threshold.
        """
        # Defensive: ensure float (callers may pass string like "25")
        try:
            expected_ctc = float(expected_ctc)
        except (ValueError, TypeError):
            return 0
        if self.max_lakhs is None or self.max_lakhs > 200:
            return 0
        if self.max_lakhs >= expected_ctc:
            return 5
        if self.max_lakhs >= expected_ctc * 0.8:
            return 3
        return 0

    def market_position(self, market_avg: float) -> str:
        """Categorize as below/at/above market."""
        if self.midpoint is None:
            return "unknown"
        if self.midpoint < market_avg * 0.85:
            return "below"
        if self.midpoint > market_avg * 1.15:
            return "above"
        return "at_market"
