"""Skill taxonomy -- canonical skill names with alias normalization."""


# ── Skill Alias Map ──────────────────────────────────────────────────────────
# Canonical skill name -> set of known aliases.
# SkillTaxonomy normalizes all inputs through this map.

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


class SkillTaxonomy:
    """Domain object for skill normalization.

    88 canonical skills, 150+ aliases, case-insensitive normalization.
    """

    def __init__(self, aliases: dict[str, set[str]]):
        self._aliases = aliases
        self._lookup: dict[str, str] = {}
        for canonical, alias_set in aliases.items():
            self._lookup[canonical] = canonical
            for alias in alias_set:
                self._lookup[alias] = canonical

    def normalize(self, skill: str) -> str:
        """Normalize a skill string to its canonical form."""
        return self._lookup.get(skill.lower().strip(), skill.lower().strip())

    def parse_set(self, raw) -> frozenset[str]:
        """Parse skills from any format (set/str/list/tuple) to normalized frozenset."""
        if isinstance(raw, set):
            items = {s for s in raw if s}
        elif isinstance(raw, str):
            items = {s.strip() for s in raw.split(",") if s.strip()}
        elif isinstance(raw, (list, tuple)):
            items = {s.strip() for s in raw if isinstance(s, str) and s.strip()}
        else:
            return frozenset()
        return frozenset(self.normalize(s) for s in items)

    def match(self, job_skills: frozenset[str], profile_skills: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
        """Return (matched, missing) skills."""
        matched = job_skills & profile_skills
        missing = job_skills - profile_skills
        return matched, missing

    @property
    def canonical_count(self) -> int:
        return len(self._aliases)

    @property
    def alias_count(self) -> int:
        return sum(len(v) for v in self._aliases.values())


# Module-level singleton
DEFAULT_TAXONOMY = SkillTaxonomy(SKILL_ALIASES)
