"""Tests for naukri_server.utils — slug derivation and atomic JSON I/O."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open


from naukri_server.utils import derive_slug, load_json_with_backup, save_json_atomic


# ---------------------------------------------------------------------------
# derive_slug
# ---------------------------------------------------------------------------

class TestDeriveSlug:
    def test_strips_pvt_ltd(self):
        assert derive_slug("Infosys Pvt. Ltd.") == "infosys"

    def test_strips_private_limited(self):
        assert derive_slug("Wipro Private Limited") == "wipro"

    def test_strips_india_suffix(self):
        assert derive_slug("Google India") == "google"

    def test_multiple_word_company(self):
        assert derive_slug("Tata Consultancy Services") == "tata-consultancy"

    def test_unicode_stripped(self):
        # Non-ASCII chars replaced by the regex, result trimmed
        assert derive_slug("Caf\u00e9 Corp.") == "caf"

    def test_empty_string(self):
        assert derive_slug("") == ""

    def test_already_slugified(self):
        assert derive_slug("my-company") == "my-company"

    def test_special_characters(self):
        assert derive_slug("Foo & Bar @#$ Solutions") == "foo-bar"

    def test_only_one_suffix_stripped(self):
        # "Technologies" is stripped first (matched), "India" remains inside
        # because break fires after the first match.  Verify single-strip.
        assert derive_slug("Acme India Technologies") == "acme-india"

    def test_leading_trailing_whitespace(self):
        assert derive_slug("  Zoom Inc.  ") == "zoom"


# ---------------------------------------------------------------------------
# load_json_with_backup
# ---------------------------------------------------------------------------

class TestLoadJsonWithBackup:
    def test_valid_primary(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('[{"id": 1}]', encoding="utf-8")
        logger = MagicMock()
        result = load_json_with_backup(p, logger)
        assert result == [{"id": 1}]
        logger.warning.assert_not_called()
        logger.error.assert_not_called()

    def test_corrupt_primary_valid_backup(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text("NOT JSON", encoding="utf-8")
        backup = tmp_path / "data.backup"
        backup.write_text('[{"id": 2}]', encoding="utf-8")
        logger = MagicMock()
        result = load_json_with_backup(p, logger)
        assert result == [{"id": 2}]
        logger.warning.assert_called_once()

    def test_both_corrupted(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text("BAD", encoding="utf-8")
        backup = tmp_path / "data.backup"
        backup.write_text("ALSO BAD", encoding="utf-8")
        logger = MagicMock()
        result = load_json_with_backup(p, logger)
        assert result == []
        logger.error.assert_called_once()

    def test_missing_file(self, tmp_path):
        p = tmp_path / "nonexistent.json"
        logger = MagicMock()
        result = load_json_with_backup(p, logger)
        assert result == []

    def test_empty_file(self, tmp_path):
        """An empty file is invalid JSON and has no backup -> []."""
        p = tmp_path / "data.json"
        p.write_text("", encoding="utf-8")
        logger = MagicMock()
        result = load_json_with_backup(p, logger)
        assert result == []
        logger.error.assert_called_once()


# ---------------------------------------------------------------------------
# save_json_atomic
# ---------------------------------------------------------------------------

class TestSaveJsonAtomic:
    def test_creates_file_and_writes(self, tmp_path):
        p = tmp_path / "data.json"
        logger = MagicMock()
        save_json_atomic(p, [1, 2, 3], logger)
        assert json.loads(p.read_text(encoding="utf-8")) == [1, 2, 3]

    def test_creates_backup_on_overwrite(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('[0]', encoding="utf-8")
        logger = MagicMock()
        save_json_atomic(p, [1], logger)
        backup = tmp_path / "data.backup"
        assert backup.exists()
        assert json.loads(backup.read_text(encoding="utf-8")) == [0]
        assert json.loads(p.read_text(encoding="utf-8")) == [1]

    def test_no_backup_for_new_file(self, tmp_path):
        p = tmp_path / "fresh.json"
        logger = MagicMock()
        save_json_atomic(p, {"k": "v"}, logger)
        backup = tmp_path / "fresh.backup"
        assert not backup.exists()

    def test_tmp_cleaned_up(self, tmp_path):
        """After save, no .tmp file should remain (os.replace removes it)."""
        p = tmp_path / "data.json"
        logger = MagicMock()
        save_json_atomic(p, [42], logger)
        tmp = tmp_path / "data.tmp"
        assert not tmp.exists()
