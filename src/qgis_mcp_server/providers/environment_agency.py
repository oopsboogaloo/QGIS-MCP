"""Environment Agency LIDAR for England, via WCS (task 4.8, added later).

The highest-resolution elevation this server can reach, and the only provider
that returns ground truth rather than a global model: 1 m DTM from airborne
laser survey, roughly 900 times the cell count of a 30 m global DEM over the
same ground.

Three things make it different from the global DEM adapters.

**Size is the constraint, not the quota.** It is free and unmetered, which
removes the usual brake and replaces it with a worse one: at about 4 MB per
square kilometre, a town-sized AOI is hundreds of megabytes. A request that
would be absurd is refused before it is sent, with the two things that fix it
--- a smaller AOI or a coarser resolution --- named rather than implied.

**It is already in EPSG:27700.** The global DEMs arrive in 4326 and must be
reprojected, which resamples every cell. This does not, so for British work it
is both sharper and less handled. It also sidesteps the datum question
entirely: no OSGB36 transform happens, so OSTN15 does not enter into it.

**Heights are Ordnance Datum Newlyn, not EGM2008.** REQ-13 exists for exactly
this. Mixing an ODN surface with an EGM2008 one leaves an offset of up to a
metre or so that varies across the country --- small enough to look like noise,
large enough to matter in a flood or sea-level model. The datum is recorded on
every dataset so the mismatch is visible rather than discovered.

**Coverage is an envelope, not a guarantee.** The composite has genuine holes:
surveys were flown where they were needed. An AOI inside the envelope can still
return nothing, so an empty or all-nodata result is reported as a coverage gap
rather than as a failure.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_server.providers.base import Dataset, DataType, OgcAdapter
from qgis_mcp_server.providers.catalogue import EA_LIDAR_LICENCE, ENVIRONMENT_AGENCY

BASE_URL = (
    "https://environment.data.gov.uk/spatialdata/"
    "lidar-composite-digital-terrain-model-dtm-1m-2022/wcs"
)

#: Coverage ids as the live service advertises them.
ELEVATION_COVERAGE = (
    "13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m"
)
HILLSHADE_COVERAGE = (
    "13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Hillshade_DTM_1m"
)

USER_AGENT = "qgis-mcp/0.1 (QGIS MCP server; https://github.com/oopsboogaloo/QGIS-MCP)"

DEFAULT_TIMEOUT = 300.0

WCS_VERSION = "2.0.1"

#: The CRS the AOI is expressed in when subsetting. The server projects it
#: into the coverage's native EPSG:27700, which keeps PROJ out of the
#: server process entirely.
SUBSETTING_CRS = "http://www.opengis.net/def/crs/EPSG/0/4326"

#: Native resolution of the composite, in metres.
NATIVE_RESOLUTION_M = 1.0

#: Measured, not estimated: a 1 km x 1 km request at 1 m came back as
#: 4,194,755 bytes of Float32 GeoTIFF. Four bytes a cell plus a header, which
#: is what it should be --- the figure is here so the size gate below is
#: arithmetic rather than a guess.
BYTES_PER_CELL = 4

#: Largest download this adapter will issue without being told to.
#:
#: 250 MB is already an awkward raster to open; past it, QGIS is slow and the
#: user almost always wanted a coarser grid or a smaller area. Refused before
#: the request so nothing is half-downloaded.
MAX_DOWNLOAD_BYTES = 250_000_000

#: Resolutions offered, finest first. The service resamples server-side, so a
#: coarser request is a smaller download rather than the same download
#: decimated afterwards.
RESOLUTIONS_M = (1.0, 2.0, 5.0, 10.0, 25.0)

#: Vertical datum of the composite (REQ-13).
VERTICAL_DATUM = "Ordnance Datum Newlyn (ODN)"


class EaLidarError(Exception):
    """An Environment Agency LIDAR request failed."""


@dataclass(frozen=True)
class LidarProduct:
    """One coverage offered by the service."""

    identifier: str
    coverage_id: str
    title: str
    is_elevation: bool
    notes: str = ""


PRODUCTS = (
    LidarProduct(
        identifier="ea_lidar_dtm_1m",
        coverage_id=ELEVATION_COVERAGE,
        title="EA LIDAR composite DTM, 1 m (England)",
        is_elevation=True,
        notes=(
            "Bare-earth terrain: buildings and vegetation removed. Float32 "
            "metres above Ordnance Datum Newlyn."
        ),
    ),
    LidarProduct(
        identifier="ea_lidar_hillshade_1m",
        coverage_id=HILLSHADE_COVERAGE,
        title="EA LIDAR composite hillshade, 1 m (England)",
        is_elevation=False,
        notes=(
            "A rendered relief image rather than elevation values. Useful as a "
            "backdrop; useless as a heightmap --- use the DTM for anything "
            "that needs numbers."
        ),
    ),
)

PRODUCTS_BY_IDENTIFIER = {product.identifier: product for product in PRODUCTS}


def aoi_bng_span(aoi: tuple[float, float, float, float]) -> tuple[float, float]:
    """Approximate AOI width and height in metres, for sizing only.

    A flat approximation with a latitude correction. Sizing does not need a
    projection: being 1% out on an estimate used to choose a resolution and
    refuse an absurd request changes nothing, and a real transform here would
    make this module depend on PROJ for no benefit.
    """
    import math

    min_lon, min_lat, max_lon, max_lat = aoi
    mid_lat = math.radians((min_lat + max_lat) / 2)
    width = abs(max_lon - min_lon) * 111_320 * math.cos(mid_lat)
    height = abs(max_lat - min_lat) * 110_570
    return width, height


def estimate_bytes(
    aoi: tuple[float, float, float, float], resolution_m: float
) -> int:
    """Download size for an AOI at a resolution."""
    width, height = aoi_bng_span(aoi)
    cells = (width / resolution_m) * (height / resolution_m)
    return int(cells * BYTES_PER_CELL)


def choose_resolution(
    aoi: tuple[float, float, float, float],
    *,
    budget_bytes: int = MAX_DOWNLOAD_BYTES,
) -> float | None:
    """The finest resolution whose download fits ``budget_bytes``.

    Returns ``None`` when even the coarsest offered does not fit, which is the
    adapter's way of saying this AOI is the wrong shape of request rather than
    a failed one.
    """
    for resolution in RESOLUTIONS_M:
        if estimate_bytes(aoi, resolution) <= budget_bytes:
            return resolution
    return None


class EnvironmentAgencyAdapter(OgcAdapter):
    """EA LIDAR composite, through WCS 2.0.1."""

    capability = ENVIRONMENT_AGENCY

    def __init__(
        self,
        credentials=None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> None:
        super().__init__(credentials)
        self.base_url = base_url
        self.timeout = timeout
        self.max_download_bytes = max_download_bytes

    # -- search ------------------------------------------------------------

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """Products available for ``aoi``. No network call.

        The service has no per-AOI catalogue to query, so the useful work is
        the sizing: which resolution fits, what it will cost, and whether the
        request is reasonable at all.
        """
        if data_types and DataType.ELEVATION not in data_types:
            return []
        if not self.capability.covers(aoi):
            return []

        width, height = aoi_bng_span(aoi)
        area_km2 = (width * height) / 1_000_000
        resolution = choose_resolution(aoi, budget_bytes=self.max_download_bytes)

        datasets = []
        for product in PRODUCTS:
            chosen = resolution or RESOLUTIONS_M[-1]
            extra: dict[str, Any] = {
                "coverage_id": product.coverage_id,
                "resolution_m": chosen,
                "native_resolution_m": NATIVE_RESOLUTION_M,
                "aoi_area_km2": round(area_km2, 2),
                "available_resolutions_m": list(RESOLUTIONS_M),
                "notes": product.notes,
                "coverage_caveat": (
                    "the composite is an envelope with real holes --- surveys "
                    "were flown where they were needed, so an AOI inside "
                    "England can still return nothing"
                ),
            }
            if product.is_elevation:
                # REQ-13: recorded on the dataset, because mixing this with an
                # EGM2008 global DEM leaves an offset that looks like noise.
                extra["vertical_datum"] = VERTICAL_DATUM
                extra["vertical_datum_warning"] = (
                    "heights are ODN; the global DEMs (COP30, SRTM, NASADEM) "
                    "are EGM2008. Combining them without conversion leaves an "
                    "offset of up to about a metre that varies across the "
                    "country."
                )
            if resolution is None:
                extra["too_large"] = (
                    f"even at {RESOLUTIONS_M[-1]:g} m this AOI is about "
                    f"{estimate_bytes(aoi, RESOLUTIONS_M[-1]) / 1e6:.0f} MB, "
                    f"over the {self.max_download_bytes / 1e6:.0f} MB limit. "
                    f"Use a smaller AOI, or a global DEM for an area this size."
                )
            elif resolution > NATIVE_RESOLUTION_M:
                extra["resolution_note"] = (
                    f"{chosen:g} m rather than the native "
                    f"{NATIVE_RESOLUTION_M:g} m: at native resolution this AOI "
                    f"would be about "
                    f"{estimate_bytes(aoi, NATIVE_RESOLUTION_M) / 1e6:.0f} MB. "
                    f"The server resamples, so this is a smaller download "
                    f"rather than a large one thrown away."
                )

            datasets.append(
                Dataset(
                    provider=self.name,
                    identifier=product.identifier,
                    title=product.title,
                    data_type=DataType.ELEVATION,
                    licence=EA_LIDAR_LICENCE,
                    # Native. Nothing is reprojected, which is half the point.
                    crs="EPSG:27700",
                    resolution_m=chosen,
                    estimated_size_bytes=estimate_bytes(aoi, chosen),
                    format="GTiff",
                    attribution=EA_LIDAR_LICENCE.attribution,
                    # Ahead of every global DEM inside England: 1 m of surveyed
                    # ground beats 30 m of modelled satellite, and it needs no
                    # reprojection to get there.
                    preference=0 if product.is_elevation else 3,
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
        """GetCoverage for ``dataset`` over ``aoi``, as a GeoTIFF."""
        product = PRODUCTS_BY_IDENTIFIER.get(dataset.identifier)
        if product is None:
            raise EaLidarError(
                f"{dataset.identifier!r} is not an EA LIDAR product. Known: "
                f"{', '.join(sorted(PRODUCTS_BY_IDENTIFIER))}"
            )

        resolution = float(
            (dataset.extra or {}).get("resolution_m") or NATIVE_RESOLUTION_M
        )
        estimated = estimate_bytes(aoi, resolution)
        if estimated > self.max_download_bytes:
            raise EaLidarError(
                f"this request is about {estimated / 1e6:.0f} MB at "
                f"{resolution:g} m, over the {self.max_download_bytes / 1e6:.0f} "
                f"MB limit. Refused before sending, so nothing is "
                f"half-downloaded. Use a smaller AOI or a coarser resolution."
            )

        min_lon, min_lat, max_lon, max_lat = aoi
        # The AOI is sent in 4326 and the *server* projects it. The coverage is
        # published in EPSG:27700 with E/N axes, so the obvious implementation
        # converts locally -- but that would mean a PROJ transform inside the
        # server process, which has no PyQGIS and no pyproj by design
        # (design.md section 2.2).
        #
        # subsettingCrs with the named axes Long and Lat avoids both problems,
        # and removes an axis-order trap as well: the axes are named, so there
        # is no positional order to get backwards the way WMS 1.3.0's
        # EPSG:4326 bbox has. The response still arrives in EPSG:27700.
        url = f"{self.base_url}?" + urllib.parse.urlencode(
            {
                "service": "WCS",
                "version": WCS_VERSION,
                "request": "GetCoverage",
                "coverageId": product.coverage_id,
                "format": "image/tiff",
                "subsettingCrs": SUBSETTING_CRS,
            }
        ) + (
            f"&subset=Long({min_lon},{max_lon})"
            f"&subset=Lat({min_lat},{max_lat})"
        )
        if resolution != NATIVE_RESOLUTION_M:
            # Server-side resampling: a smaller download, not a big one
            # thinned afterwards.
            url += (
                f"&scaleaxes=E({NATIVE_RESOLUTION_M / resolution}),"
                f"N({NATIVE_RESOLUTION_M / resolution})"
            )

        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise EaLidarError(
                f"the Environment Agency refused the request (HTTP {exc.code}): "
                f"{exc.read()[:300].decode('utf-8', errors='replace')}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EaLidarError(f"could not reach the Environment Agency: {exc}") from exc

        # WCS reports failures as XML, often with a 200. Written to a .tif it
        # becomes a file GDAL rejects much later, with a message about the
        # file rather than about the request.
        if body[:2] not in (b"II", b"MM"):
            snippet = body[:400].decode("utf-8", errors="replace")
            raise EaLidarError(
                f"the service returned {len(body)} bytes that are not a TIFF. "
                f"This is usually an AOI outside the surveyed area. {snippet}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return destination


__all__ = [
    "BASE_URL",
    "BYTES_PER_CELL",
    "MAX_DOWNLOAD_BYTES",
    "NATIVE_RESOLUTION_M",
    "PRODUCTS",
    "PRODUCTS_BY_IDENTIFIER",
    "RESOLUTIONS_M",
    "SUBSETTING_CRS",
    "VERTICAL_DATUM",
    "EaLidarError",
    "EnvironmentAgencyAdapter",
    "LidarProduct",
    "aoi_bng_span",
    "choose_resolution",
    "estimate_bytes",
]
