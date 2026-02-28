"""
Answer cache — persists across sessions.
"""

import asyncio
import json

from naukri_server.config import CACHE_FILE


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _cache_key(question_name: str, answer_option: dict) -> str:
    return f"{question_name}_{json.dumps(answer_option, sort_keys=True)}"


_cache_lock = asyncio.Lock()
