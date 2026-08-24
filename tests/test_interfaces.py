"""Unit tests for naukri_server.interfaces — abstraction layer over api.py / browser.py.

Verifies that:
  - NaukriApiClient.get delegates to api_get with correct args
  - NaukriApiClient.post delegates to api_post with correct args
  - NaukriApiClient.put delegates to api_put with correct args
  - NaukriApiClient.delete delegates to api_delete with correct args
  - ApiClient cannot be instantiated directly (abstract)
  - BrowserProvider cannot be instantiated directly (abstract)
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.interfaces import NaukriApiClient, ApiClient, BrowserProvider


# ===========================================================================
# 1. NaukriApiClient.get delegates to api_get
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.api.api_get", new_callable=AsyncMock)
async def test_api_client_get_delegates_to_api_get(mock_api_get):
    mock_api_get.return_value = {"jobs": []}
    client = NaukriApiClient()

    result = await client.get("/some/path", params={"page": "1"}, extra_headers={"X-Custom": "yes"})

    mock_api_get.assert_awaited_once_with("/some/path", params={"page": "1"}, extra_headers={"X-Custom": "yes"})
    assert result == {"jobs": []}


@pytest.mark.asyncio
@patch("naukri_server.api.api_get", new_callable=AsyncMock)
async def test_api_client_get_defaults_none_params(mock_api_get):
    mock_api_get.return_value = {"ok": True}
    client = NaukriApiClient()

    result = await client.get("/path")

    mock_api_get.assert_awaited_once_with("/path", params=None, extra_headers=None)
    assert result == {"ok": True}


# ===========================================================================
# 2. NaukriApiClient.post delegates to api_post
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.api.api_post", new_callable=AsyncMock)
async def test_api_client_post_delegates_to_api_post(mock_api_post):
    mock_api_post.return_value = {"status": 200}
    client = NaukriApiClient()

    result = await client.post("/apply", body={"job_id": "123"})

    mock_api_post.assert_awaited_once_with("/apply", body={"job_id": "123"}, extra_headers=None)
    assert result == {"status": 200}


# ===========================================================================
# 3. NaukriApiClient.put delegates to api_put
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.api.api_put", new_callable=AsyncMock)
async def test_api_client_put_delegates_to_api_put(mock_api_put):
    mock_api_put.return_value = {"updated": True}
    client = NaukriApiClient()

    result = await client.put("/profile", body={"name": "Test"})

    mock_api_put.assert_awaited_once_with("/profile", body={"name": "Test"}, extra_headers=None)
    assert result == {"updated": True}


# ===========================================================================
# 4. NaukriApiClient.delete delegates to api_delete
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.api.api_delete", new_callable=AsyncMock)
async def test_api_client_delete_delegates_to_api_delete(mock_api_delete):
    mock_api_delete.return_value = {}
    client = NaukriApiClient()

    result = await client.delete("/item/456", body={"reason": "test"})

    mock_api_delete.assert_awaited_once_with("/item/456", body={"reason": "test"}, extra_headers=None)
    assert result == {}


@pytest.mark.asyncio
@patch("naukri_server.api.api_delete", new_callable=AsyncMock)
async def test_api_client_delete_defaults_none_body(mock_api_delete):
    mock_api_delete.return_value = {}
    client = NaukriApiClient()

    result = await client.delete("/item/789")

    mock_api_delete.assert_awaited_once_with("/item/789", body=None, extra_headers=None)
    assert result == {}


# ===========================================================================
# 5. Abstract classes cannot be instantiated directly
# ===========================================================================

def test_api_client_abstract_cannot_instantiate():
    with pytest.raises(TypeError):
        ApiClient()


def test_browser_provider_abstract_cannot_instantiate():
    with pytest.raises(TypeError):
        BrowserProvider()
