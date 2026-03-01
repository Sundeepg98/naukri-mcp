"""
Answer cache — persists across sessions.
"""

import asyncio
import json
import os
import shutil
import time

from naukri_server.config import CACHE_FILE


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    # Add timestamp to entries missing one
    for key, entry in cache.items():
        if isinstance(entry, dict) and "cached_at" not in entry:
            entry["cached_at"] = time.time()
    # Purge entries older than 30 days
    cutoff = time.time() - (30 * 86400)
    cache = {k: v for k, v in cache.items() if not isinstance(v, dict) or v.get("cached_at", time.time()) > cutoff}
    # Atomic write with backup
    text = json.dumps(cache, indent=2, ensure_ascii=False)
    if CACHE_FILE.exists():
        backup = CACHE_FILE.with_suffix(".backup")
        shutil.copy2(str(CACHE_FILE), str(backup))
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(CACHE_FILE))


def _cache_key(question_name: str, answer_option: dict) -> str:
    try:
        opts = json.dumps(answer_option, sort_keys=True)
    except (TypeError, ValueError):
        opts = str(sorted(answer_option.items()) if answer_option else "")
    return f"{question_name}_{opts}"


_cache_lock = asyncio.Lock()
