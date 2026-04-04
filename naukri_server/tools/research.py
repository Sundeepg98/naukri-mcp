"""Unified company research — thin dispatcher over CompanyService."""

from naukri_server.services.company_service import (
    research_company as _research_company,
    salary_benchmark as _salary_benchmark,
)

# Backward-compat aliases (were @mcp.tool, now dispatched via naukri_company / naukri_insights)
naukri_research_company = _research_company
naukri_salary_benchmark = _salary_benchmark
