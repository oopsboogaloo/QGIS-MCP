"""British Geological Survey, via WMS (task 4.8).

The one adapter here that returns a rendered image rather than data. BGS
publishes DiGMap as OGC WMS; there is no public WFS for the free tier (the
WFS endpoint answers with a MapServer error page), so what comes back is a
picture of the geology, not polygons of it. That is a real limitation and the
adapter states it rather than letting a caller discover it when they try to
query an attribute.

**The free tier is 1:625,000, and the distinction is licensing, not detail.**
BGS also publishes ``BGS_Detailed_Geology`` at 1:50,000 on a different host.
That one is the commercially licensed product, and this adapter deliberately
does not offer it: a provider that silently handed over licensed data under an
open-licence banner would be worse than one that offered nothing. The 625k
service states its own terms --- "made available for all uses - including
commercial use" --- and those are read from the live capabilities rather than
asserted here, so they cannot drift out of date in a constant.

**CRS:84 rather than EPSG:4326, deliberately.** In WMS 1.3.0, ``EPSG:4326``
means latitude first; ``CRS:84`` means longitude first. Both are advertised by
this service, and choosing the wrong one silently returns an image of
somewhere else --- the transposition produces a valid request for a valid
place, just not the one asked for.

**The image is georeferenced by us, not by the server.** MapServer's
``image/tiff`` is a plain TIFF with no geotags, so a world file and a
projection file are written alongside it from the bbox and pixel size we chose.
Those are known exactly, which makes this more reliable than hoping the server
embeds them.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_server.providers.base import Dataset, DataType, OgcAdapter
from qgis_mcp_server.providers.catalogue import BGS, BGS_LICENCE

#: The 1:625,000 DiGMap service: the tier BGS makes available for all uses.
BASE_URL = "https://ogc.bgs.ac.uk/cgi-bin/BGS_Bedrock_and_Superficial_Geology/wms"

USER_AGENT = "qgis-mcp/0.1 (QGIS MCP server; https://github.com/oopsboogaloo/QGIS-MCP)"

DEFAULT_TIMEOUT = 120.0

WMS_VERSION = "1.3.0"

#: Longitude-first, unambiguously. See the module docstring.
REQUEST_CRS = "CRS:84"

#: GeoTIFF out, so the result lands in the raster pipeline unchanged.
IMAGE_FORMAT = "image/tiff"

#: Largest image edge requested, in pixels.
#:
#: 1:625,000 source data carries no detail below roughly 150 m on the ground,
#: so a larger raster would be empty magnification --- and WMS servers refuse
#: oversized requests anyway.
MAX_PIXELS = 2048

#: Smallest, so a tiny AOI still produces a usable image.
MIN_PIXELS = 64

#: What this service cannot do, stated where a caller will read it.
RASTER_ONLY_NOTE = (
    "WMS returns a rendered image, not features: the result can be displayed "
    "and sampled by colour, but not queried by attribute. BGS publishes no "
    "public WFS for the free tier, so vector geology needs a BGS licence "
    "agreement."
)


class BgsError(Exception):
    """A BGS request failed."""


@dataclass(frozen=True)
class BgsLayer:
    """One WMS layer, as advertised by GetCapabilities."""

    name: str
    title: str
    abstract: str = ""


def _text(element, path: str, namespaces: dict[str, str]) -> str:
    found = element.findtext(path, namespaces=namespaces)
    return (found or "").strip()


def parse_capabilities(xml: bytes) -> tuple[list[BgsLayer], dict[str, Any]]:
    """Layers and service terms from a WMS 1.3.0 capabilities document.

    The access constraints are pulled out because they are the licence, from
    the authority, as of today. A constant in this file would be a copy that
    silently goes stale --- and for the one provider here whose free tier is
    bounded by scale, a stale licence statement is the worst kind to hold.
    """
    namespaces = {"wms": "http://www.opengis.net/wms"}
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise BgsError(f"BGS sent a capabilities document that would not parse: {exc}")

    service = root.find(".//wms:Service", namespaces)
    terms: dict[str, Any] = {}
    if service is not None:
        terms = {
            "service_title": _text(service, "wms:Title", namespaces),
            "fees": _text(service, "wms:Fees", namespaces),
            "access_constraints": _text(service, "wms:AccessConstraints", namespaces),
        }

    layers = []
    for element in root.findall(".//wms:Layer/wms:Layer", namespaces):
        name = _text(element, "wms:Name", namespaces)
        if not name:
            # A grouping layer with no name cannot be requested.
            continue
        layers.append(
            BgsLayer(
                name=name,
                title=_text(element, "wms:Title", namespaces) or name,
                abstract=_text(element, "wms:Abstract", namespaces),
            )
        )
    return layers, terms


def pixel_size(
    aoi: tuple[float, float, float, float],
    *,
    max_pixels: int = MAX_PIXELS,
    min_pixels: int = MIN_PIXELS,
) -> tuple[int, int]:
    """Image dimensions for an AOI, preserving its aspect ratio.

    Aspect ratio matters: a square image of a wide AOI is stretched geology,
    which looks plausible and is wrong everywhere. The same mistake, in the
    other direction, produced a 12995x13000 raster earlier in this project.
    """
    min_lon, min_lat, max_lon, max_lat = aoi
    width = abs(max_lon - min_lon)
    height = abs(max_lat - min_lat)
    if width <= 0 or height <= 0:
        raise BgsError(f"the AOI {aoi} has no area")

    if width >= height:
        pixels_x = max_pixels
        pixels_y = max(min_pixels, round(max_pixels * height / width))
    else:
        pixels_y = max_pixels
        pixels_x = max(min_pixels, round(max_pixels * width / height))
    return int(pixels_x), int(pixels_y)


def world_file(
    aoi: tuple[float, float, float, float], width: int, height: int
) -> str:
    """An ESRI world file for the requested image.

    Six lines: x pixel size, rotation, rotation, y pixel size, and the centre
    of the top-left pixel. The y size is negative because image rows run south
    as the coordinate runs north, and the half-pixel offset is what the format
    specifies --- both are the usual places a hand-written world file lands the
    raster half a pixel out, or upside down.
    """
    min_lon, min_lat, max_lon, max_lat = aoi
    x_size = (max_lon - min_lon) / width
    y_size = -(max_lat - min_lat) / height
    return "\n".join(
        [
            f"{x_size:.12f}",
            "0.0",
            "0.0",
            f"{y_size:.12f}",
            f"{min_lon + x_size / 2:.12f}",
            f"{max_lat + y_size / 2:.12f}",
            "",
        ]
    )


class BgsAdapter(OgcAdapter):
    """BGS 1:625,000 geology, as georeferenced images."""

    capability = BGS

    def __init__(
        self,
        credentials=None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_pixels: int = MAX_PIXELS,
    ) -> None:
        super().__init__(credentials)
        self.base_url = base_url
        self.timeout = timeout
        self.max_pixels = max_pixels
        self._layers: list[BgsLayer] | None = None
        self._terms: dict[str, Any] = {}

    # -- capabilities ------------------------------------------------------

    def _get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise BgsError(
                f"BGS refused the request (HTTP {exc.code}): "
                f"{exc.read()[:300].decode('utf-8', errors='replace')}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BgsError(f"could not reach BGS: {exc}") from exc

    def layers(self) -> list[BgsLayer]:
        """Advertised layers, fetched once per adapter."""
        if self._layers is None:
            url = f"{self.base_url}?" + urllib.parse.urlencode(
                {
                    "service": "WMS",
                    "request": "GetCapabilities",
                    "version": WMS_VERSION,
                }
            )
            self._layers, self._terms = parse_capabilities(self._get(url))
        return self._layers

    # -- search ------------------------------------------------------------

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """The geology layers covering ``aoi``."""
        if data_types and DataType.GEOLOGY not in data_types:
            return []
        if not self.capability.covers(aoi):
            return []

        available = self.layers()
        width, height = pixel_size(aoi, max_pixels=self.max_pixels)

        datasets = []
        for layer in available:
            extra: dict[str, Any] = {
                "wms_layer": layer.name,
                "image_size_px": [width, height],
                "request_crs": REQUEST_CRS,
                "raster_only": RASTER_ONLY_NOTE,
                "scale_note": (
                    "1:625,000. BGS publishes 1:50,000 DiGMap as well, but that "
                    "tier needs a BGS licence agreement and is not offered here."
                ),
            }
            if layer.abstract:
                extra["abstract"] = layer.abstract
            # Terms as the service states them today, not as this file
            # remembers them.
            extra.update(
                {key: value for key, value in self._terms.items() if value}
            )

            datasets.append(
                Dataset(
                    provider=self.name,
                    identifier=layer.name,
                    title=layer.title,
                    data_type=DataType.GEOLOGY,
                    licence=BGS_LICENCE,
                    crs="EPSG:4326",
                    estimated_size_bytes=width * height * 3,
                    format="GTiff",
                    attribution=BGS_LICENCE.attribution,
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
        """GetMap for ``dataset`` over ``aoi``, written with georeferencing."""
        known = {layer.name for layer in self.layers()}
        if dataset.identifier not in known:
            raise BgsError(
                f"{dataset.identifier!r} is not a layer of this BGS service. "
                f"Available: {', '.join(sorted(known))}"
            )

        width, height = pixel_size(aoi, max_pixels=self.max_pixels)
        min_lon, min_lat, max_lon, max_lat = aoi
        url = f"{self.base_url}?" + urllib.parse.urlencode(
            {
                "service": "WMS",
                "request": "GetMap",
                "version": WMS_VERSION,
                "layers": dataset.identifier,
                "styles": "",
                # CRS:84 is longitude-first. Under EPSG:4326 this same bbox
                # would be read as latitude-first and return an image of a
                # different place entirely, with no error.
                "crs": REQUEST_CRS,
                "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                "width": width,
                "height": height,
                "format": IMAGE_FORMAT,
                "transparent": "true",
            }
        )

        body = self._get(url)

        # WMS reports errors as a 200 with an XML body. Writing that into a
        # .tif would produce a file GDAL refuses somewhere much later, with a
        # message about the file rather than about the request.
        if body[:5] in (b"<?xml", b"<Serv", b"<html", b"<HTML"):
            snippet = body[:400].decode("utf-8", errors="replace")
            raise BgsError(f"BGS returned an error document rather than an image: {snippet}")
        if len(body) < 128:
            raise BgsError(f"BGS returned {len(body)} bytes, which is not an image")

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)

        # MapServer's image/tiff carries no geotags, so the georeferencing is
        # written from the bbox and size we chose --- which are known exactly.
        destination.with_suffix(".tfw").write_text(
            world_file(aoi, width, height), encoding="ascii"
        )
        destination.with_suffix(".prj").write_text(
            'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
            'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
            encoding="ascii",
        )
        return destination


__all__ = [
    "BASE_URL",
    "IMAGE_FORMAT",
    "MAX_PIXELS",
    "RASTER_ONLY_NOTE",
    "REQUEST_CRS",
    "BgsAdapter",
    "BgsError",
    "BgsLayer",
    "parse_capabilities",
    "pixel_size",
    "world_file",
]
