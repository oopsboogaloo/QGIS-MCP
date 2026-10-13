"""Ordnance Survey Data Hub, via the NGD Features API (task 4.8).

OS publishes several APIs behind one key. This adapter uses the **NGD Features
API**, an OGC API Features service: collections of GeoJSON with real
server-side bbox filtering, which makes it the only provider here that returns
exactly the area asked for rather than something to clip afterwards.

Three things shape the implementation.

**Collections are versioned, and the version moves.** ``trn-ntwk-roadlink``
exists as v1 through v5, and OS adds versions without warning. Hardcoding v5
means silently using stale data the month v6 appears, so the latest version is
discovered from ``/collections`` and cached per adapter instance.

**Every request spends the account's allowance.** The free tier is a monthly
value of premium transactions, not a request count, so a runaway pager is a
bill rather than a rate-limit error. Paging is therefore capped, and a fetch
that hit the cap says so in the output rather than quietly returning a partial
layer that looks complete.

**Not everything OS sells is in this API.** OS Terrain 50 elevation and
Boundary-Line administrative areas come from the *Downloads* API, which is a
different service with a different shape. The provider capability declares
them because OS does offer them; this adapter does not serve them, and says so
rather than leaving a caller to wonder why a declared data type returned
nothing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_server.providers.base import Dataset, DataType, RestApiAdapter
from qgis_mcp_server.providers.catalogue import (
    OS_DATA_HUB,
    OS_DATA_HUB_CREDENTIAL,
    OS_DATA_HUB_LICENCE,
)

BASE_URL = "https://api.os.uk/features/ngd/ofa/v1"

USER_AGENT = "qgis-mcp/0.1 (QGIS MCP server; https://github.com/oopsboogaloo/QGIS-MCP)"

DEFAULT_TIMEOUT = 120.0

#: Features per request. The API's own maximum for this endpoint.
PAGE_SIZE = 100

#: Most pages a single fetch will follow.
#:
#: 50 pages is 5,000 features, which covers a town centre comfortably. The cap
#: exists because each page is a chargeable transaction: an unbounded pager
#: over a county would spend the month's allowance silently, and the failure
#: would arrive as a bill rather than an error.
MAX_PAGES = 50

#: Data types this adapter cannot serve, and where they actually live.
#:
#: Declared so a caller asking for UK elevation is told *why* OS returned
#: nothing, rather than concluding OS has no elevation data.
ELSEWHERE = {
    DataType.ELEVATION: (
        "OS Terrain 50 and Terrain 5 come from the OS Downloads API, which is "
        "a separate service from NGD Features. For UK elevation right now, "
        "OpenTopography's global DEMs are wired up and work."
    ),
    DataType.ADMIN_BOUNDARIES: (
        "OS Boundary-Line comes from the OS Downloads API rather than NGD "
        "Features. Natural Earth serves administrative boundaries at small "
        "scales in the meantime."
    ),
    DataType.BASEMAP: (
        "OS Maps raster tiles are a tile service, not a feature collection; "
        "add them to QGIS as an XYZ layer rather than downloading them."
    ),
}


class OsDataHubError(Exception):
    """An OS Data Hub request failed."""


@dataclass(frozen=True)
class NgdTheme:
    """One data type, mapped to an NGD collection family."""

    data_type: str
    family: str
    title: str
    #: Rough features per square kilometre, for the size estimate. OS feature
    #: density varies hugely between a city and a moor, so this is an order of
    #: magnitude and is labelled as such in the response.
    features_per_km2: int
    notes: str = ""


THEMES = (
    NgdTheme(
        data_type=DataType.ROADS,
        family="trn-ntwk-roadlink",
        title="OS road network links",
        features_per_km2=120,
        notes=(
            "The routable road network, with names, classifications and "
            "road numbers. Already in EPSG:27700 terms though served as "
            "lon/lat, and far more consistent than OSM for UK work."
        ),
    ),
    NgdTheme(
        data_type=DataType.RAILWAYS,
        family="trn-ntwk-railwaylink",
        title="OS railway network links",
        features_per_km2=4,
        notes=(
            "The operational rail network as routable links. OS removes lines "
            "once lifted, so a closed branch appears here only while it is "
            "still track on the ground --- OpenStreetMap keeps them tagged as "
            "disused or abandoned, which is the better source for historic "
            "alignments."
        ),
    ),
    NgdTheme(
        data_type=DataType.BUILDINGS,
        family="bld-fts-building",
        title="OS building footprints",
        features_per_km2=900,
        notes="Every building footprint, which makes this the densest theme.",
    ),
    NgdTheme(
        data_type=DataType.HYDROLOGY,
        family="wtr-fts-water",
        title="OS water areas",
        features_per_km2=40,
    ),
    NgdTheme(
        data_type=DataType.LANDCOVER,
        family="lnd-fts-land",
        title="OS land cover",
        features_per_km2=200,
    ),
    NgdTheme(
        data_type=DataType.PLACE_NAMES,
        family="gnm-fts-namedpoint",
        title="OS named points",
        features_per_km2=15,
    ),
)


def latest_collection(collection_ids: list[str], family: str) -> str | None:
    """The highest-numbered version of ``family`` among ``collection_ids``.

    OS versions collections with a numeric suffix and adds versions over time.
    Picking the highest is what keeps this adapter current without edits; the
    alternative is a constant that silently goes stale.

    Compared numerically rather than as text, because ``-10`` must beat ``-9``
    and string ordering gets that backwards.
    """
    best: tuple[int, str] | None = None
    prefix = f"{family}-"
    for identifier in collection_ids:
        if not identifier.startswith(prefix):
            continue
        suffix = identifier[len(prefix) :]
        if not suffix.isdigit():
            continue
        version = int(suffix)
        if best is None or version > best[0]:
            best = (version, identifier)
    return best[1] if best else None


def aoi_area_km2(aoi: tuple[float, float, float, float]) -> float:
    """Rough AOI area in square kilometres, for the size estimate.

    A flat approximation with a cosine correction for longitude. Good enough
    for an estimate across the UK's 11 degrees of latitude, and a proper
    geodesic area would imply a precision the feature-density figures do not
    have anyway.
    """
    import math

    min_lon, min_lat, max_lon, max_lat = aoi
    mid_lat = math.radians((min_lat + max_lat) / 2)
    width = abs(max_lon - min_lon) * 111.32 * math.cos(mid_lat)
    height = abs(max_lat - min_lat) * 110.57
    return width * height


class OsDataHubAdapter(RestApiAdapter):
    """OS NGD Features, behind the shared adapter interface."""

    capability = OS_DATA_HUB

    def __init__(
        self,
        credentials=None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_pages: int = MAX_PAGES,
        page_size: int = PAGE_SIZE,
    ) -> None:
        super().__init__(credentials)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_pages = max_pages
        self.page_size = page_size
        self._collection_ids: list[str] | None = None

    # -- helpers -----------------------------------------------------------

    def _key(self) -> str:
        key = self.credentials.get(OS_DATA_HUB_CREDENTIAL)
        if not key:
            raise OsDataHubError(
                f"no OS Data Hub API key is configured. Register at "
                f"{OS_DATA_HUB_CREDENTIAL.registration_url} and set "
                f"{OS_DATA_HUB_CREDENTIAL.env_var}, or add it to the "
                f"[credentials] table in qgis-mcp.toml."
            )
        return key

    def _get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read()[:400].decode("utf-8", errors="replace")
            hint = ""
            if exc.code == 401:
                hint = (
                    " The key was rejected. Check it is enabled for the NGD "
                    "Features API on the project it belongs to --- OS keys are "
                    "per project and per API."
                )
            elif exc.code == 429:
                hint = (
                    " The account's transaction allowance is exhausted or the "
                    "rate limit was hit."
                )
            raise OsDataHubError(
                f"OS Data Hub refused the request (HTTP {exc.code}).{hint} {body}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OsDataHubError(f"could not reach OS Data Hub: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise OsDataHubError(f"OS Data Hub sent unreadable JSON: {exc}") from exc

    def collection_ids(self) -> list[str]:
        """Every collection the key can see, fetched once per adapter."""
        if self._collection_ids is None:
            url = f"{self.base_url}/collections?" + urllib.parse.urlencode(
                {"key": self._key()}
            )
            payload = self._get(url)
            self._collection_ids = [
                collection.get("id", "")
                for collection in payload.get("collections", [])
            ]
        return self._collection_ids

    # -- search ------------------------------------------------------------

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """Collections available for ``aoi``.

        One network call, for the collection list, so that the versions
        reported are the ones that exist today rather than the ones that
        existed when this was written.
        """
        if not self.capability.covers(aoi):
            return []

        try:
            available = self.collection_ids()
        except OsDataHubError:
            # REQ-10: an adapter that cannot answer is reported by the
            # dispatcher with its status, never allowed to fail the menu. The
            # exception carries the reason and the dispatcher records it.
            raise

        area = aoi_area_km2(aoi)
        datasets = []
        for theme in THEMES:
            if data_types and theme.data_type not in data_types:
                continue
            identifier = latest_collection(available, theme.family)
            if identifier is None:
                continue
            estimated_features = int(area * theme.features_per_km2)
            datasets.append(
                Dataset(
                    provider=self.name,
                    identifier=identifier,
                    title=f"{theme.title} ({identifier})",
                    data_type=theme.data_type,
                    licence=OS_DATA_HUB_LICENCE,
                    crs="EPSG:4326",
                    # ~400 bytes per feature as GeoJSON, which OS attribute
                    # richness makes a floor rather than an average.
                    estimated_size_bytes=estimated_features * 400,
                    format="GeoJSON",
                    attribution=OS_DATA_HUB_LICENCE.attribution,
                    # Preferred over OSM and Natural Earth inside the UK: it is
                    # surveyed rather than crowd-sourced, and consistent.
                    preference=0,
                    extra={
                        "collection": identifier,
                        "family": theme.family,
                        "aoi_area_km2": round(area, 2),
                        "estimated_features": estimated_features,
                        "estimated_pages": max(
                            1, -(-estimated_features // self.page_size)
                        ),
                        "max_pages": self.max_pages,
                        "size_estimate_quality": (
                            "order of magnitude only: OS feature density varies "
                            "by more than tenfold between a city centre and open "
                            "country"
                        ),
                        "transaction_note": (
                            "each page is a chargeable transaction against the "
                            "account's monthly allowance"
                        ),
                        "notes": theme.notes,
                    },
                )
            )

        for data_type, reason in ELSEWHERE.items():
            if data_types and data_type not in data_types:
                continue
            if not data_types and data_type not in self.capability.data_types:
                continue
            # Not a Dataset: there is nothing to fetch. Recorded on the first
            # returned dataset instead, so the information reaches the caller
            # without pretending to be an offer.
            if datasets:
                datasets[0].extra.setdefault("not_served_here", {})[data_type] = reason

        return datasets

    # -- fetch -------------------------------------------------------------

    def fetch(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        destination: Path,
    ) -> Path:
        """Page through the collection's features for ``aoi``."""
        key = self._key()
        min_lon, min_lat, max_lon, max_lat = aoi
        url = f"{self.base_url}/collections/{dataset.identifier}/items?" + (
            urllib.parse.urlencode(
                {
                    "key": key,
                    # OGC API Features bbox is min_x,min_y,max_x,max_y in
                    # CRS84 -- the same order as this project's AOI
                    # convention, for once.
                    "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                    "limit": self.page_size,
                }
            )
        )

        features: list[dict[str, Any]] = []
        pages = 0
        truncated = False
        while url and pages < self.max_pages:
            payload = self._get(url)
            features.extend(payload.get("features", []))
            pages += 1
            url = _next_link(payload, key)
            if url and pages >= self.max_pages:
                truncated = True

        collection = {
            "type": "FeatureCollection",
            "crs": {
                "type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
            },
            "features": features,
            "attribution": OS_DATA_HUB_LICENCE.attribution,
            "source": {
                "provider": self.name,
                "collection": dataset.identifier,
                "aoi": list(aoi),
                "pages": pages,
                "features": len(features),
                "truncated": truncated,
            },
        }
        if truncated:
            # Stated in the data as well as the result, because a partial
            # layer that looks complete is the failure worth preventing: the
            # map simply stops having buildings part-way across, and nothing
            # says why.
            collection["source"]["truncated_note"] = (
                f"stopped after {self.max_pages} pages ({len(features)} "
                f"features); there are more. Narrow the AOI, or raise "
                f"max_pages knowing each page is a chargeable transaction."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(collection), encoding="utf-8")
        return destination


def _next_link(payload: dict[str, Any], key: str) -> str | None:
    """The ``next`` page URL, with the key reattached.

    OS returns a next link without the API key, so following it verbatim gets
    a 401 on page two --- which looks like the key expiring mid-fetch.
    """
    for link in payload.get("links", []):
        if link.get("rel") != "next" or not link.get("href"):
            continue
        href = link["href"]
        if "key=" in href:
            return href
        separator = "&" if "?" in href else "?"
        return f"{href}{separator}key={urllib.parse.quote(key)}"
    return None


__all__ = [
    "BASE_URL",
    "ELSEWHERE",
    "MAX_PAGES",
    "PAGE_SIZE",
    "THEMES",
    "NgdTheme",
    "OsDataHubAdapter",
    "OsDataHubError",
    "aoi_area_km2",
    "latest_collection",
]
