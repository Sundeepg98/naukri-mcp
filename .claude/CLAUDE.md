# Naukri MCP Server — Developer Guide

## Architecture
- **38 tools** across 27 modules using FastMCP (`@mcp.tool()` decorators)
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

## File Ownership for Parallel Development
Each parallel agent should ONLY modify its assigned files. Conflicts break merges.
Check the implementation plan for file assignments before starting.

## Key File Paths
- Tools: `naukri_server/tools/*.py`
- Infrastructure: `naukri_server/api.py`, `browser.py`, `config.py`, `utils.py`
- Tests: `tests/test_*.py`
- Data: `applications.json`, `saved_jobs.json`, `reminders.json`, `questions.json`
- Config: `naukri_server/config.py` (API endpoints, timeouts, headers)
