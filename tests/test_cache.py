"""Tests for naukri_server.cache — answer caching with backup recovery."""

import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from naukri_server.cache import (
    _load_cache, _save_cache, _cache_key,
    delete_cached_answer, update_cached_answer,
)


class TestCacheKey:
    def test_deterministic(self):
        key1 = _cache_key("notice_period", {"type": "select", "options": ["1m", "2m"]})
        key2 = _cache_key("notice_period", {"type": "select", "options": ["1m", "2m"]})
        assert key1 == key2

    def test_different_questions(self):
        key1 = _cache_key("notice_period", {})
        key2 = _cache_key("expected_ctc", {})
        assert key1 != key2


class TestCacheLoadSave:
    def test_round_trip(self, tmp_path):
        test_file = tmp_path / "test_cache.json"
        cache = {"q1": {"answer": "yes", "cached_at": time.time()}}
        with patch("naukri_server.cache.CACHE_FILE", test_file):
            _save_cache(cache)
            loaded = _load_cache()
            assert "q1" in loaded
            assert loaded["q1"]["answer"] == "yes"

    def test_empty_file(self, tmp_path):
        test_file = tmp_path / "nonexistent.json"
        with patch("naukri_server.cache.CACHE_FILE", test_file):
            assert _load_cache() == {}

    def test_backup_recovery(self, tmp_path):
        test_file = tmp_path / "test_cache.json"
        backup_file = tmp_path / "test_cache.backup"

        # Write corrupt primary
        test_file.write_text("not valid json", encoding="utf-8")
        # Write valid backup
        backup_file.write_text('{"q1": {"answer": "backup"}}', encoding="utf-8")

        with patch("naukri_server.cache.CACHE_FILE", test_file):
            loaded = _load_cache()
            assert "q1" in loaded
            assert loaded["q1"]["answer"] == "backup"


class TestDeleteCachedAnswer:
    @pytest.mark.asyncio
    async def test_delete_existing(self, tmp_path):
        test_file = tmp_path / "test_cache.json"
        cache = {"q1": {"answer": "yes", "cached_at": time.time()}}
        test_file.write_text(json.dumps(cache), encoding="utf-8")
        with patch("naukri_server.cache.CACHE_FILE", test_file):
            result = await delete_cached_answer("q1")
            assert result is True
            loaded = _load_cache()
            assert "q1" not in loaded

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, tmp_path):
        test_file = tmp_path / "test_cache.json"
        test_file.write_text("{}", encoding="utf-8")
        with patch("naukri_server.cache.CACHE_FILE", test_file):
            result = await delete_cached_answer("nonexistent")
            assert result is False


class TestUpdateCachedAnswer:
    @pytest.mark.asyncio
    async def test_update_existing(self, tmp_path):
        test_file = tmp_path / "test_cache.json"
        cache = {"q1": {"answer": "old", "cached_at": time.time()}}
        test_file.write_text(json.dumps(cache), encoding="utf-8")
        with patch("naukri_server.cache.CACHE_FILE", test_file):
            result = await update_cached_answer("q1", "new")
            assert result is True
            loaded = _load_cache()
            assert loaded["q1"]["answer"] == "new"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, tmp_path):
        test_file = tmp_path / "test_cache.json"
        test_file.write_text("{}", encoding="utf-8")
        with patch("naukri_server.cache.CACHE_FILE", test_file):
            result = await update_cached_answer("nonexistent", "value")
            assert result is False
