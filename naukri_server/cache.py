"""
Answer cache — persists across sessions.
"""

import asyncio
import json
import os

from naukri_server.config import CACHE_FILE


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    text = json.dumps(cache, indent=2, ensure_ascii=False)
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
