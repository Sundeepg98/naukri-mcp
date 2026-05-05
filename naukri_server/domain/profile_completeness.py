"""Profile completeness assessment — grading, gap detection, actionable tips."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProfileGap:
    """A single gap in the user's profile."""
    section: str
    action: str
    impact: str  # "high", "medium", "low"


@dataclass(frozen=True)
class CompletionReport:
    """Result of profile completeness analysis.

    Invariants:
    - grade is always A, B, C, or D
    - tips is never empty
    """
    completeness_pct: int | None
    grade: str
    strengths: tuple[str, ...]
    gaps: tuple[ProfileGap, ...]
    tips: tuple[str, ...]

    @classmethod
    def from_profile(cls, profile_data: dict, completeness_pct: int | None = None) -> "CompletionReport":
        """Pure computation — extract gap detection and grading from profile data.

        Args:
            profile_data: Dict with the same shape as _get_profile() returns
                          (key_skills, employment, education, current_ctc, etc.).
            completeness_pct: Optional completeness percentage from the dashboard API.

        Returns:
            A frozen CompletionReport with grade, strengths, gaps, and tips.
        """
        strengths: list[str] = []
        gaps: list[ProfileGap] = []

        # --- Check key skills ---
        key_skills = profile_data.get("key_skills")
        if key_skills:
            if isinstance(key_skills, str):
                skill_count = len([s.strip() for s in key_skills.split(",") if s.strip()])
            elif isinstance(key_skills, list):
                skill_count = len(key_skills)
            else:
                skill_count = 0

            if skill_count >= 10:
                strengths.append(f"Good skill coverage ({skill_count} skills listed)")
            elif skill_count > 0:
                gaps.append(ProfileGap(
                    section="Key Skills",
                    action=f"Add more skills (currently {skill_count}, aim for 15+)",
                    impact="high",
                ))
        else:
            gaps.append(ProfileGap(
                section="Key Skills",
                action="Add relevant skills to your profile",
                impact="high",
            ))

        # --- Check employment history ---
        employment = profile_data.get("employment", [])
        if employment:
            strengths.append(f"Employment history present ({len(employment)} entries)")
            current = [e for e in employment if e.get("end_date") == "Present"]
            if not current:
                gaps.append(ProfileGap(
                    section="Employment",
                    action="Mark your current job (no entry shows 'Present')",
                    impact="medium",
                ))
        else:
            gaps.append(ProfileGap(
                section="Employment",
                action="Add your employment history",
                impact="high",
            ))

        # --- Check education ---
        education = profile_data.get("education", [])
        if education:
            strengths.append(f"Education details present ({len(education)} entries)")
        else:
            gaps.append(ProfileGap(
                section="Education",
                action="Add your educational qualifications",
                impact="medium",
            ))

        # --- Check CTC info ---
        if profile_data.get("current_ctc"):
            strengths.append("Current CTC specified")
        else:
            gaps.append(ProfileGap(
                section="Current CTC",
                action="Add your current CTC for better job matching",
                impact="medium",
            ))

        if profile_data.get("expected_ctc"):
            strengths.append("Expected CTC specified")
        else:
            gaps.append(ProfileGap(
                section="Expected CTC",
                action="Add your expected CTC to filter relevant jobs",
                impact="medium",
            ))

        # --- Check notice period ---
        if profile_data.get("notice_period"):
            strengths.append(f"Notice period set: {profile_data['notice_period']}")
        else:
            gaps.append(ProfileGap(
                section="Notice Period",
                action="Set your notice period — recruiters filter by availability",
                impact="high",
            ))

        # --- Check skills with experience ---
        skills_exp = profile_data.get("skills_with_experience", [])
        if skills_exp:
            with_years = [s for s in skills_exp if s.get("experience_years", 0) > 0]
            if with_years:
                strengths.append(f"{len(with_years)} skills have experience years specified")
            if len(skills_exp) > len(with_years):
                gaps.append(ProfileGap(
                    section="IT Skills",
                    action=f"Add experience years for {len(skills_exp) - len(with_years)} skills missing them",
                    impact="medium",
                ))

        # --- Calculate grade ---
        if completeness_pct is not None:
            if completeness_pct >= 80:
                grade = "A"
            elif completeness_pct >= 60:
                grade = "B"
            elif completeness_pct >= 40:
                grade = "C"
            else:
                grade = "D"
        else:
            if len(gaps) == 0:
                grade = "A"
            elif len(gaps) <= 2:
                grade = "B"
            elif len(gaps) <= 4:
                grade = "C"
            else:
                grade = "D"

        # --- Tips ---
        tips: list[str] = []
        if grade in ("C", "D"):
            tips.append("Profiles with 80%+ completeness get 3x more recruiter views")
        if not any(g.section == "Key Skills" for g in gaps):
            tips.append("Update your skills regularly to match trending job requirements")
        else:
            tips.append("Profiles with 15+ skills appear in more search results")
        tips.append("Use naukri_boost_profile() daily to stay in 'recently active' searches")
        if gaps:
            high_impact = [g for g in gaps if g.impact == "high"]
            if high_impact:
                tips.append(f"Priority: Fix {len(high_impact)} high-impact gap(s) first")

        return cls(
            completeness_pct=completeness_pct,
            grade=grade,
            strengths=tuple(strengths),
            gaps=tuple(gaps),
            tips=tuple(tips),
        )

    def to_dict(self) -> dict:
        return {
            "completeness_pct": self.completeness_pct,
            "grade": self.grade,
            "strengths": list(self.strengths),
            "gaps": [{"section": g.section, "action": g.action, "impact": g.impact} for g in self.gaps],
            "tips": list(self.tips),
        }
