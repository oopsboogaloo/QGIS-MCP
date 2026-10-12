"""Record real provider responses as committed test fixtures (task 4.4).

    python test-data/record_provider_fixtures.py

Same reasoning as the geocoder recorder: a hand-written stand-in encodes what
its author believed the response looked like, and the details that matter are
the ones nobody would invent.

**Three responses, and the error bodies are the valuable ones.** The successful
fetch returns raw GeoTIFF bytes, which involves almost no parsing -- its worth
as a fixture is that a test can drive the whole fetch path offline and then
check QGIS actually opens what was written. The *rejections* are where real
parsing happens: OpenTopography answers a bad request with HTTP 400 and an XML
``<error>`` element, and the message inside names the problem precisely enough
to pass straight through to the caller.

**This costs API calls.** The free non-academic allowance is 50 per 24 hours,
and this script spends three. Already-recorded responses are skipped unless
--force is given, and the AOI is deliberately tiny -- about 1 km2, which comes
back as roughly 9 KB.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
FIXTURE_DIR = SCRIPT_DIR / "provider-fixtures" / "opentopography"

sys.path.insert(0, str(REPO_ROOT / "src"))

from qgis_mcp_server.config import Config  # noqa: E402
from qgis_mcp_server.providers.catalogue import OPENTOPOGRAPHY_CREDENTIAL  # noqa: E402
from qgis_mcp_server.providers.opentopography import (  # noqa: E402
    ENDPOINT,
    USER_AGENT,
)

#: The fixture AOI, over the same Hampshire ground as the synthetic fixtures
#: (OS grid square SU4080). Tiny on purpose: 0.015 by 0.010 degrees.
FIXTURE_AOI = (-1.270, 51.170, -1.255, 51.180)

CASES = [
    (
        "cop30_su4080.tif",
        {"demtype": "COP30"},
        "a successful fetch of the smallest useful AOI, so a test can drive the "
        "whole fetch path offline and confirm QGIS opens what was written",
    ),
    (
        "error_bad_demtype.xml",
        {"demtype": "NOTADEM"},
        "an unknown dataset: HTTP 400 with an XML <error> body naming it",
    ),
    (
        "error_area_limit.xml",
        {"demtype": "COP30", "_aoi": (-170.0, -60.0, 170.0, 70.0)},
        "an over-limit area: HTTP 400 whose message states both the dataset's "
        "limit and the computed area, which is more useful than anything we "
        "could word ourselves",
    ),
]


def record(name: str, overrides: dict, api_key: str) -> tuple[str, int]:
    """Make one request and write whatever came back."""
    aoi = overrides.pop("_aoi", FIXTURE_AOI)
    min_lon, min_lat, max_lon, max_lat = aoi
    params = {
        "demtype": "COP30",
        "south": min_lat,
        "north": max_lat,
        "west": min_lon,
        "east": max_lon,
        "outputFormat": "GTiff",
        **overrides,
        "API_Key": api_key,
    }
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code

    path = FIXTURE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)

    # The request is recorded alongside the body, with the key removed. A
    # fixture whose provenance is unknown cannot be re-derived or trusted.
    meta = {
        "status": status,
        "demtype": params["demtype"],
        "aoi": list(aoi),
        "outputFormat": params["outputFormat"],
        "bytes": len(body),
        "endpoint": ENDPOINT,
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    return f"HTTP {status}", len(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record OpenTopography responses as committed fixtures."
    )
    parser.add_argument(
        "--force", action="store_true", help="re-request already-recorded cases."
    )
    args = parser.parse_args(argv)

    api_key = Config.load().credential_store.get(OPENTOPOGRAPHY_CREDENTIAL)
    if not api_key:
        print(
            "no OpenTopography API key configured. Set "
            f"{OPENTOPOGRAPHY_CREDENTIAL.env_var} or add it to the "
            "[credentials] table in qgis-mcp.toml.",
            file=sys.stderr,
        )
        return 2

    print(f"recording into {FIXTURE_DIR}")
    print(
        f"{len(CASES)} cases; the free non-academic allowance is 50 calls per "
        f"24 hours\n"
    )

    spent = 0
    for name, overrides, rationale in CASES:
        path = FIXTURE_DIR / name
        if path.exists() and not args.force:
            print(f"  = {name} already recorded ({path.stat().st_size} bytes)")
            continue
        if spent:
            time.sleep(1.5)
        status, size = record(name, dict(overrides), api_key)
        spent += 1
        print(f"  + {name}  {status}  {size} bytes")
        print(f"      why: {rationale}")

    print(f"\nspent {spent} API call(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
