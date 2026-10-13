"""OpenHistoricalMap: the same data model, with time (added after task 4.8).

OSM for the past. Same Overpass query language, same element shapes, same
tags --- plus ``start_date`` and ``end_date`` on features, which is what makes
"what stood here in 1925" a filter rather than a guess.

It reuses the OpenStreetMap adapter's machinery deliberately: the tiling,
de-duplicating merge and GeoJSON conversion are identical problems and were
hard-won. What differs is three things, and each is a real difference rather
than a configuration:

**The date filter is the point.** Without one, a query returns every feature
OHM holds for the area across all of history, which for a city is both large
and meaningless --- Roman road and 1960s flyover in one layer.

**It is applied here, not by Overpass, and that was learnt the hard way.** The
obvious implementation is an Overpass ``(if:)`` clause, and the first version
of this adapter used one against ``start_decdate``/``end_decdate``. Those tags
do not exist in OHM: it serves the raw, human-written ``start_date`` strings.
``!is_tag('start_decdate')`` was therefore always true, the clause always
passed, and the filter silently did nothing --- identical results for 1500,
1900 and 2024 were what finally gave it away. The dates are messy enough to
need real parsing (``1849-10-30``, ``1882``, ``C19``, ``~1900``, ``1890s``,
absent), so that happens in :func:`parse_year` where it can be tested, and a
little more is downloaded than is kept.

**An absent date is not a date of zero.** A feature with a start and no end
has not been demolished; one with an end and no start predates the record.
Most of OHM carries no dates at all, so dropping the undated would empty the
map and look like poor coverage rather than an over-strict query. They are
kept by default, and the response says how many the filter removed.

**Coverage is thin and uneven.** OHM is volunteer-built and nothing like
present-day OSM for completeness. London is among the better-mapped places and
is still partial. An empty result means "nobody has mapped this yet" far more
often than "nothing was there", and the adapter says so rather than letting a
blank layer imply an empty landscape.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_server.providers.base import Dataset, DataType, OverpassAdapter
from qgis_mcp_server.providers.catalogue import (
    OPENHISTORICALMAP,
    OPENHISTORICALMAP_LICENCE,
)
from qgis_mcp_server.providers.openstreetmap import (
    elements_to_geojson,
    merge_elements,
    tile_aoi,
)

#: OHM runs its own Overpass instance. There is only one, so a failure here is
#: a failure --- there is no mirror to fall back to, unlike OSM proper.
ENDPOINTS = ("https://overpass-api.openhistoricalmap.org/api/interpreter",)

USER_AGENT = (
    "qgis-mcp/0.1 (QGIS MCP server; https://github.com/oopsboogaloo/QGIS-MCP)"
)

QUERY_TIMEOUT = 180
SOCKET_TIMEOUT = 240.0
TILE_PAUSE_SECONDS = 1.5

#: Larger tiles than the OpenStreetMap adapter uses, deliberately.
#:
#: That threshold is sized for present-day OSM, where a dense city centre
#: can return hundreds of thousands of elements. OHM holds a tiny fraction
#: of that for the same ground, so OSM's tiling would issue twenty queries
#: where two will do --- against a single volunteer-run instance with no
#: mirror to spread the load.
MAX_TILE_DEGREES_SQUARED = 0.25


class OhmError(Exception):
    """OpenHistoricalMap could not serve the request."""


@dataclass(frozen=True)
class HistoricTheme:
    """One data type, and the Overpass QL that selects it."""

    data_type: str
    identifier: str
    title: str
    selectors: tuple[str, ...]
    element_kinds: tuple[str, ...] = ("way", "relation")
    notes: str = ""


THEMES = (
    HistoricTheme(
        data_type=DataType.BUILDINGS,
        identifier="ohm_buildings",
        title="OpenHistoricalMap buildings",
        selectors=('["building"]',),
        notes=(
            "Named buildings with construction and demolition dates. This is "
            "where OHM is richest, and also where its coverage is most "
            "obviously partial: the notable survives, the ordinary terrace "
            "usually does not."
        ),
    ),
    HistoricTheme(
        data_type=DataType.ROADS,
        identifier="ohm_roads",
        title="OpenHistoricalMap roads",
        selectors=('["highway"]',),
        element_kinds=("way",),
    ),
    HistoricTheme(
        data_type=DataType.RAILWAYS,
        identifier="ohm_railways",
        title="OpenHistoricalMap railways",
        selectors=('["railway"]',),
        element_kinds=("way",),
        notes=(
            "The theme OHM is best suited to: the network changed constantly, "
            "and almost every line of a given year is gone or altered now."
        ),
    ),
    HistoricTheme(
        data_type=DataType.HYDROLOGY,
        identifier="ohm_water",
        title="OpenHistoricalMap waterways and water",
        selectors=('["waterway"]', '["natural"="water"]'),
    ),
    HistoricTheme(
        data_type=DataType.LANDCOVER,
        identifier="ohm_landuse",
        title="OpenHistoricalMap land use",
        selectors=('["landuse"]',),
    ),
    HistoricTheme(
        data_type=DataType.ADMIN_BOUNDARIES,
        identifier="ohm_boundaries",
        title="OpenHistoricalMap administrative boundaries",
        selectors=('["boundary"="administrative"]',),
        notes=(
            "Boundaries are where a date matters most: county and borough "
            "lines moved repeatedly, and a modern boundary over a historic "
            "map is the commonest way to get a period map quietly wrong."
        ),
    ),
)

THEMES_BY_IDENTIFIER = {theme.identifier: theme for theme in THEMES}


#: Matches the year at the front of an OSM-style date string.
_YEAR = re.compile(r"^\s*[~?c.]*\s*(-?\d{1,4})")

#: ``C19`` and ``19C`` both mean the nineteenth century in OSM date syntax.
_CENTURY = re.compile(r"^\s*[~?]*\s*(?:C\s*(\d{1,2})|(\d{1,2})\s*C)\s*$", re.I)


def parse_year(value: Any) -> int | None:
    """Best-effort year from an OSM-style date string, or ``None``.

    These are written by people, not machines, and the variety is the whole
    problem: ``1849-10-30``, ``1882``, ``~1900``, ``C19``, ``1890s``, ``?``,
    and absent. Returning ``None`` for anything unrecognised is deliberate ---
    guessing a year for ``C19`` to the decade would be inventing precision the
    source does not have, and silently misplacing a feature in time is exactly
    the failure a date filter is supposed to prevent.

    A century is taken as its *first* year, which errs toward including a
    feature in a query rather than excluding it.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    century = _CENTURY.match(text)
    if century:
        number = int(century.group(1) or century.group(2))
        return (number - 1) * 100 + 1

    match = _YEAR.match(text)
    if match:
        return int(match.group(1))
    return None


def extant_in(tags: dict[str, Any], year: int, *, keep_undated: bool = True) -> bool:
    """Whether a feature with these tags existed in ``year``.

    Open-ended at both ends: a building with a start and no end has not been
    demolished, and one with an end and no start was there before the map
    began. Treating a missing end as "gone" would empty the map of everything
    still standing.
    """
    start = parse_year(tags.get("start_date"))
    end = parse_year(tags.get("end_date"))
    if start is None and end is None:
        return keep_undated
    if start is not None and start > year:
        return False
    if end is not None and end < year:
        return False
    return True


def build_query(
    theme: HistoricTheme,
    tile: tuple[float, float, float, float],
    *,
    timeout: int = QUERY_TIMEOUT,
) -> str:
    """Overpass QL for one theme over one tile.

    **No date filtering here, deliberately.** The obvious implementation puts
    an Overpass ``(if:)`` clause on the query, and the first version of this
    adapter did exactly that against ``start_decdate``/``end_decdate``. Those
    tags do not exist in OHM --- it serves the raw ``start_date`` strings ---
    so ``!is_tag('start_decdate')`` was always true and the clause always
    passed. Identical results for 1500, 1900 and 2024 were what gave it away.

    The dates are messy enough to need real parsing, so that happens in
    :func:`extant_in` where it can be tested, and the cost is downloading a
    little more than is kept. OHM is sparse; it is a cheap trade.
    """
    min_lon, min_lat, max_lon, max_lat = tile
    box = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    lines = "\n".join(
        f"  {kind}{selector}({box});"
        for selector in theme.selectors
        for kind in theme.element_kinds
    )
    return f"[out:json][timeout:{timeout}];\n(\n{lines}\n);\nout geom;"


class OpenHistoricalMapAdapter(OverpassAdapter):
    """Historic vector data, by year."""

    capability = OPENHISTORICALMAP

    def __init__(
        self,
        credentials=None,
        *,
        endpoints: tuple[str, ...] = ENDPOINTS,
        socket_timeout: float = SOCKET_TIMEOUT,
        query_timeout: int = QUERY_TIMEOUT,
        tile_pause: float = TILE_PAUSE_SECONDS,
        year: int | None = None,
    ) -> None:
        super().__init__(credentials)
        self.endpoints = endpoints
        self.socket_timeout = socket_timeout
        self.query_timeout = query_timeout
        self.tile_pause = tile_pause
        #: Default year for searches and fetches. Overridable per dataset via
        #: ``extra["year"]``, which is how a caller asks for a different date
        #: without a second adapter.
        self.year = year
        self.last_endpoint: str | None = None

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """Themes available for ``aoi``. No network call."""
        try:
            tiles = len(tile_aoi(aoi, max_area=MAX_TILE_DEGREES_SQUARED))
            refusal = None
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            tiles = 0
            refusal = str(exc)

        datasets = []
        for theme in THEMES:
            if data_types and theme.data_type not in data_types:
                continue
            extra: dict[str, Any] = {
                "overpass_tiles": tiles,
                "year": self.year,
                "element_kinds": list(theme.element_kinds),
                "coverage_caveat": (
                    "OpenHistoricalMap is volunteer-built and far from "
                    "complete. An empty result usually means nobody has mapped "
                    "this area or period yet, not that nothing was there."
                ),
                "undated_features": (
                    "features with no start_date or end_date are kept by "
                    "default, because most of OHM has none; set "
                    "keep_undated=false for only those provably extant"
                ),
                "notes": theme.notes,
            }
            if refusal:
                extra["unavailable_reason"] = refusal
            datasets.append(
                Dataset(
                    provider=self.name,
                    identifier=theme.identifier,
                    title=(
                        f"{theme.title} ({self.year})" if self.year else theme.title
                    ),
                    data_type=theme.data_type,
                    licence=OPENHISTORICALMAP_LICENCE,
                    crs="EPSG:4326",
                    # Unknowable without asking, and OHM density varies by
                    # orders of magnitude between a mapped city and everywhere
                    # else. Left unset rather than invented.
                    estimated_size_bytes=None,
                    format="GeoJSON",
                    attribution=OPENHISTORICALMAP_LICENCE.attribution,
                    # Behind every present-day source: this is historic data
                    # and should never win a search for what is there now.
                    preference=9,
                    extra=extra,
                )
            )
        return datasets

    def fetch(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        destination: Path,
    ) -> Path:
        """Fetch one theme for ``aoi``, at the dataset's year if it has one."""
        theme = THEMES_BY_IDENTIFIER.get(dataset.identifier)
        if theme is None:
            raise OhmError(
                f"{dataset.identifier!r} is not an OpenHistoricalMap theme. "
                f"Known: {', '.join(sorted(THEMES_BY_IDENTIFIER))}"
            )

        extra = dataset.extra or {}
        year = extra.get("year", self.year)
        keep_undated = bool(extra.get("keep_undated", True))

        tiles = tile_aoi(aoi, max_area=MAX_TILE_DEGREES_SQUARED)
        responses = []
        for index, tile in enumerate(tiles):
            if index:
                time.sleep(self.tile_pause)
            responses.append(
                self._query(build_query(theme, tile, timeout=self.query_timeout))
            )

        elements = merge_elements(responses)
        before = len(elements)
        if year is not None:
            elements = [
                element
                for element in elements
                if extant_in(element.get("tags") or {}, year, keep_undated=keep_undated)
            ]
        collection = elements_to_geojson(
            elements, attribution=OPENHISTORICALMAP_LICENCE.attribution
        )
        collection["source"] = {
            "provider": self.name,
            "theme": theme.identifier,
            "year": year,
            "keep_undated": keep_undated,
            "aoi": list(aoi),
            "tiles": len(tiles),
            "endpoint": self.last_endpoint,
            "features": len(collection["features"]),
            "features_before_date_filter": before,
            "date_filtered_out": before - len(elements),
        }
        if not collection["features"]:
            collection["source"]["empty_note"] = (
                "no features returned. OpenHistoricalMap coverage is partial, "
                "so this is far more likely to mean nobody has mapped this "
                "area or period than that the ground was empty."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(collection), encoding="utf-8")
        return destination

    def _query(self, query: str) -> dict[str, Any]:
        failures = []
        for endpoint in self.endpoints:
            try:
                request = urllib.request.Request(
                    endpoint,
                    data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
                    headers={"User-Agent": USER_AGENT},
                )
                with urllib.request.urlopen(
                    request, timeout=self.socket_timeout
                ) as response:
                    body = response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", errors="replace")
                failures.append(f"HTTP {exc.code}: {detail}")
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failures.append(str(exc))
                continue

            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                failures.append(f"unreadable response ({exc})")
                continue

            if "elements" not in payload:
                failures.append(payload.get("remark", "no 'elements' in response"))
                continue

            self.last_endpoint = endpoint
            return payload

        raise OhmError(
            "OpenHistoricalMap's Overpass instance did not answer. Unlike OSM "
            "there is only one, so there is no mirror to fall back to.\n  "
            + "\n  ".join(failures)
        )


__all__ = [
    "ENDPOINTS",
    "MAX_TILE_DEGREES_SQUARED",
    "THEMES",
    "THEMES_BY_IDENTIFIER",
    "HistoricTheme",
    "OhmError",
    "OpenHistoricalMapAdapter",
    "build_query",
    "extant_in",
    "parse_year",
]
