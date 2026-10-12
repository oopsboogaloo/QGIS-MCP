"""Clipping a layer to an area of interest (task 4.2).

A thin wrapper over four Processing algorithms. ``tasks.md`` places it here
rather than with the terrain set because the acquisition flow needs it:
provider extents routinely exceed the requested area, so without it every
acquisition ends in untrimmed layers -- and Natural Earth has no server-side
bbox filtering at all, so its files arrive whole and *must* be clipped locally.

**The reason this is a tool and not a note in a docstring.** QGIS extent strings
are ordered ``xmin,xmax,ymin,ymax``. Nearly every other spatial interface a
caller will have touched -- GeoJSON bboxes, OGC, GDAL's ``-te``, shapely's
``bounds`` -- orders them ``xmin,ymin,xmax,ymax``. Passing the latter to
``gdal:cliprasterbyextent`` does not fail. Clipping the 50x50 fixture DEM to
``440200,180300,440600,180600`` instead of ``440200,440600,180300,180600``
returns a 12995x13000 raster with easting and northing swapped, reported as
success. That is the silent-plausible-output failure mode ``design.md`` §5 is
written about, and the fix is to invert the order once, here, where it has a
test on it -- rather than at each of the dozens of call sites tasks 4 and 5 will
add.

So this module takes bounding boxes in the conventional
``(xmin, ymin, xmax, ymax)`` order and converts at the boundary.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_protocol import Envelope, ErrorCode, error
from qgis_mcp_server.processing import ProcessingRunner

#: Raster and vector, per input kind and per AOI form.
ALGORITHM_BY_CASE = {
    ("raster", "bbox"): "gdal:cliprasterbyextent",
    ("raster", "mask"): "gdal:cliprasterbymasklayer",
    ("vector", "bbox"): "native:extractbyextent",
    ("vector", "mask"): "native:clip",
}

RASTER_SUFFIXES = {
    ".tif",
    ".tiff",
    ".vrt",
    ".img",
    ".asc",
    ".bil",
    ".dem",
    ".hgt",
    ".jp2",
    ".nc",
    ".grd",
    ".png",
    ".jpg",
}

VECTOR_SUFFIXES = {
    ".shp",
    ".geojson",
    ".json",
    ".gml",
    ".kml",
    ".kmz",
    ".gpx",
    ".tab",
    ".mif",
    ".fgb",
    ".parquet",
}

#: GeoPackage and SQLite can hold either, so they are not in the maps above.
#: Guessing here would be a coin flip whose wrong side produces a confusing
#: algorithm error rather than a clear one.
AMBIGUOUS_SUFFIXES = {".gpkg", ".sqlite", ".db", ".zip"}


def format_qgis_extent(
    bbox: tuple[float, float, float, float], crs: str | None = None
) -> str:
    """Render a bounding box as a QGIS extent string.

    Takes ``(xmin, ymin, xmax, ymax)`` -- the order GeoJSON, OGC, GDAL and
    shapely all use -- and emits ``xmin,xmax,ymin,ymax``, which is what QGIS
    parses. See the module docstring for why this conversion is worth isolating.

    The CRS suffix is not decoration. Without it QGIS interprets the numbers in
    the layer's own CRS, which is right by luck when they match and silently
    wrong when they do not.
    """
    xmin, ymin, xmax, ymax = (float(value) for value in bbox)
    extent = f"{xmin},{xmax},{ymin},{ymax}"
    return f"{extent} [{crs}]" if crs else extent


def validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Check a bounding box given as ``(xmin, ymin, xmax, ymax)``.

    A reversed box is refused rather than normalised. Silently sorting the
    numbers would mask the one mistake this module exists to prevent: a caller
    who supplied them in QGIS's order instead of this one.
    """
    try:
        values = tuple(float(value) for value in bbox)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"bbox must be four numbers (xmin, ymin, xmax, ymax); got {bbox!r}"
        ) from exc
    if len(values) != 4:
        raise ValueError(
            f"bbox must have exactly four values (xmin, ymin, xmax, ymax); got "
            f"{len(values)}"
        )
    xmin, ymin, xmax, ymax = values
    if xmin >= xmax or ymin >= ymax:
        raise ValueError(
            f"bbox (xmin={xmin}, ymin={ymin}, xmax={xmax}, ymax={ymax}) is "
            f"empty or reversed. Expected order is (xmin, ymin, xmax, ymax) -- "
            f"note this is NOT QGIS's own xmin,xmax,ymin,ymax extent order, "
            f"which is converted internally."
        )
    return xmin, ymin, xmax, ymax


def detect_data_kind(source: str | Path) -> str | None:
    """Guess whether a source is raster or vector from its extension.

    Returns ``None`` when the extension cannot decide, which includes
    GeoPackage -- it holds either. An explicit answer from the caller beats a
    guess, so ``None`` becomes a request for one rather than a default.
    """
    path = Path(str(source).split("|", 1)[0])
    suffix = path.suffix.lower()
    if suffix in RASTER_SUFFIXES:
        return "raster"
    if suffix in VECTOR_SUFFIXES:
        return "vector"
    return None


@dataclass
class ClipRunner:
    """Clips layers via Processing.

    Delegates to :class:`ProcessingRunner`, which means REQ-4's destination
    guard applies here without being reimplemented: an output path that already
    exists needs ``confirm=True``.
    """

    processing: ProcessingRunner

    def clip(
        self,
        source: str | Path,
        destination: str | Path,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        bbox_crs: str | None = None,
        mask: str | Path | None = None,
        data_kind: str | None = None,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Envelope:
        """Clip ``source`` to an AOI and write it to ``destination``.

        Exactly one of ``bbox`` or ``mask`` is required. A bounding box gives a
        rectangular result with no ragged nodata edge, which is what a Blender
        heightmap wants; a mask polygon follows a real boundary, which is what
        keeps a city render from being fringed with isolated farm tracks.
        """
        if (bbox is None) == (mask is None):
            return error(
                ErrorCode.BAD_REQUEST,
                "clip_to_aoi needs exactly one of bbox or mask. A bounding box "
                "gives a rectangular result with no ragged edge; a mask polygon "
                "follows a real boundary.",
            )

        resolved_kind = data_kind or detect_data_kind(source)
        if resolved_kind is None:
            suffix = Path(str(source).split("|", 1)[0]).suffix.lower()
            hint = (
                " GeoPackage and SQLite can hold either raster or vector, so "
                "the extension cannot decide."
                if suffix in AMBIGUOUS_SUFFIXES
                else ""
            )
            return error(
                ErrorCode.BAD_REQUEST,
                f"cannot tell whether {source} is raster or vector from its "
                f"extension ({suffix or 'none'}).{hint} Pass data_kind="
                f"'raster' or 'vector'.",
                source=str(source),
            )
        if resolved_kind not in ("raster", "vector"):
            return error(
                ErrorCode.BAD_REQUEST,
                f"data_kind must be 'raster' or 'vector'; got {resolved_kind!r}",
            )

        aoi_form = "bbox" if bbox is not None else "mask"
        algorithm = ALGORITHM_BY_CASE[(resolved_kind, aoi_form)]

        parameters: dict[str, Any] = {
            "INPUT": str(source),
            "OUTPUT": str(destination),
        }

        if bbox is not None:
            try:
                validated = validate_bbox(bbox)
            except ValueError as exc:
                return error(ErrorCode.BAD_REQUEST, str(exc), bbox=list(bbox))
            extent = format_qgis_extent(validated, bbox_crs)
            if resolved_kind == "raster":
                parameters["PROJWIN"] = extent
            else:
                parameters["EXTENT"] = extent
                # Truncate geometries at the boundary rather than keeping whole
                # features that merely intersect it. A road running 20 km out
                # of the AOI is not what "clip to this area" means.
                parameters["CLIP"] = True
        else:
            if resolved_kind == "raster":
                parameters["MASK"] = str(mask)
                # Without this the output keeps the input's full extent and
                # only blanks the outside, which defeats the purpose: the file
                # stays the original size and a heightmap built from it is
                # mostly nodata.
                parameters["CROP_TO_CUTLINE"] = True
                # The point of clipping is usually to prepare for an export,
                # and resampling here would be a second, unasked-for change to
                # the pixel grid.
                parameters["KEEP_RESOLUTION"] = True
            else:
                parameters["OVERLAY"] = str(mask)

        envelope = self.processing.run(
            algorithm,
            parameters,
            confirm=confirm,
            timeout=timeout,
        )

        if envelope.ok and isinstance(envelope.result, dict):
            # Which algorithm ran is part of the answer: the four cases behave
            # differently at the edges, and a caller comparing two clips needs
            # to know they went through the same one.
            envelope.result["clip"] = {
                "algorithm_id": algorithm,
                "data_kind": resolved_kind,
                "aoi_form": aoi_form,
                "data_kind_was_detected": data_kind is None,
            }
            if bbox is not None:
                envelope.result["clip"]["bbox"] = list(validate_bbox(bbox))
                envelope.result["clip"]["bbox_crs"] = bbox_crs
                envelope.result["clip"]["qgis_extent_string"] = format_qgis_extent(
                    validate_bbox(bbox), bbox_crs
                )
        return envelope


__all__ = [
    "ALGORITHM_BY_CASE",
    "AMBIGUOUS_SUFFIXES",
    "RASTER_SUFFIXES",
    "VECTOR_SUFFIXES",
    "ClipRunner",
    "detect_data_kind",
    "format_qgis_extent",
    "validate_bbox",
]
