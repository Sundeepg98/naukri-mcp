# Test Strategy — Naukri MCP

## Convention
All tests are PURE: no network, no browser, no file I/O.
Use `unittest.mock.patch` with `AsyncMock` for async helpers.
Patch at the **source module** where the function is defined.

## File Naming
- `test_<module>_deep.py` — Comprehensive tests for one tool module
- `test_<feature>.py` — Feature-specific tests
- `test_lsp_contracts.py` — Behavioral contract verification
- `test_models.py`, `test_interfaces.py` — Domain/architecture tests

## Running Tests
```
pytest tests/ -q                    # All tests
pytest tests/ -k "deep"             # Deep module tests only
pytest tests/ -k "lsp or contract"  # Contract tests only
pytest tests/ -k "wiring"           # Architecture wiring tests
```

## Fixtures
Shared fixtures in `conftest.py`: mock_api_client, sample_job, sample_application, sample_profile.
