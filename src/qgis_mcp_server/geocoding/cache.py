"""The local resolution cache (task 4.1).

Two jobs, and the second is why it exists in the test suite as well as at
runtime:

1. **It keeps Nominatim usage inside policy.** Nominatim is free and asks for no
   more than one request per second and no bulk querying. A cache turns a
   repeated place lookup into no request at all.

2. **It makes the tests deterministic without mocking the geocoder.** Recorded
   real responses are committed as fixtures, so a test exercises the real
   parsing code against the real response shape -- which a hand-written mock
   would not, since a mock encodes what the author *believed* the response
   looked like. ``tasks.md`` 4.1 says it outright: "No geocoder mocking needed
   -- the cache makes it deterministic."

A cache entry stores the raw provider response, not parsed candidates. Parsing
is the part most likely to have a bug, so caching its *input* keeps it under
test; caching its output would freeze the bug alongside the fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Bumped when the stored shape changes, so stale entries are ignored rather
#: than misread.
CACHE_FORMAT_VERSION = 1


def cache_key(provider: str, query: str, params: dict[str, Any] | None = None) -> str:
    """A stable filename-safe key for one lookup.

    The query is normalised -- lowercased, whitespace collapsed -- so that
    "Newport", "newport" and " newport " are one cache entry rather than three
    requests.
    """
    normalised = " ".join((query or "").strip().lower().split())
    material = json.dumps(
        {
            "provider": provider,
            "query": normalised,
            "params": params or {},
            "version": CACHE_FORMAT_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass
class ResolutionCache:
    """A directory of recorded geocoder responses."""

    directory: Path
    #: When set, a miss stays a miss and nothing is written. The test suite
    #: uses this so that a fixture gap fails loudly as an unexpected network
    #: call, rather than silently reaching out and passing.
    read_only: bool = False

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)

    def path_for(self, provider: str, query: str, params: dict[str, Any] | None = None) -> Path:
        return self.directory / provider / f"{cache_key(provider, query, params)}.json"

    def get(
        self, provider: str, query: str, params: dict[str, Any] | None = None
    ) -> Any | None:
        """The recorded response, or ``None`` on a miss.

        A corrupt entry is treated as a miss rather than an error: the cache is
        an optimisation, and a half-written file should cost one request, not
        break the lookup.
        """
        path = self.path_for(provider, query, params)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("version") != CACHE_FORMAT_VERSION:
            return None
        return payload.get("response")

    def put(
        self,
        provider: str,
        query: str,
        response: Any,
        params: dict[str, Any] | None = None,
    ) -> Path | None:
        """Record a response. Returns the path written, or ``None``."""
        if self.read_only:
            return None
        path = self.path_for(provider, query, params)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_FORMAT_VERSION,
            "provider": provider,
            # The original query is kept alongside the hash so a human can tell
            # what a fixture file is for without recomputing the key.
            "query": query,
            "params": params or {},
            "response": response,
        }
        # Written atomically: a cache half-written by an interrupted run would
        # otherwise be read back as a corrupt entry on every subsequent call.
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return path

    def entries(self) -> list[Path]:
        """Every cache file, for inspection and for fixture listings."""
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.rglob("*.json"))


__all__ = ["CACHE_FORMAT_VERSION", "ResolutionCache", "cache_key"]
