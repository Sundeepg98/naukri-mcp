"""Shared utility functions."""

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path


def derive_slug(company_name: str) -> str:
    """Derive an AmbitionBox-style URL slug from a company name.

    Strips common suffixes (Pvt. Ltd., Inc., etc.) and normalizes to lowercase
    hyphenated format.
    """
    name = company_name.strip()
    for suffix in ("Pvt. Ltd.", "Pvt Ltd", "Private Limited", "Ltd.", "Ltd",
                   "Limited", "Inc.", "Inc", "Corp.", "Corp", "Corporation",
                   "LLP", "LLC", "Technologies", "Technology", "Solutions",
                   "Services", "India"):
        if name.lower().endswith(suffix.lower()):
            name = name[:len(name) - len(suffix)].strip()
            break  # Only strip one suffix (fixes research.py bug)
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    slug = re.sub(r'-+', '-', slug)
    return slug


def load_json_with_backup(path: Path, logger) -> list:
    """Load JSON file with .backup fallback recovery."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            backup = path.with_suffix(".backup")
            if backup.exists():
                try:
                    logger.warning("Primary %s corrupted, recovering from backup", path.name)
                    return json.loads(backup.read_text(encoding="utf-8"))
                except Exception:
                    pass
            logger.error("Both primary and backup corrupted for %s", path.name)
            return []
    return []


def save_json_atomic(path: Path, data, logger):
    """Atomic JSON write with .backup creation."""
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if path.exists():
        backup = path.with_suffix(".backup")
        shutil.copy2(str(path), str(backup))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


class TtlCache:
    """Simple async TTL cache for a single value.

    Usage:
        cache = TtlCache(ttl=30)
        data = await cache.get(fetch_fn)  # calls fetch_fn() on cache miss
        cache.invalidate()                # force next call to refetch
    """
    def __init__(self, ttl: float):
        self._ttl = ttl
        self._data = None
        self._ts = 0.0
        self._lock = asyncio.Lock()

    async def get(self, fetch_fn):
        """Return cached data or call fetch_fn() if stale."""
        now = time.time()
        if self._data is not None and (now - self._ts) < self._ttl:
            return self._data
        async with self._lock:
            now = time.time()
            if self._data is not None and (now - self._ts) < self._ttl:
                return self._data
            self._data = await fetch_fn()
            self._ts = time.time()
            return self._data

    def invalidate(self):
        """Clear cached data."""
        self._data = None
        self._ts = 0.0
