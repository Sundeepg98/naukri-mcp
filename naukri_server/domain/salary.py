"""Salary value object — parsing, normalization, and market positioning.

RE-EXPORT SHIM. The implementation moved to ``jobcore.salary``. The one piece
that is genuinely Naukri's — how many rupees make a lakh — stays here and is
injected, so the shared library never has to import this server's config.

Every import path that worked before still works:

    from naukri_server.domain.salary import Salary

``Salary`` below is a Naukri-bound subclass of ``jobcore.salary.Salary``: same
constructor, same fields, same methods, and it follows
``naukri_server.config.LAKHS_MULTIPLIER`` if that constant is ever changed.
"""

from jobcore.salary import Salary as _JobcoreSalary
from jobcore.salary import SalaryConfig

from naukri_server.config import LAKHS_MULTIPLIER

#: Naukri's unit convention, handed to jobcore rather than imported by it.
SALARY_CONFIG = SalaryConfig(lakhs_multiplier=LAKHS_MULTIPLIER)


class Salary(_JobcoreSalary):
    """Immutable value object for salary in lakhs per annum.

    Encapsulates:
    - String parsing with unit detection (>200 = raw rupees)
    - Market positioning (below/at/above)
    - CTC comparison scoring

    Behaviour is identical to the pre-extraction implementation; see
    ``jobcore.salary.Salary`` for the logic.
    """

    CONFIG = SALARY_CONFIG


__all__ = ["Salary", "SalaryConfig", "SALARY_CONFIG"]
