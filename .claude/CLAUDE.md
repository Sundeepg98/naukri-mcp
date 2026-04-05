# Naukri MCP Server — Developer Guide

## Architecture
- **26 tools** across 24 tool modules using FastMCP (`@mcp.tool()` decorators)
- **Playwright** persistent Chrome profile for browser automation
- **aiohttp** global session for REST API calls
- **PagePool** (3 tabs) for concurrent browser operations

## Tool Patterns

### Action-Parameter Pattern
Most consolidated tools use `action` as first param to dispatch to private helpers:
```python
@mcp.tool()
async def naukri_tool(action: str = "list", ...) -> dict:
    if action == "list":
        return await _list_items(...)
    elif action == "detail":
        return await _get_detail(...)
    else:
        return {"status": "error", "message": f"Unknown action: {action}", "error_code": "VALIDATION_ERROR"}
```

### @api_tool Decorator
Simple REST tools use `@api_tool("context")` from `api.py` for standardized error handling:
```python
@mcp.tool()
@api_tool("Get subscription status")
async def naukri_get_subscription_status() -> dict:
    data = await api_get(SUBSCRIPTION_API)
    return {"status": "success", ...}
```

### Browser vs REST
- **REST-first** for reads (api_get/api_post with auth token)
- **Browser** for mutations (Akamai blocks programmatic PUT/DELETE)
- **Browser intercept** for pages that load data via XHR (`page_intercept_json`)
- Use `async with browser.page_pool.acquire() as page:` to get a browser tab

## Testing Conventions
- All tests are **pure** — no network, no browser, no file I/O
- Use `unittest.mock.patch` with `AsyncMock` for async helpers
- Patch at the **source module** (where the function is defined), not where it's imported
- Test files: `tests/test_<module_group>.py`
- Run: `python -m pytest tests/ -q`

## Validation Helpers (`naukri_server/validation.py`)
- `validate_limit(value, max_allowed=50)` — clamps to [1, max_allowed], use for all limit params
- `validate_page(value)` — clamps to [1, ∞), use for all page params
- `validate_enum(value, allowed, field_name)` — case-insensitive enum check, returns matched value or None
- All three silently clamp invalid values (AI-ergonomic: no VALIDATION_ERROR for off-by-one)

## How to Add a New Tool
1. Choose the right module file in `naukri_server/tools/`
2. Add `@mcp.tool()` decorator with complete docstring (Args + Returns)
3. Use `error_code` field in all error returns (VALIDATION_ERROR, API_ERROR, BROWSER_ERROR, NOT_FOUND, AUTH_ERROR)
4. Use `validate_limit()` / `validate_page()` from `naukri_server.validation` for param validation
5. Return `{total, count, page, has_more}` for list endpoints
6. Register in `naukri_server/__init__.py` if new module
7. Add tests in `tests/`

## Common Pitfalls
- **Akamai CDN** blocks PUT/DELETE on many endpoints — use browser automation for mutations
- **PagePool** has 3 tabs — don't hold tabs longer than necessary
- **Token refresh** uses asyncio.Lock — never nest page_pool.acquire inside refresh lock
- **Browser intercept** can miss responses — always set appropriate timeout
- **.backup files** — all JSON data files use atomic writes with backup recovery

## AmbitionBox REST Bridge
- `ambitionbox.py` has 7 REST helper functions: `ab_get_benefits`, `ab_get_work_culture`, `ab_get_interview_questions`, `ab_get_competitors`, `ab_get_locations`, `ab_get_applied_jobs_insights`, `ab_get_salary_rest`
- Uses cookies extracted from browser context (cached with 30min TTL)
- Auto-refreshes cookies on 401/403
- Config constants: `AB_SALARY_API`, `AB_BENEFITS_API`, `AB_REVIEW_DIST_API`, etc.
- Used by `research.py` for parallel data fetching alongside browser scraping

## Screening Question Handling
- First apply attempt may return `status: "needs_input"` with `questions` list
- Questions cached in `questions.json` for fuzzy matching
- Retry with answers: `naukri_apply(job_id, answers={"question_id": "answer"})`
- `apply.py` allows retry when existing record has `status == "needs_input"` AND answers provided
- `smart_apply._apply_top_fits` auto-retries needs_input jobs if general answers provided

## Fit Scoring (`scoring.py`)
- 88 canonical skills with 150 aliases for normalization
- Base score: 60% skill match + 40% experience match
- Bonuses (up to +15): location (+5), work_mode (+5), salary (+5), agent_eligible (+5)
- Over-qualification: sqrt penalty curve (floor 60, not linear cliff at 50)
- Returns `reasons` list explaining skill gaps, experience mismatch, missing bonuses
- Experience parsed with month precision: "5 years 6 months" = 5.5

## Data Persistence
- JSON files: `applications.json`, `saved_jobs.json`, `reminders.json`, `questions.json`
- Atomic writes via `save_json_atomic()` with `.backup` recovery
- Thread-safe via `asyncio.Lock` per file (`_applications_lock`, `_saved_jobs_lock`)
- Merge deduplicates by `job_id` before syncing remote data

## File Ownership for Parallel Development
Each parallel agent should ONLY modify its assigned files. Conflicts break merges.
Check the implementation plan for file assignments before starting.

## Abstraction Interfaces (`naukri_server/interfaces.py`)
- `ApiClient` and `BrowserProvider` abstract classes for new tools
- Concrete implementations (`NaukriApiClient`, `PlaywrightBrowserProvider`) wrap existing `api.py` / `browser.py`
- Existing tools still use direct imports (migration is optional and incremental)
- **New tools SHOULD use interfaces for testability** � depend on `api_client` / `browser_provider` singletons instead of importing `api_get` / `api_post` directly

## Key File Paths
- Tools: `naukri_server/tools/*.py` (100 MCP tools)
- Services: `naukri_server/services/*.py` (7 service modules)
- Domain: `naukri_server/domain/*.py` (9 domain objects)
- Database: `naukri_server/database.py` (SQLite, 7 tables, 24+ CRUD helpers)
- Infrastructure: `naukri_server/api.py`, `browser.py`, `config.py`, `utils.py`, `interfaces.py`
- Events: `naukri_server/events.py` (EventBus, 12 event types)
- Subscribers: `naukri_server/subscribers.py` (8 reactive handlers)
- Workflows: `naukri_server/workflows.py` (2 saga workflows)
- Tests: `tests/test_*.py`

## Domain Objects (`naukri_server/domain/`)

Rich domain objects encoding business rules. Use these instead of raw dicts for business logic.

| Object | File | Purpose |
|--------|------|---------|
| `SkillTaxonomy` | `domain/skill_taxonomy.py` | 88 canonical skills, 150 aliases, normalization |
| `Salary` | `domain/salary.py` | Parsing, unit detection, CTC comparison, market positioning |
| `FitScore` | `domain/fit_score.py` | Job-profile matching (SkillMatch + ExperienceScore + BonusScore) |
| `StalenessReport` | `domain/application.py` | 5-signal staleness scoring |
| `StatusTransition` | `domain/application.py` | Positive/negative transition classification |
| `CompletionReport` | `domain/profile_completeness.py` | Profile grading, gaps, tips |
| `ParsedSalary` | `domain/job.py` | Consolidated salary parsing with logging |
| `ApplicationDetail` | `domain/application_detail.py` | API response parsing factory |
| `ProfileParser` | `domain/profile.py` | Profile field extraction with safe_get |

### Anti-Corruption Layer
All external API data MUST be accessed through `safe_get()` from `naukri_server.domain` or domain factories. Never use raw `.get()` on API responses without logging.

## Events & Subscribers

The system is event-driven. State changes emit events, subscribers react with side effects.

### Emitting Events
```python
from naukri_server.events import event_bus, ApplicationSubmitted
await event_bus.emit(ApplicationSubmitted(job_id=job_id, company=company))
```

### Event Types (12 total)
ApplicationSubmitted, ApplicationStatusChanged, ApplicationStale, RecruiterEngaged,
ApplicationInterviewScheduled, SyncCompleted, ReminderDue, ReminderSet,
ProfileScoreChanged, SavedJobAdded, SavedJobRemoved, SavedJobExpiring

### Subscriber Safety Rules
- Wrap handler body in try/except (never crash the caller)
- Use lazy imports inside handlers
- EventBus has depth guard (max 3) preventing infinite loops
- Every event persisted to SQLite event_log table

## Sagas & Workflows

Multi-step operations use SagaExecutor for partial-success handling.

### When to Use Saga
- Multi-step operation where each step is independently valuable
- Need step-by-step timing and error tracking
- Partial success is acceptable (not all-or-nothing)

### Current Sagas (7)
| Saga | Steps | File |
|------|-------|------|
| apply_workflow | apply → reminder | apply_service.py |
| sync_applications | merge → purge → detect → persist → emit | sync.py |
| sync_saved_jobs | merge → persist → emit | sync.py |
| apply_top_fits | score → apply_batch | smart_apply.py |
| compare_jobs | fetch → score → aggregate | compare.py |
| interview_lifecycle | prep → reminder → notify | workflows.py |
| stale_follow_up | draft → notify | workflows.py |

### Adding a New Saga
```python
from naukri_server.sagas import SagaExecutor
saga = SagaExecutor("my_workflow")
saga.add_step("step1", async_fn_1)
saga.add_step("step2", async_fn_2)
result = await saga.run()
# result: {status, completed_steps, step_timings, errors}
```
