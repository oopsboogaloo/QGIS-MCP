"""OpenStreetMap via the Overpass API (task 4.8).

``design.md`` §7 names this the acquisition layer's most likely source of real
work beyond the interface, and it is right. Three things make it unlike the
other adapters:

**Large AOIs are refused rather than served slowly.** Overpass enforces its own
timeout and memory ceiling, and a county-sized query for buildings hits both.
So an AOI above a threshold is split into tiles, queried one at a time, and the
results merged. :func:`tile_aoi` and :func:`merge_elements` are the whole of
that, and both are pure functions so the arithmetic is testable without a
network.

**Merging needs de-duplication, not concatenation.** A way crossing a tile
boundary is returned by *both* tiles, in full, because ``out geom`` returns
whole geometries regardless of where the clip fell. Concatenating would draw
every boundary-crossing road twice -- invisible on a map, obvious in a feature
count, and quietly wrong in any length or area total computed from it.

**The public instances are shared and often saturated.** A 504 from the main
endpoint is routine rather than exceptional, so the mirrors are tried in turn
and the one that answered is recorded in the result.

Attribution is not optional here: ODbL requires it, and a *database* derived
from OSM carries share-alike. :data:`qgis_mcp_server.providers.catalogue`
states both, and they travel with every dataset this adapter returns.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from qgis_mcp_server.providers.base import Dataset, DataType, OverpassAdapter
from qgis_mcp_server.providers.catalogue import ODBL_LICENCE, OPENSTREETMAP

#: Public Overpass instances, tried in order.
#:
#: Several because the main one is frequently saturated and answers 504. This
#: is not defensive programming for a rare case: during development the first
#: real query of the session was refused by the primary endpoint.
#: Checked against live instances rather than copied from a list: at the time
#: of writing ``overpass.osm.jp`` served a certificate for a different
#: hostname, which fails verification before any query is sent, and
#: ``overpass.osm.ch`` answered in 0.2s while the main instance was returning
#: 504 and two others timed out. Mirrors come and go; this order is a starting
#: point, and :meth:`OpenStreetMapAdapter._ordered_endpoints` learns within a
#: run which one is actually answering.
ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

USER_AGENT = (
    "qgis-mcp/0.1 (QGIS MCP server; "
    "https://github.com/oopsboogaloo/QGIS-MCP)"
)

#: Seconds given to Overpass *inside* the query, and to the socket outside it.
#: The query value is what Overpass enforces; the socket value must exceed it
#: or the connection dies while the server is still working on an answer it
#: would have delivered.
QUERY_TIMEOUT = 120
SOCKET_TIMEOUT = 180.0

#: Largest AOI, in square degrees, sent to Overpass as a single query.
#:
#: Not derived from anything Overpass publishes --- it enforces element counts
#: and runtime, neither of which is knowable before asking. Chosen so that a
#: dense European city centre's building footprints come back inside
#: ``QUERY_TIMEOUT``, which is the worst realistic case per unit area.
MAX_TILE_DEGREES_SQUARED = 0.04

#: Never split beyond this, however large the AOI.
#:
#: A continental request would otherwise become thousands of polite queries
#: against a shared public service, which is how a client gets blocked. Past
#: this the honest answer is that Overpass is the wrong tool and a planet
#: extract is the right one, and the adapter says so.
MAX_TILES = 64

#: Courtesy pause between tile queries against a free shared service.
TILE_PAUSE_SECONDS = 1.0


class OverpassError(Exception):
    """Overpass could not serve the request."""


@dataclass(frozen=True)
class OsmTheme:
    """One data type, and the Overpass QL that selects it."""

    data_type: str
    identifier: str
    title: str
    #: Overpass selectors, each applied to the given element kinds.
    selectors: tuple[str, ...]
    element_kinds: tuple[str, ...] = ("way",)
    #: Rough bytes per square degree, for REQ-4d's size estimate. Order of
    #: magnitude only --- OSM density varies by orders of magnitude between a
    #: city and a moor, and no estimate made without asking can be better than
    #: that. Stated as an estimate in the response rather than implied exact.
    bytes_per_square_degree: int = 20_000_000
    notes: str = ""


THEMES = (
    OsmTheme(
        data_type=DataType.ROADS,
        identifier="osm_roads",
        title="OpenStreetMap roads and paths",
        selectors=('["highway"]',),
        bytes_per_square_degree=25_000_000,
        notes=(
            "Every highway class, from motorway to footway. Filter on the "
            "'highway' attribute after loading: footways and service roads "
            "usually outnumber the named road network several times over."
        ),
    ),
    OsmTheme(
        data_type=DataType.RAILWAYS,
        identifier="osm_railways",
        title="OpenStreetMap railways",
        selectors=('["railway"]',),
        bytes_per_square_degree=2_000_000,
        notes=(
            "Includes disused and abandoned lines, which are tagged "
            "railway=disused or railway=abandoned rather than dropped. Worth "
            "keeping: over LIDAR relief the embankment of a line lifted "
            "decades ago is often still plainly visible, and the two together "
            "say more than either alone. Filter on the 'railway' attribute to "
            "separate operational track from the rest."
        ),
    ),
    OsmTheme(
        data_type=DataType.BUILDINGS,
        identifier="osm_buildings",
        title="OpenStreetMap building footprints",
        selectors=('["building"]',),
        element_kinds=("way", "relation"),
        bytes_per_square_degree=60_000_000,
        notes=(
            "The densest OSM theme by far, and the one most likely to need "
            "tiling. Relations are included, so building parts of complexes "
            "are not dropped."
        ),
    ),
    OsmTheme(
        data_type=DataType.HYDROLOGY,
        identifier="osm_water",
        title="OpenStreetMap waterways and water bodies",
        selectors=('["waterway"]', '["natural"="water"]'),
        element_kinds=("way", "relation"),
        bytes_per_square_degree=10_000_000,
        notes="Linear waterways and water-body polygons in one layer.",
    ),
    OsmTheme(
        data_type=DataType.LANDCOVER,
        identifier="osm_landuse",
        title="OpenStreetMap land use and natural cover",
        selectors=('["landuse"]', '["natural"]'),
        element_kinds=("way", "relation"),
        bytes_per_square_degree=30_000_000,
        notes="Fields, woodland, residential areas, and natural surfaces.",
    ),
    OsmTheme(
        data_type=DataType.PLACE_NAMES,
        identifier="osm_places",
        title="OpenStreetMap place nodes",
        selectors=('["place"]',),
        element_kinds=("node",),
        bytes_per_square_degree=200_000,
        notes=(
            "Settlement and locality nodes with names and populations. "
            "Sparse, so it rarely needs tiling."
        ),
    ),
)

THEMES_BY_IDENTIFIER = {theme.identifier: theme for theme in THEMES}


# ---------------------------------------------------------------------------
# Tiling and merging -- pure, so the arithmetic is testable without a network
# ---------------------------------------------------------------------------


def aoi_area_degrees_squared(aoi: tuple[float, float, float, float]) -> float:
    """Area of an AOI in square degrees.

    Degrees rather than km2 deliberately: what Overpass struggles with is the
    number of elements, which tracks the span in the coordinate system it is
    queried in. Converting to km2 would add a latitude correction that makes
    the tiling harder to reason about without making it more accurate.
    """
    min_lon, min_lat, max_lon, max_lat = aoi
    return abs(max_lon - min_lon) * abs(max_lat - min_lat)


def tile_aoi(
    aoi: tuple[float, float, float, float],
    *,
    max_area: float = MAX_TILE_DEGREES_SQUARED,
    max_tiles: int = MAX_TILES,
) -> list[tuple[float, float, float, float]]:
    """Split ``aoi`` into tiles each no larger than ``max_area``.

    Returns the AOI unchanged when it already fits, so the common case costs
    one query and no arithmetic.

    The grid is square in tile *count* rather than in tile shape: a long thin
    AOI gets split along its long axis more than its short one, because that
    is what keeps each tile's element count down. Tiles share edges exactly,
    so nothing between them is missed; the duplicates that creates on the
    shared edges are :func:`merge_elements`'s problem.
    """
    min_lon, min_lat, max_lon, max_lat = aoi
    if min_lon > max_lon or min_lat > max_lat:
        raise OverpassError(
            f"the AOI {aoi} has its corners the wrong way round; expected "
            f"[min_lon, min_lat, max_lon, max_lat]"
        )

    area = aoi_area_degrees_squared(aoi)
    if area <= max_area or area == 0:
        return [aoi]

    width = max_lon - min_lon
    height = max_lat - min_lat

    # Split each axis in proportion to its length, so tiles come out roughly
    # square whatever the AOI's shape.
    import math

    tiles_needed = math.ceil(area / max_area)
    aspect = width / height if height else 1.0
    columns = max(1, round(math.sqrt(tiles_needed * aspect)))
    rows = max(1, math.ceil(tiles_needed / columns))

    if columns * rows > max_tiles:
        raise OverpassError(
            f"this AOI would need {columns * rows} Overpass queries and the "
            f"limit is {max_tiles}. Overpass is a query service for modest "
            f"areas, not a bulk download: for an area this size use a planet "
            f"extract (geofabrik.de) and load it locally. Refused before "
            f"sending anything, so no requests were spent."
        )

    lon_step = width / columns
    lat_step = height / rows
    tiles = []
    for row in range(rows):
        for column in range(columns):
            tiles.append(
                (
                    min_lon + column * lon_step,
                    min_lat + row * lat_step,
                    min_lon + (column + 1) * lon_step if column + 1 < columns else max_lon,
                    min_lat + (row + 1) * lat_step if row + 1 < rows else max_lat,
                )
            )
    return tiles


def merge_elements(responses: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge Overpass responses, dropping elements seen more than once.

    The de-duplication is the point. ``out geom`` returns a way's *entire*
    geometry whenever the way intersects the queried box, so a road crossing a
    tile boundary comes back complete from both tiles. Concatenating would
    double it: invisible on screen, since the two copies are identical and
    coincident, and wrong in every count, length and area derived from the
    layer afterwards.

    Keyed by element type and id together, because a node and a way may share
    an id --- they are separate id spaces in OSM.
    """
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for response in responses:
        for element in response.get("elements", []):
            key = (element.get("type", "?"), element.get("id", -1))
            # First wins. Copies from different tiles are the same element, so
            # which one is kept does not matter; that it is kept once does.
            merged.setdefault(key, element)
    return list(merged.values())


def build_query(
    theme: OsmTheme,
    tile: tuple[float, float, float, float],
    *,
    timeout: int = QUERY_TIMEOUT,
) -> str:
    """Overpass QL for one theme over one tile.

    Overpass takes a bounding box as ``south,west,north,east`` --- a different
    order again from GeoJSON's ``[lon, lat]``, from QGIS extent strings, and
    from this project's own AOI convention. Spelled out with named locals
    rather than sliced inline, because this is the fourth coordinate-order
    convention in the codebase and a silent transposition here would return
    data for somewhere else entirely.
    """
    min_lon, min_lat, max_lon, max_lat = tile
    south, west, north, east = min_lat, min_lon, max_lat, max_lon
    box = f"{south},{west},{north},{east}"

    clauses = "\n".join(
        f"  {kind}{selector}({box});"
        for selector in theme.selectors
        for kind in theme.element_kinds
    )
    return f"[out:json][timeout:{timeout}];\n(\n{clauses}\n);\nout geom;"


def elements_to_geojson(
    elements: Iterable[dict[str, Any]], *, attribution: str = ""
) -> dict[str, Any]:
    """Convert Overpass elements to a GeoJSON FeatureCollection.

    Geometry rules, which are OSM's rather than this adapter's choices:

    * a node becomes a Point;
    * a way whose first and last positions coincide becomes a Polygon, since
      that is how OSM represents an area --- there is no separate area type;
    * any other way becomes a LineString;
    * a relation without resolved geometry is skipped rather than guessed at.

    Tags become properties as-is. Filtering them here would mean deciding on
    the caller's behalf which of OSM's open-ended tagging is interesting, and
    the caller is better placed to know.
    """
    features = []
    for element in elements:
        kind = element.get("type")
        tags = dict(element.get("tags") or {})

        if kind == "node":
            if "lat" not in element or "lon" not in element:
                continue
            geometry = {
                "type": "Point",
                "coordinates": [element["lon"], element["lat"]],
            }
        else:
            points = element.get("geometry") or []
            coordinates = [
                [point["lon"], point["lat"]]
                for point in points
                if point and "lon" in point and "lat" in point
            ]
            if len(coordinates) < 2:
                continue
            if len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
                geometry = {"type": "Polygon", "coordinates": [coordinates]}
            else:
                geometry = {"type": "LineString", "coordinates": coordinates}

        features.append(
            {
                "type": "Feature",
                "id": element.get("id"),
                "geometry": geometry,
                "properties": {
                    "osm_id": element.get("id"),
                    "osm_type": kind,
                    **tags,
                },
            }
        )

    collection: dict[str, Any] = {
        "type": "FeatureCollection",
        # CRS84 is GeoJSON's only CRS, stated explicitly so a reader does not
        # have to assume it.
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": features,
    }
    if attribution:
        collection["attribution"] = attribution
    return collection


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class OpenStreetMapAdapter(OverpassAdapter):
    """OpenStreetMap vector data through Overpass."""

    capability = OPENSTREETMAP

    def __init__(
        self,
        credentials=None,
        *,
        endpoints: tuple[str, ...] = ENDPOINTS,
        socket_timeout: float = SOCKET_TIMEOUT,
        query_timeout: int = QUERY_TIMEOUT,
        tile_pause: float = TILE_PAUSE_SECONDS,
    ) -> None:
        super().__init__(credentials)
        self.endpoints = endpoints
        self.socket_timeout = socket_timeout
        self.query_timeout = query_timeout
        self.tile_pause = tile_pause
        #: The endpoint that last answered, reported in fetch results so a slow
        #: or failing mirror is visible rather than guessed at.
        self.last_endpoint: str | None = None

    # -- search ------------------------------------------------------------

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """Themes available for ``aoi``, with no network call.

        OSM covers everywhere and Overpass has no catalogue to query, so this
        is a filter over the declared themes. The useful work it does is the
        size and tiling estimate, which is what REQ-4d's confirmation needs in
        order to be a real decision rather than a formality.
        """
        area = aoi_area_degrees_squared(aoi)
        try:
            tiles = len(tile_aoi(aoi))
            refusal = None
        except OverpassError as exc:
            tiles = 0
            refusal = str(exc)

        datasets = []
        for theme in THEMES:
            if data_types and theme.data_type not in data_types:
                continue
            extra: dict[str, Any] = {
                "aoi_area_deg2": round(area, 5),
                "overpass_tiles": tiles,
                "element_kinds": list(theme.element_kinds),
                "notes": theme.notes,
            }
            if tiles > 1:
                extra["tiling_note"] = (
                    f"this AOI exceeds what one Overpass query reliably "
                    f"returns, so it will be fetched as {tiles} tiles and "
                    f"merged, with duplicates across tile edges removed"
                )
            if refusal:
                extra["unavailable_reason"] = refusal
            datasets.append(
                Dataset(
                    provider=self.name,
                    identifier=theme.identifier,
                    title=theme.title,
                    data_type=theme.data_type,
                    licence=ODBL_LICENCE,
                    crs="EPSG:4326",
                    estimated_size_bytes=int(area * theme.bytes_per_square_degree),
                    format="GeoJSON",
                    attribution=ODBL_LICENCE.attribution,
                    extra=extra,
                )
            )
        return datasets

    # -- fetch -------------------------------------------------------------

    def fetch(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        destination: Path,
    ) -> Path:
        """Fetch one theme for ``aoi``, tiling and merging as needed."""
        theme = THEMES_BY_IDENTIFIER.get(dataset.identifier)
        if theme is None:
            raise OverpassError(
                f"{dataset.identifier!r} is not an OpenStreetMap theme. "
                f"Known: {', '.join(sorted(THEMES_BY_IDENTIFIER))}"
            )

        tiles = tile_aoi(aoi)
        responses = []
        for index, tile in enumerate(tiles):
            if index:
                # A courtesy pause, not a rate limit: these are free shared
                # instances and a burst from one client is how everyone's
                # queries get slower.
                time.sleep(self.tile_pause)
            responses.append(self._query(build_query(theme, tile, timeout=self.query_timeout)))

        elements = merge_elements(responses)
        collection = elements_to_geojson(
            elements, attribution=ODBL_LICENCE.attribution
        )
        collection["source"] = {
            "provider": self.name,
            "theme": theme.identifier,
            "aoi": list(aoi),
            "tiles": len(tiles),
            "endpoint": self.last_endpoint,
            "elements_before_merge": sum(
                len(response.get("elements", [])) for response in responses
            ),
            "elements_after_merge": len(elements),
        }

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(collection), encoding="utf-8")
        return destination

    def _ordered_endpoints(self) -> tuple[str, ...]:
        """Mirrors to try, most recently successful first.

        This matters precisely when tiling does: a saturated instance takes
        nine seconds to answer 504 and another forty-five to time out, so
        rediscovering that for every one of sixteen tiles costs more than the
        queries themselves. The first tile pays to find a working mirror; the
        rest go straight to it.
        """
        if self.last_endpoint is None or self.last_endpoint not in self.endpoints:
            return tuple(self.endpoints)
        rest = tuple(e for e in self.endpoints if e != self.last_endpoint)
        return (self.last_endpoint, *rest)

    def _query(self, query: str) -> dict[str, Any]:
        """Run one Overpass query, trying each mirror in turn."""
        failures = []
        for endpoint in self._ordered_endpoints():
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
                failures.append(f"{_host(endpoint)}: HTTP {exc.code}")
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failures.append(f"{_host(endpoint)}: {exc}")
                continue

            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                failures.append(f"{_host(endpoint)}: unreadable response ({exc})")
                continue

            if "elements" not in payload:
                remark = payload.get("remark", "no 'elements' in the response")
                failures.append(f"{_host(endpoint)}: {remark}")
                continue

            self.last_endpoint = endpoint
            return payload

        raise OverpassError(
            "every Overpass mirror refused this query. A 504 from the main "
            "instance is routine --- it is a free shared service --- so this "
            "is worth retrying before treating it as a real failure.\n  "
            + "\n  ".join(failures)
        )


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc or url


__all__ = [
    "ENDPOINTS",
    "MAX_TILES",
    "MAX_TILE_DEGREES_SQUARED",
    "THEMES",
    "THEMES_BY_IDENTIFIER",
    "OpenStreetMapAdapter",
    "OsmTheme",
    "OverpassError",
    "aoi_area_degrees_squared",
    "build_query",
    "elements_to_geojson",
    "merge_elements",
    "tile_aoi",
]
