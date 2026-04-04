"""Skill gap analysis — find systematic gaps across multiple job listings.

Business logic lives in naukri_server.services.insights_service.
This module re-exports the function so existing imports and test patch-paths remain valid.
"""

# Re-export from service layer — preserves import path and patch target
from naukri_server.services.insights_service import (  # noqa: F401
    skill_gap_analysis as _skill_gap_analysis,
)

naukri_skill_gap_analysis = _skill_gap_analysis
