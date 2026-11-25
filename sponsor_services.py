"""Optional Flagsmith controls and Sarvam transcription for the Telegram bots."""
import logging
import os
import time
from functools import lru_cache

import requests


@lru_cache(maxsize=1)
def _environment_flags(key, refresh_window):
    try:
        response = requests.get(
            "https://api.flagsmith.com/api/v1/flags/",
            headers={"X-Environment-Key": key}, timeout=4, allow_redirects=False,
        )
        response.raise_for_status()
        return {entry["feature"]["name"]: entry["enabled"] for entry in response.json()
                if type(entry["enabled"]) is bool}
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logging.warning("Flagsmith flags unavailable (%s)", type(exc).__name__)
        return {}


def flag_enabled(name, default):
    key = os.getenv("FLAGSMITH_SERVER_KEY", "").strip()
    if not key:
        return default
    return _environment_flags(key, int(time.monotonic()) // 10).get(name, default)


def transcribe(audio, key):
    response = requests.post(
        "https://api.sarvam.ai/speech-to-text",
        headers={"api-subscription-key": key},
        data={"model": "saaras:v4", "mode": "translate"},
        files={"file": ("telegram.ogg", audio, "audio/ogg")},
        timeout=(5, 25), allow_redirects=False,
    )
    response.raise_for_status()
    transcript = response.json()["transcript"]
    if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 4000:
        raise ValueError("Invalid audio transcript")
    return transcript.strip()
