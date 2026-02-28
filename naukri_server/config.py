"""
Configuration constants and logging setup for Naukri MCP server.
"""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("naukri")

# Paths — go up from naukri_server/ to naukri/ where chrome-profile/ and questions.json live
CHROME_PROFILE = str(Path(__file__).parent.parent / "chrome-profile")
CACHE_FILE = Path(__file__).parent.parent / "questions.json"
NAUKRI_BASE = "https://www.naukri.com"

# Timeouts (ms for Playwright, seconds for aiohttp)
NAV_TIMEOUT = 20_000
ELEMENT_TIMEOUT = 5_000
API_TIMEOUT = 30  # seconds

# API headers (from Naukri-Automation reverse engineering)
API_HEADERS = {
    "accept": "application/json",
    "appid": "121",
    "clientid": "d3skt0p",
    "content-type": "application/json",
    "systemid": "Naukri",
    "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

# Apply request trailer fields (required by Naukri's apply endpoint)
APPLY_TRAILER = {
    "flowtype": "show",
    "crossdomain": True,
    "jquery": 1,
    "rdxMsgId": "",
    "chatBotSDK": True,
    "applyTypeId": "107",
    "closebtn": "y",
    "applySrc": "drecomm_profile",
}
