"""The Nominatim geocoder (task 4.1).

The global no-key default, per the narrative spec §4.6.1: it needs no
registration, covers everywhere, and -- with ``polygon_geojson`` -- returns real
boundary polygons rather than only rectangles. That last point is what makes it
more than a fallback: clipping a city's streets to its actual built-up boundary
instead of its bounding rectangle is the difference between a render of a city
and a render of a city plus the surrounding farmland.

**Usage policy is a design constraint, not a footnote.** Nominatim is free and
asks for an identifying User-Agent, no more than one request per second, and no
bulk querying. So this adapter sends a descriptive User-Agent, serialises its
own requests with a minimum interval, and reads from the cache first -- which
turns a repeated lookup into no request at all.

Standard library only: ``urllib`` rather than a new HTTP dependency, since one
GET with query parameters does not justify one.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, ClassVar

from qgis_mcp_server.geocoding.base import Geocoder, GeocoderError, PlaceCandidate
from qgis_mcp_server.geocoding.cache import ResolutionCache

ENDPOINT = "https://nominatim.openstreetmap.org/search"

#: Nominatim's policy asks for an identifying User-Agent naming the application.
#: A generic or absent one is grounds for being blocked.
USER_AGENT = (
    "qgis-mcp/0.1 (QGIS MCP server; +https://github.com/oopsboogaloo/QGIS-MCP)"
)

#: Minimum seconds between requests from this process.
MIN_REQUEST_INTERVAL = 1.1

DEFAULT_TIMEOUT = 15.0


def bbox_from_nominatim(boundingbox: list[Any]) -> tuple[float, float, float, float]:
    """Convert Nominatim's bounding box to ``(min_lon, min_lat, max_lon, max_lat)``.

    Nominatim orders it ``[south, north, west, east]`` -- latitudes first, then
    longitudes. That is the third coordinate-order convention in this codebase,
    after GeoJSON's ``(min_lon, min_lat, max_lon, max_lat)`` and QGIS's extent
    strings, and like the others it converts silently when mishandled: a
    latitude read as a longitude is a valid number in a plausible range, so the
    result is a bounding box somewhere off the coast of Africa rather than an
    error.

    So the conversion is isolated here, with a test on it.
    """
    if not boundingbox or len(boundingbox) != 4:
        raise GeocoderError(
            f"expected a 4-element boundingbox, got {boundingbox!r}"
        )
    try:
        south, north, west, east = (float(value) for value in boundingbox)
    except (TypeError, ValueError) as exc:
        raise GeocoderError(
            f"boundingbox contains non-numeric values: {boundingbox!r}"
        ) from exc
    return (west, south, east, north)


def admin_context_from_address(address: dict[str, Any] | None) -> str:
    """Build the "where is it" string REQ-9 requires, from Nominatim's address.

    The field names vary by place: a village has a ``county``, a city may have
    a ``state`` instead, and some have neither. So this walks a preference
    order and keeps whatever exists rather than assuming a fixed shape -- the
    goal is enough context to tell two same-named places apart, not a complete
    address.
    """
    if not address:
        return ""
    preferred = (
        "suburb",
        "city_district",
        "town",
        "city",
        "municipality",
        "county",
        "state_district",
        "state",
        "region",
        "country",
    )
    seen: list[str] = []
    for field_name in preferred:
        value = address.get(field_name)
        if value and value not in seen:
            seen.append(str(value))
    # The place's own name usually leads the display_name already, so the first
    # element is dropped when it duplicates the deepest administrative level.
    return ", ".join(seen)


@dataclass
class NominatimGeocoder(Geocoder):
    """Resolves place names through Nominatim."""

    cache: ResolutionCache | None = None
    timeout: float = DEFAULT_TIMEOUT
    #: Restrict results to these ISO 3166-1 alpha-2 codes, e.g. ``("gb",)``.
    country_codes: tuple[str, ...] = ()
    #: Set to refuse network access outright. The test suite uses this so a
    #: fixture gap fails as an explicit error instead of quietly going online
    #: and passing.
    offline: bool = False
    _last_request: float = field(default=0.0, repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    # ClassVar, not a dataclass field: these identify the adapter rather than
    # configure an instance. As fields their defaults would be assigned in
    # __init__ and silently overwrite a subclass's own declaration.
    name: ClassVar[str] = "nominatim"
    requires_credentials: ClassVar[bool] = False

    def resolve(self, place: str, *, limit: int = 10) -> list[PlaceCandidate]:
        query = (place or "").strip()
        if not query:
            raise GeocoderError("place name is empty")

        params: dict[str, Any] = {
            "q": query,
            "format": "jsonv2",
            "limit": str(max(1, min(limit, 40))),
            "addressdetails": "1",
            # Real boundaries rather than rectangles, which is the reason this
            # geocoder is the default rather than merely the fallback.
            "polygon_geojson": "1",
        }
        if self.country_codes:
            params["countrycodes"] = ",".join(self.country_codes)

        # Cache key excludes nothing: a lookup with a different limit or country
        # filter is a different question and gets its own entry.
        cache_params = {
            key: value for key, value in params.items() if key != "q"
        }

        payload = None
        from_cache = False
        if self.cache is not None:
            payload = self.cache.get(self.name, query, cache_params)
            from_cache = payload is not None

        if payload is None:
            if self.offline:
                raise GeocoderError(
                    f"no cached Nominatim response for {query!r} and network "
                    f"access is disabled. Record a fixture for this query, or "
                    f"allow network access."
                )
            payload = self._request(params)
            if self.cache is not None:
                self.cache.put(self.name, query, payload, cache_params)

        return self._parse(payload, from_cache=from_cache)

    def _request(self, params: dict[str, Any]) -> Any:
        """One rate-limited GET."""
        url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                # Nominatim serves English names by default; asking explicitly
                # keeps results stable regardless of the host's locale, which
                # matters because fixtures are recorded once and compared later.
                "Accept-Language": "en",
            },
        )

        with self._lock:
            gap = time.monotonic() - self._last_request
            if self._last_request and gap < MIN_REQUEST_INTERVAL:
                time.sleep(MIN_REQUEST_INTERVAL - gap)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                raise GeocoderError(
                    f"Nominatim returned HTTP {exc.code}: {exc.reason}. "
                    f"A 403 or 429 usually means the usage policy was exceeded."
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise GeocoderError(f"could not reach Nominatim: {exc}") from exc
            finally:
                self._last_request = time.monotonic()

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GeocoderError(
                f"Nominatim returned unparseable JSON: {exc}"
            ) from exc

    def _parse(self, payload: Any, *, from_cache: bool) -> list[PlaceCandidate]:
        if not isinstance(payload, list):
            raise GeocoderError(
                f"expected a JSON array from Nominatim, got "
                f"{type(payload).__name__}"
            )

        candidates: list[PlaceCandidate] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            try:
                bbox = bbox_from_nominatim(entry.get("boundingbox") or [])
                latitude = float(entry["lat"])
                longitude = float(entry["lon"])
            except (GeocoderError, KeyError, TypeError, ValueError):
                # One malformed entry does not invalidate the rest: Nominatim
                # occasionally returns a result without a bounding box, and
                # dropping it is better than failing a lookup that has four
                # other good answers.
                continue

            address = entry.get("address") or {}
            candidates.append(
                PlaceCandidate(
                    name=entry.get("name") or entry.get("display_name", ""),
                    display_name=entry.get("display_name", ""),
                    place_type=entry.get("type") or entry.get("category") or "",
                    admin_context=admin_context_from_address(address),
                    latitude=latitude,
                    longitude=longitude,
                    bbox=bbox,
                    # Nominatim's own relevance score. Comparable between
                    # results of one query; not across geocoders.
                    confidence=float(entry.get("importance") or 0.0),
                    source=self.name,
                    country_code=str(address.get("country_code") or "").lower(),
                    geometry=entry.get("geojson"),
                    extra={
                        "osm_type": entry.get("osm_type"),
                        "osm_id": entry.get("osm_id"),
                        "place_rank": entry.get("place_rank"),
                        "addresstype": entry.get("addresstype"),
                        "from_cache": from_cache,
                    },
                )
            )

        candidates.sort(key=lambda candidate: -candidate.confidence)
        return candidates


__all__ = [
    "ENDPOINT",
    "MIN_REQUEST_INTERVAL",
    "USER_AGENT",
    "NominatimGeocoder",
    "admin_context_from_address",
    "bbox_from_nominatim",
]
