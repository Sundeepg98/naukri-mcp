"""Shared fit scoring logic for job-profile matching."""

import re
from typing import Optional


# ── Skill Alias Map ──────────────────────────────────────────────────────────
# Canonical skill name → set of known aliases.
# parse_skills() normalizes all inputs through this map.

SKILL_ALIASES: dict[str, set[str]] = {
    "javascript": {"js", "vanilla js", "es6", "es2015", "ecmascript"},
    "typescript": {"ts"},
    "python": {"py", "python3"},
    "java": {"core java", "java8", "java11", "java17"},
    "c#": {"csharp", "c sharp"},
    "c++": {"cpp", "cplusplus"},
    "golang": {"go lang", "go"},
    "ruby": {"rb"},
    "rust": {"rustlang"},
    "react": {"reactjs", "react.js", "react js"},
    "angular": {"angularjs", "angular.js", "angular js"},
    "vue": {"vuejs", "vue.js", "vue js"},
    "next.js": {"nextjs", "next"},
    "node.js": {"nodejs", "node", "node js"},
    "express": {"expressjs", "express.js"},
    "django": {"django rest framework", "drf"},
    "flask": {"flask api"},
    "spring boot": {"springboot", "spring-boot", "spring"},
    "fastapi": {"fast api"},
    ".net": {"dotnet", "dot net", "asp.net"},
    "kubernetes": {"k8s", "k8"},
    "docker": {"containerization", "containers"},
    "terraform": {"tf", "iac", "infrastructure as code"},
    "ansible": {"configuration management"},
    "jenkins": {"ci server"},
    "postgresql": {"postgres", "psql", "pgsql"},
    "mongodb": {"mongo", "mongo db"},
    "mysql": {"my sql"},
    "redis": {"redis cache"},
    "elasticsearch": {"elastic", "elk", "elastic search"},
    "apache kafka": {"kafka"},
    "rabbitmq": {"rabbit mq", "amqp"},
    "amazon web services": {"aws"},
    "microsoft azure": {"azure"},
    "google cloud platform": {"gcp", "google cloud"},
    "ci/cd": {"cicd", "ci cd", "continuous integration", "continuous deployment"},
    "rest api": {"rest", "restful", "restful api", "rest apis"},
    "graphql": {"graph ql"},
    "machine learning": {"ml"},
    "deep learning": {"dl"},
    "natural language processing": {"nlp"},
    "computer vision": {"cv"},
    "artificial intelligence": {"ai"},
    "data science": {"data analytics"},
    "microservices": {"micro services", "micro-services"},
    "devops": {"dev ops", "dev-ops"},
    "agile": {"scrum", "kanban"},
    "html": {"html5"},
    "css": {"css3", "scss", "sass", "less"},
    "linux": {"unix", "ubuntu", "centos", "rhel"},
    "git": {"version control"},
    "sql": {"structured query language"},
    "nosql": {"no sql", "non-relational"},
    "power bi": {"powerbi"},
    "tableau": {"data visualization"},
    "apache spark": {"spark", "pyspark"},
    "hadoop": {"hdfs", "mapreduce"},
    "snowflake": {"snowflake db"},
    "nestjs": {"nest", "nest.js", "nest js"},
    "react native": {"reactnative", "react-native"},
    "maven": {"apache maven"},
    "gradle": {"gradle build"},
    "pytest": {"py.test"},
    "junit": {"junit5", "junit4"},
    "k3s": {"k3"},
    # Mobile
    "kotlin": {"kt"},
    "swift": {"swiftui"},
    "flutter": {"dart"},
    # ML/AI
    "tensorflow": {"keras"},
    "pytorch": {"torch"},
    "scikit-learn": {"sklearn"},
    # Testing
    "selenium": {"webdriver"},
    "cypress": {"cypress.io"},
    "playwright": {"pw"},
    # Data
    "airflow": {"apache airflow"},
    "dbt": {"data build tool"},
    # Frontend
    "svelte": {"sveltekit"},
    "nuxt": {"nuxtjs", "nuxt.js"},
    "remix": {"remix.run"},
    # BaaS/Cloud
    "supabase": {"supa"},
    "firebase": {"firestore"},
    # Observability
    "datadog": {"dd"},
    "grafana": {"grafana cloud"},
    "prometheus": {"prom"},
    "splunk": {"splunk cloud"},
    # AWS Messaging
    "sqs": {"amazon sqs", "aws sqs"},
    "sns": {"amazon sns", "aws sns"},
    # Design
    "figma": {"figma design"},
}

# Reverse lookup built once at import time
_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in SKILL_ALIASES.items():
    _ALIAS_LOOKUP[_canonical] = _canonical
    for _alias in _aliases:
        _ALIAS_LOOKUP[_alias] = _canonical


def normalize_skill(skill: str) -> str:
    """Normalize a skill string to its canonical form via alias lookup."""
    s = skill.lower().strip()
    return _ALIAS_LOOKUP.get(s, s)


def parse_skills(raw) -> set:
    """Normalize skills from any format (string, list, set) to a canonical lowercase set.

    Handles comma-separated strings, lists, tuples, and sets.
    All skills are normalized through SKILL_ALIASES (e.g., "JS" → "javascript").
    """
    if isinstance(raw, set):
        items = {s for s in raw if s}
    elif isinstance(raw, str):
        items = {s.strip() for s in raw.split(",") if s.strip()}
    elif isinstance(raw, (list, tuple)):
        items = {s.strip() for s in raw if isinstance(s, str) and s.strip()}
    else:
        return set()
    return {normalize_skill(s) for s in items}


# ── Bonus Scoring Helpers ────────────────────────────────────────────────────

def _score_location(job_location: Optional[str], profile_location: Optional[str]) -> int:
    """Score location match. Returns 0 or 5 bonus points."""
    if not job_location or not profile_location:
        return 0
    jl = job_location.lower().strip()
    pl = profile_location.lower().strip()
    # Exact city match (substring to handle "Bangalore/Bengaluru" vs "Bangalore")
    if pl in jl or jl in pl:
        return 5
    # Remote is universally acceptable
    if any(w in jl for w in ("remote", "wfh", "work from home", "anywhere")):
        return 5
    return 0


def _score_work_mode(job_work_mode: Optional[str]) -> int:
    """Score work mode. Remote/WFH gets a bonus. Returns 0-5 bonus points."""
    if not job_work_mode:
        return 0
    wm = job_work_mode.lower().strip()
    if wm in ("wfh", "remote", "work from home"):
        return 5
    if wm == "hybrid":
        return 3
    return 0  # Office — no penalty, just no bonus


def _score_salary(job_salary: Optional[str], profile_expected_ctc) -> int:
    """Score salary fit. Returns 0-5 bonus points.

    Only scores when both job salary and profile expected CTC are available.
    Accepts profile_expected_ctc as float or string (e.g., "15.0 Lacs").
    """
    if not job_salary or profile_expected_ctc is None:
        return 0
    if "not disclosed" in job_salary.lower():
        return 0
    # Parse profile CTC to float if string
    if isinstance(profile_expected_ctc, str):
        ctc_nums = re.findall(r'(\d+(?:\.\d+)?)', profile_expected_ctc)
        if not ctc_nums:
            return 0
        profile_expected_ctc = float(ctc_nums[0])
    elif not isinstance(profile_expected_ctc, (int, float)):
        return 0
    nums = re.findall(r'(\d+(?:\.\d+)?)', job_salary)
    if len(nums) < 2:
        return 0
    try:
        job_max = float(nums[-1])
        if job_max > 200:  # Likely wrong unit — can't compare
            return 0
        if job_max >= profile_expected_ctc:
            return 5  # Meets or exceeds expectation
        elif job_max >= profile_expected_ctc * 0.8:
            return 3  # Within 20%
        return 0  # Below expectation
    except (ValueError, IndexError):
        return 0


# ── Main Scoring Function ───────────────────────────────────────────────────

def compute_fit_score(
    job_skills: set,
    profile_skills: set,
    job_exp_str: str,
    profile_exp,
    # Optional enrichment (backward compatible — all default to None)
    job_location: Optional[str] = None,
    profile_location: Optional[str] = None,
    job_work_mode: Optional[str] = None,
    job_salary: Optional[str] = None,
    profile_expected_ctc=None,
    # Numeric experience fields (avoid regex round-trip when available)
    experience_min: Optional[int] = None,
    experience_max: Optional[int] = None,
) -> dict:
    """Compute fit score between a job and a candidate profile.

    Base score: 60% skills + 40% experience (unchanged from v1).
    Additive bonuses: +5 location match, +5 remote/WFH, +5 salary fit (max +15).
    Overall score capped at 100.

    Args:
        job_skills: Set of normalized job skill strings
        profile_skills: Set of normalized profile skill strings
        job_exp_str: Experience string like "3-5 years"
        profile_exp: Profile experience (string like "5 years 0 months" or numeric)
        job_location: Job city/location string (optional)
        profile_location: User's current location (optional)
        job_work_mode: "WFH", "Hybrid", "Office", etc. (optional)
        job_salary: Salary string like "15-20 LPA" (optional)
        profile_expected_ctc: Expected CTC in LPA (optional)

    Returns:
        {overall_score, skill_match, experience_match, bonuses (if data provided),
         recommendation}
    """
    # ── Skill match (unchanged) ──
    matched_skills = job_skills & profile_skills
    missing_skills = job_skills - profile_skills
    skill_score = (len(matched_skills) / len(job_skills) * 100) if job_skills else 50

    # ── Experience match ──
    exp_score = 50  # Default if can't determine
    if profile_exp is not None and (job_exp_str or experience_min is not None):
        p_exp_match = re.findall(r'(\d+)', str(profile_exp))
        p_exp = float(p_exp_match[0]) if p_exp_match else 0

        # Prefer numeric fields (avoid regex round-trip), fall back to regex
        if experience_min is not None and experience_max is not None:
            min_exp, max_exp = float(experience_min), float(experience_max)
        elif job_exp_str:
            exp_nums = re.findall(r'(\d+)', str(job_exp_str))
            if len(exp_nums) >= 2:
                min_exp, max_exp = float(exp_nums[0]), float(exp_nums[1])
            else:
                min_exp = max_exp = None
        else:
            min_exp = max_exp = None

        if min_exp is not None and max_exp is not None:
            if min_exp <= p_exp <= max_exp:
                exp_score = 100
            elif p_exp < min_exp:
                exp_score = max(0, 100 - (min_exp - p_exp) * 20)
            else:
                exp_score = max(50, 100 - (p_exp - max_exp) * 10)

    # ── Base score (unchanged formula) ──
    base_score = skill_score * 0.6 + exp_score * 0.4

    # ── Additive bonuses (new) ──
    loc_bonus = _score_location(job_location, profile_location)
    wm_bonus = _score_work_mode(job_work_mode)
    sal_bonus = _score_salary(job_salary, profile_expected_ctc)
    total_bonus = loc_bonus + wm_bonus + sal_bonus

    overall_score = min(100, round(base_score + total_bonus))

    # ── Recommendation ──
    if overall_score >= 80:
        recommendation = "Strong match — apply confidently"
    elif overall_score >= 60:
        recommendation = "Good match — worth applying"
    elif overall_score >= 40:
        recommendation = "Partial match — review missing skills before applying"
    else:
        recommendation = "Weak match — consider upskilling first"

    result = {
        "overall_score": overall_score,
        "skill_match": {
            "score": round(skill_score),
            "matched": sorted(matched_skills),
            "missing": sorted(missing_skills),
        },
        "experience_match": {
            "score": round(exp_score),
            "your_experience": profile_exp,
            "required": job_exp_str,
        },
        "recommendation": recommendation,
    }

    # Include bonus breakdown when any enrichment data was provided
    if job_location is not None or job_work_mode is not None or job_salary is not None:
        result["bonuses"] = {
            "location": loc_bonus,
            "work_mode": wm_bonus,
            "salary": sal_bonus,
            "total": total_bonus,
        }

    return result
