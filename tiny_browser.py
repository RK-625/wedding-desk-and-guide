"""TinyBrowser REST lifecycle. Keep the CDP capability URL in memory only."""
import os
import re
from urllib.parse import urlsplit

import requests


API = "https://api.browser.tinyfish.ai"


def _headers():
    key = os.environ.get("TINYFISH_API_KEY", "").strip()
    if not key:
        raise ValueError("TINYFISH_API_KEY is missing")
    return {"X-API-Key": key}


def close(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", session_id):
        raise ValueError("Invalid browser session ID")
    response = requests.delete(f"{API}/{session_id}", headers=_headers(),
                               timeout=(5, 15), allow_redirects=False)
    if response.status_code not in (200, 204, 404, 410):
        response.raise_for_status()
        raise ValueError("Unexpected browser close response")


def create():
    response = requests.post(API, headers=_headers(), json={"timeout_seconds": 180},
                             timeout=(5, 65), allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 201:
        raise ValueError("Unexpected browser creation response")
    data = response.json()
    session_id = data["session_id"]
    endpoint = data.get("cdp_url")
    if not isinstance(endpoint, str) or urlsplit(endpoint).scheme != "wss" or not urlsplit(endpoint).hostname:
        close(session_id)
        raise ValueError("Missing secure CDP endpoint")
    return session_id, endpoint
