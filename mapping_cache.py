"""Local cache of Claude field->value mappings, keyed by ATS domain + field-set hash.

Avoids re-calling Claude when the same domain presents the same set of
fields (label + type + options) again.
"""

import hashlib
import json
import os

CACHE_PATH = os.path.join(os.path.dirname(__file__), "mapping_cache.json")


def _load() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def hash_fields(fields) -> str:
    canonical = [{"label": f.label, "type": f.type, "options": f.options} for f in fields]
    blob = json.dumps(canonical, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(domain: str, field_hash: str):
    return _load().get(domain, {}).get(field_hash)


def store(domain: str, field_hash: str, mappings) -> None:
    data = _load()
    data.setdefault(domain, {})[field_hash] = mappings
    _save(data)
