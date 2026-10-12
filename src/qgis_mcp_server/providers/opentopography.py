"""The OpenTopography adapter (task 4.4).

``tasks.md`` calls this "the natural first: closest to config-driven, and serves
the highest-priority data type". The reason it is config-driven is structural:
OpenTopography's global catalogue is a **fixed list** of DEM types, each with a
known resolution, coverage and per-request area limit. So ``search`` is
filtering a static manifest and makes no network call at all; only ``fetch``
touches the service.

**Vertical datums are declared per dataset, and that is load-bearing.** REQ-13
requires a DEM's vertical datum to be recorded as layer metadata, and the
reasoning is quantitative: sea-level modelling works in single-metre
increments, so stray ellipsoidal heights would be wrong by roughly 50 m in the
UK. OpenTopography serves both kinds -- ``SRTMGL1`` is EGM96 orthometric and
``SRTMGL1_E`` is WGS 84 ellipsoidal, differing by nothing in the name but the
suffix. Declaring the datum here is what lets REQ-13's warning fire at all.

Verified against the live API in October 2026: the endpoint takes ``demtype``,
``south``/``north``/``west``/``east``, ``outputFormat`` and ``API_Key``, and
returns raw GeoTIFF bytes. A rejected request is an HTTP 400 carrying an XML
``<error>`` element whose text names the problem -- including, for an
over-limit area, both the limit and the computed area.

Standard library only.
"""

from __future__ import annotations

import math
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_server.credentials import CredentialStore
from qgis_mcp_server.providers.base import (
    Dataset,
    DataType,
    RestApiAdapter,
)
from qgis_mcp_server.providers.catalogue import OPENTOPOGRAPHY, OPENTOPOGRAPHY_LICENCE

ENDPOINT = "https://portal.opentopography.org/API/globaldem"

USER_AGENT = (
    "qgis-mcp/0.1 (QGIS MCP server; +https://github.com/oopsboogaloo/QGIS-MCP)"
)

DEFAULT_TIMEOUT = 300.0

#: Mean Earth radius, for the area estimate the pre-flight check uses.
EARTH_RADIUS_KM = 6371.0


class VerticalDatum:
    """Vertical datums OpenTopography's products use (REQ-13).

    Named rather than free text so a mismatch can be detected by comparison
    instead of by string-matching prose. ``UNKNOWN`` is a real value, not a
    placeholder: REQ-13 requires a warning when a datum is unknown, so a
    dataset whose datum has not been confirmed must say so rather than guess.
    """

    EGM96 = "EGM96"
    EGM2008 = "EGM2008"
    WGS84_ELLIPSOID = "WGS84 ellipsoid"
    MEAN_SEA_LEVEL = "approximate mean sea level"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GlobalDem:
    """One entry in OpenTopography's fixed global catalogue."""

    demtype: str
    title: str
    resolution_m: float
    #: Per-request cap in km2, from the API documentation. Enforced before the
    #: request so an over-large AOI does not spend one of a small daily
    #: allowance to be told no.
    max_area_km2: float
    #: Latitude coverage. SRTM stops at 60N/56S, which silently returns nothing
    #: further north -- a real trap for Scandinavian or Arctic work.
    min_latitude: float
    max_latitude: float
    vertical_datum: str
    #: Tie-break among datasets of equal resolution, lower first.
    #:
    #: The leading entry in a menu is what gets chosen, so this is not
    #: cosmetic. Two things it encodes: Copernicus leads its resolution band
    #: because it is truly global and current, and the ellipsoidal variants
    #: rank last because leading with one would hand a caller heights roughly
    #: 50 m off over the UK for no stated reason (REQ-13).
    preference: int = 1
    notes: str = ""

    @property
    def coverage(self) -> tuple[float, float, float, float]:
        return (-180.0, self.min_latitude, 180.0, self.max_latitude)

    def covers(self, aoi: tuple[float, float, float, float]) -> bool:
        """Whether this dataset's latitude band reaches the AOI."""
        _, min_lat, _, max_lat = aoi
        return not (max_lat < self.min_latitude or min_lat > self.max_latitude)


#: The terrain-relevant global products.
#:
#: GEDI L3 and the GEBCO products are deliberately absent: they are canopy and
#: bathymetry grids at 450 m to 1 km, which serve neither the terrain-render
#: pipeline nor the sea-level modelling this project needs. Adding one is a
#: tuple entry when a use case appears.
GLOBAL_DEMS: tuple[GlobalDem, ...] = (
    GlobalDem(
        demtype="COP30",
        preference=0,
        title="Copernicus GLO-30",
        resolution_m=30.0,
        max_area_km2=450_000.0,
        min_latitude=-90.0,
        max_latitude=90.0,
        vertical_datum=VerticalDatum.EGM2008,
        notes=(
            "Truly global and generally the best 30 m choice outside countries "
            "with national LiDAR. Preferred default."
        ),
    ),
    GlobalDem(
        demtype="COP90",
        preference=0,
        title="Copernicus GLO-90",
        resolution_m=90.0,
        max_area_km2=4_050_000.0,
        min_latitude=-90.0,
        max_latitude=90.0,
        vertical_datum=VerticalDatum.EGM2008,
        notes="The 90 m Copernicus product; nine times the area allowance.",
    ),
    GlobalDem(
        demtype="SRTMGL1",
        title="SRTM GL1 (30 m)",
        resolution_m=30.0,
        max_area_km2=450_000.0,
        min_latitude=-56.0,
        max_latitude=60.0,
        vertical_datum=VerticalDatum.EGM96,
        notes="Does not cover above 60 degrees N or below 56 degrees S.",
    ),
    GlobalDem(
        demtype="SRTMGL1_E",
        preference=9,
        title="SRTM GL1 Ellipsoidal (30 m)",
        resolution_m=30.0,
        max_area_km2=450_000.0,
        min_latitude=-56.0,
        max_latitude=60.0,
        vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        notes=(
            "Ellipsoidal heights, NOT orthometric. In the UK these sit roughly "
            "50 m above EGM96 values, so mixing this with any other product -- "
            "or using it for sea-level work -- needs a datum conversion first "
            "(REQ-13 warns, it does not convert)."
        ),
    ),
    GlobalDem(
        demtype="SRTMGL3",
        title="SRTM GL3 (90 m)",
        resolution_m=90.0,
        max_area_km2=4_050_000.0,
        min_latitude=-56.0,
        max_latitude=60.0,
        vertical_datum=VerticalDatum.EGM96,
        notes="Does not cover above 60 degrees N or below 56 degrees S.",
    ),
    GlobalDem(
        demtype="AW3D30",
        title="ALOS World 3D (30 m)",
        resolution_m=30.0,
        max_area_km2=450_000.0,
        min_latitude=-82.0,
        max_latitude=82.0,
        vertical_datum=VerticalDatum.EGM96,
        notes="Wider latitude coverage than SRTM; useful above 60 degrees N.",
    ),
    GlobalDem(
        demtype="AW3D30_E",
        preference=9,
        title="ALOS World 3D Ellipsoidal (30 m)",
        resolution_m=30.0,
        max_area_km2=450_000.0,
        min_latitude=-82.0,
        max_latitude=82.0,
        vertical_datum=VerticalDatum.WGS84_ELLIPSOID,
        notes="Ellipsoidal heights -- see the SRTMGL1_E note.",
    ),
    GlobalDem(
        demtype="NASADEM",
        title="NASADEM (30 m)",
        resolution_m=30.0,
        max_area_km2=450_000.0,
        min_latitude=-56.0,
        max_latitude=60.0,
        vertical_datum=VerticalDatum.EGM96,
        notes="A reprocessing of SRTM; same latitude limits.",
    ),
    GlobalDem(
        demtype="SRTM15Plus",
        title="SRTM15+ V2.1 (450 m, with bathymetry)",
        resolution_m=450.0,
        max_area_km2=125_000_000.0,
        min_latitude=-90.0,
        max_latitude=90.0,
        vertical_datum=VerticalDatum.MEAN_SEA_LEVEL,
        notes=(
            "Includes bathymetry, so the only product here that describes the "
            "sea floor. Far too coarse for terrain rendering."
        ),
    ),
)

DEMS_BY_TYPE = {dem.demtype: dem for dem in GLOBAL_DEMS}

#: Output formats the API accepts. GTiff is the only one worth defaulting to:
#: it carries georeferencing, which AAIGrid does only via a sidecar.
OUTPUT_FORMATS = ("GTiff", "AAIGrid", "HFA")

#: Extracts the message from the API's XML error body.
_ERROR_PATTERN = re.compile(r"<error>(.*?)</error>", re.DOTALL)


class OpenTopographyError(Exception):
    """The service refused a request or returned something unusable."""


def aoi_area_km2(aoi: tuple[float, float, float, float]) -> float:
    """Approximate area of a geographic bounding box, in square kilometres.

    Spherical, which is accurate to well under a percent at any size -- checked
    against the API's own figure, which reported 435,382,432 km2 for a box this
    formula puts at 434.9 million.

    The API remains authoritative. This exists so an obviously over-large
    request is refused locally rather than spending one of a 50-per-day
    allowance to be told the same thing.
    """
    min_lon, min_lat, max_lon, max_lat = aoi
    lon_span = math.radians(abs(max_lon - min_lon))
    sin_delta = abs(math.sin(math.radians(max_lat)) - math.sin(math.radians(min_lat)))
    return (EARTH_RADIUS_KM**2) * sin_delta * lon_span


def extract_api_error(body: bytes | str) -> str:
    """Pull the readable message out of an API error body.

    The service answers a bad request with HTTP 400 and
    ``<error>Error: ...</error>``. That text is far more useful than the status
    code -- for an over-limit area it names both the limit and the computed
    area -- so it is surfaced rather than replaced with wording of our own.
    """
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    match = _ERROR_PATTERN.search(text)
    if match:
        return " ".join(match.group(1).split())
    return " ".join(text.split())[:500] or "(no message)"


class OpenTopographyAdapter(RestApiAdapter):
    """Global elevation from OpenTopography."""

    def __init__(
        self,
        credentials: CredentialStore | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        output_format: str = "GTiff",
    ) -> None:
        super().__init__(credentials)
        self.capability = OPENTOPOGRAPHY
        self.timeout = timeout
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(
                f"output_format must be one of {OUTPUT_FORMATS}; got "
                f"{output_format!r}"
            )
        self.output_format = output_format

    # -- search: no network -----------------------------------------------

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """Datasets available for ``aoi``, from the static catalogue.

        Makes no request. The catalogue is fixed, so availability is decided by
        latitude coverage and the per-request area limit -- both known locally.
        """
        if data_types and DataType.ELEVATION not in data_types:
            return []

        area = aoi_area_km2(aoi)
        datasets: list[Dataset] = []
        for dem in GLOBAL_DEMS:
            if not dem.covers(aoi):
                continue
            if area > dem.max_area_km2:
                continue
            datasets.append(self._as_dataset(dem, aoi, area))

        # Finest resolution first -- the usual question is "what is the best
        # elevation I can get here" -- then by preference, so the leading entry
        # is one it is safe to take without reading the notes.
        datasets.sort(
            key=lambda dataset: (
                dataset.resolution_m or 0,
                dataset.extra.get("preference", 1),
                dataset.identifier,
            )
        )
        return datasets

    def unavailable_for(
        self, aoi: tuple[float, float, float, float]
    ) -> list[dict[str, Any]]:
        """Catalogue entries that cannot serve ``aoi``, and why.

        Reported rather than silently omitted. A caller with a large AOI who
        sees only 90 m products should be told that the 30 m ones were excluded
        by the area limit, not left to infer that none exist -- the remedy
        (split the AOI) is only obvious once the reason is stated.
        """
        area = aoi_area_km2(aoi)
        excluded: list[dict[str, Any]] = []
        for dem in GLOBAL_DEMS:
            if not dem.covers(aoi):
                excluded.append(
                    {
                        "identifier": dem.demtype,
                        "title": dem.title,
                        "reason": (
                            f"covers latitudes {dem.min_latitude} to "
                            f"{dem.max_latitude}, which does not reach this AOI"
                        ),
                    }
                )
            elif area > dem.max_area_km2:
                excluded.append(
                    {
                        "identifier": dem.demtype,
                        "title": dem.title,
                        "reason": (
                            f"the AOI is about {area:,.0f} km2 and this dataset "
                            f"allows {dem.max_area_km2:,.0f} km2 per request"
                        ),
                        "remedy": "split the AOI, or use a coarser dataset",
                    }
                )
        return excluded

    def _as_dataset(
        self, dem: GlobalDem, aoi: tuple[float, float, float, float], area: float
    ) -> Dataset:
        return Dataset(
            provider=self.capability.name,
            identifier=dem.demtype,
            title=dem.title,
            data_type=DataType.ELEVATION,
            licence=OPENTOPOGRAPHY_LICENCE,
            # Served in 4326 without exception, which is why REQ-12 prefers
            # leaving a higher-resolution national DEM unresampled instead.
            crs="EPSG:4326",
            resolution_m=dem.resolution_m,
            estimated_size_bytes=self._estimate_size(dem, aoi),
            format=self.output_format,
            attribution=OPENTOPOGRAPHY_LICENCE.attribution,
            extra={
                # REQ-13: recorded at acquisition so a later comparison or a
                # sea-level run can warn on a mismatch.
                "vertical_datum": dem.vertical_datum,
                "preference": dem.preference,
                "max_area_km2": dem.max_area_km2,
                "aoi_area_km2": round(area, 1),
                "latitude_coverage": [dem.min_latitude, dem.max_latitude],
                "notes": dem.notes,
            },
        )

    def _estimate_size(
        self, dem: GlobalDem, aoi: tuple[float, float, float, float]
    ) -> int:
        """Rough download size, for REQ-4d's confirmation.

        REQ-4d requires the confirmation to state approximate size, and an
        order of magnitude is what actually informs a decision -- the
        difference between a 2 MB and a 2 GB fetch. Float32 pixels, with a
        generous allowance for compression and overhead.
        """
        min_lon, min_lat, max_lon, max_lat = aoi
        metres_per_degree = 111_320.0
        width = abs(max_lon - min_lon) * metres_per_degree * math.cos(
            math.radians((min_lat + max_lat) / 2)
        )
        height = abs(max_lat - min_lat) * metres_per_degree
        pixels = (width / dem.resolution_m) * (height / dem.resolution_m)
        return max(1024, int(pixels * 4 * 0.6))

    # -- fetch: the one network call --------------------------------------

    def build_url(
        self,
        demtype: str,
        aoi: tuple[float, float, float, float],
        *,
        api_key: str,
    ) -> str:
        """The request URL. Separated so it can be tested without a request."""
        min_lon, min_lat, max_lon, max_lat = aoi
        params = {
            "demtype": demtype,
            "south": min_lat,
            "north": max_lat,
            "west": min_lon,
            "east": max_lon,
            "outputFormat": self.output_format,
            "API_Key": api_key,
        }
        return f"{ENDPOINT}?{urllib.parse.urlencode(params)}"

    def fetch(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        destination: Path,
    ) -> Path:
        """Download ``dataset`` for ``aoi`` to ``destination``.

        Raises :class:`OpenTopographyError` rather than writing a file that is
        not elevation data. The checks below exist because a half-written or
        wrong-content file with a ``.tif`` extension is the worst outcome: it
        propagates into a project and fails somewhere else entirely.
        """
        dem = DEMS_BY_TYPE.get(dataset.identifier)
        if dem is None:
            raise OpenTopographyError(
                f"{dataset.identifier!r} is not an OpenTopography global "
                f"dataset. Known: {', '.join(sorted(DEMS_BY_TYPE))}"
            )

        if not dem.covers(aoi):
            raise OpenTopographyError(
                f"{dem.demtype} covers latitudes {dem.min_latitude} to "
                f"{dem.max_latitude} and the AOI falls outside that. "
                f"COP30 and COP90 are global."
            )

        area = aoi_area_km2(aoi)
        if area > dem.max_area_km2:
            raise OpenTopographyError(
                f"the AOI is about {area:,.0f} km2 and {dem.demtype} allows "
                f"{dem.max_area_km2:,.0f} km2 per request. Split the AOI, or "
                f"use a coarser dataset. Refused locally so the request does "
                f"not count against the daily allowance."
            )

        status = self.credential_status()
        if status is None or not status.present:
            raise OpenTopographyError(
                f"no OpenTopography API key is configured. Register at "
                f"{self.capability.credential.registration_url} and set "
                f"{self.capability.credential.env_var}."
            )
        api_key = self.credentials.get(self.capability.credential)

        url = self.build_url(dem.demtype, aoi, api_key=api_key)
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT}
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # The XML <error> text is the useful part -- for an over-limit area
            # it names both the limit and the computed area. Surfaced as-is.
            raise OpenTopographyError(
                f"OpenTopography refused the request (HTTP {exc.code}): "
                f"{extract_api_error(exc.read())}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenTopographyError(
                f"could not reach OpenTopography: {exc}"
            ) from exc

        self._validate_payload(body, dem)

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Written via a temporary name and moved into place, so an interrupted
        # download cannot leave a truncated raster that later looks loadable.
        staging = destination.with_name(destination.name + ".part")
        staging.write_bytes(body)
        staging.replace(destination)
        return destination

    def _validate_payload(self, body: bytes, dem: GlobalDem) -> None:
        """Refuse anything that is not the raster we asked for.

        The API answers errors with HTTP 400, so this should never fire against
        the service as it behaves today. It is here because the alternative
        failure is silent: a proxy error page or a changed API convention would
        otherwise be written out with a ``.tif`` extension and fail much later,
        somewhere that cannot explain why.
        """
        if not body:
            raise OpenTopographyError(
                f"{dem.demtype} returned an empty response"
            )
        if b"<error>" in body[:2048] or body.lstrip()[:5] == b"<?xml":
            raise OpenTopographyError(
                f"OpenTopography returned an error document rather than "
                f"raster data: {extract_api_error(body[:2048])}"
            )
        if self.output_format == "GTiff" and body[:2] not in (b"II", b"MM"):
            raise OpenTopographyError(
                f"expected a GeoTIFF from {dem.demtype} but the response does "
                f"not start with a TIFF byte-order marker (got "
                f"{body[:8]!r}). Refusing to write it as a .tif."
            )


__all__ = [
    "DEFAULT_TIMEOUT",
    "DEMS_BY_TYPE",
    "ENDPOINT",
    "GLOBAL_DEMS",
    "OUTPUT_FORMATS",
    "GlobalDem",
    "OpenTopographyAdapter",
    "OpenTopographyError",
    "VerticalDatum",
    "aoi_area_km2",
    "extract_api_error",
]
