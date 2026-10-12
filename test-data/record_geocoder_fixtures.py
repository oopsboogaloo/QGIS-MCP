"""Record real geocoder responses as committed test fixtures (task 4.1).

    python test-data/record_geocoder_fixtures.py

``tasks.md`` 4.1 specifies "cached real responses committed as fixtures" and
"no geocoder mocking needed -- the cache makes it deterministic". This is the
script that produces them.

**Why recorded responses rather than hand-written mocks.** A mock encodes what
its author believed the response looked like, so a test built on one verifies
the parser against that belief. Nominatim's real payload has details nobody
would invent: ``boundingbox`` ordered ``[south, north, west, east]``, address
fields that vary by place type, occasional results with no bounding box at all.
Recording the real thing puts the actual parsing code under test against the
actual shape.

This makes live network calls, so it is **not** part of the test run. The test
suite reads the committed cache with network access disabled, and a fixture gap
fails as an explicit error rather than quietly going online.

Nominatim's usage policy is respected: an identifying User-Agent, requests
serialised at least 1.1 s apart, and a handful of queries rather than bulk.
Re-running it is cheap for the same reason the cache exists -- already-recorded
queries are skipped unless --force is given.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
FIXTURE_CACHE = SCRIPT_DIR / "geocoder-cache"

sys.path.insert(0, str(REPO_ROOT / "src"))

from qgis_mcp_server.geocoding import NominatimGeocoder, ResolutionCache  # noqa: E402
from qgis_mcp_server.geocoding.base import GeocoderError  # noqa: E402

#: Each query is here to exercise a specific branch of the REQ-9 contract.
#: They are documented rather than merely listed, because "why is this place in
#: the fixture set" is otherwise unanswerable six months later.
QUERIES: list[tuple[str, str]] = [
    (
        "Andover, Hampshire, England",
        "fully qualified, so it should resolve to one strong candidate -- the "
        "REQ-9 happy path where resolution proceeds without asking",
    ),
    (
        "Newport",
        "genuinely ambiguous within the UK alone (Wales, Isle of Wight, "
        "Shropshire, Essex) before counting the rest of the world. The case "
        "REQ-9 exists for: silently picking one produces data for the wrong "
        "place and nothing downstream can tell",
    ),
    (
        "Springfield",
        "ambiguous at a larger scale, to check the candidate list stays "
        "readable and the administrative context is what distinguishes entries",
    ),
    (
        "Hampshire, England",
        "an administrative area rather than a settlement: large bbox, and a "
        "real boundary polygon, which is what makes Nominatim more than a "
        "fallback (narrative spec section 4.6.1)",
    ),
    (
        "Stonehenge",
        "a named monument rather than a populated place -- small extent, so it "
        "exercises the point-like AOI warning",
    ),
    (
        "Qqzzxx Nonexistent Placename",
        "no match at all, which must read as not-found rather than as an error "
        "or an empty success",
    ),
]

#: Must match what NominatimGeocoder.resolve sends, or the keys will not line up
#: and every lookup will miss.
RECORD_LIMIT = 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record Nominatim responses as committed fixtures."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-request queries that are already recorded.",
    )
    args = parser.parse_args(argv)

    cache = ResolutionCache(FIXTURE_CACHE)
    geocoder = NominatimGeocoder(cache=cache)

    print(f"recording into {FIXTURE_CACHE}")
    print(f"{len(QUERIES)} queries, serialised at least 1.1 s apart\n")

    recorded = 0
    skipped = 0
    failed = 0

    for query, rationale in QUERIES:
        cache_params = {
            "format": "jsonv2",
            "limit": str(RECORD_LIMIT),
            "addressdetails": "1",
            "polygon_geojson": "1",
        }
        if not args.force and cache.get(geocoder.name, query, cache_params) is not None:
            print(f"  = {query!r} already recorded")
            skipped += 1
            continue

        if args.force:
            cache.path_for(geocoder.name, query, cache_params).unlink(missing_ok=True)

        try:
            candidates = geocoder.resolve(query, limit=RECORD_LIMIT)
        except GeocoderError as exc:
            print(f"  ! {query!r} FAILED: {exc}", file=sys.stderr)
            failed += 1
            continue

        print(f"  + {query!r} -> {len(candidates)} candidates")
        print(f"      why: {rationale}")
        for candidate in candidates[:3]:
            print(
                f"      {candidate.confidence:.3f}  {candidate.place_type:<16} "
                f"{candidate.display_name[:76]}"
            )
        recorded += 1

    print(f"\nrecorded {recorded}, skipped {skipped}, failed {failed}")
    if failed:
        print(
            "A failure here is usually a transient Nominatim problem or a rate "
            "limit. Wait a minute and re-run; recorded queries are skipped.",
            file=sys.stderr,
        )
        return 1

    entries = cache.entries()
    print(f"{len(entries)} fixture files in {FIXTURE_CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
