"""Small disk cache for API responses.

Keeps repeated / related queries cheap: re-running the same study, or a
follow-up that only changes one parameter (add a language, extend the
range), only pays the network cost for what actually changed.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional


class DiskCache:
    """JSON-on-disk cache keyed by an arbitrary string.

    Entries are content-addressed (sha256 of the key), so no index file is
    needed and concurrent processes cannot corrupt each other's entries.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
        return self.root / f"{digest}.json"

    def get(self, key: str, max_age_seconds: Optional[float] = None) -> Any:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if max_age_seconds is not None:
            age = time.time() - payload.get("_cached_at", 0)
            if age > max_age_seconds:
                return None
        return payload.get("data")

    def set(self, key: str, data: Any) -> None:
        path = self._path(key)
        payload = {"_cached_at": time.time(), "_key": key, "data": data}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def get_or_fetch(self, key: str, fetch_fn, max_age_seconds: Optional[float] = None):
        cached = self.get(key, max_age_seconds=max_age_seconds)
        if cached is not None:
            return cached, True  # (data, was_cache_hit)
        data = fetch_fn()
        self.set(key, data)
        return data, False
