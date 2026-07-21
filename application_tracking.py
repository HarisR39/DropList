"""Local tracking store for job applications handled by the autofill flow.

A simple JSON file keyed by job ID. There's no real database in this
project yet, so this is the minimal stand-in for one.
"""

import json
import os

TRACKING_PATH = os.path.join(os.path.dirname(__file__), "applications_tracking.json")


def _load() -> dict:
    if not os.path.exists(TRACKING_PATH):
        return {}
    with open(TRACKING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(TRACKING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def record(job_id: str, status: str, url: str = "", needs_review=None, errors=None) -> None:
    data = _load()
    data[job_id] = {
        "status": status,
        "url": url,
        "needs_review": needs_review or [],
        "errors": errors or [],
    }
    _save(data)


def get_status(job_id: str):
    return _load().get(job_id)
