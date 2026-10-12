"""Terrain export and sea-level modelling (tasks 5.2 and 5.4).

Executes in the **child** process under the QGIS-bundled interpreter, which is
where numpy, GDAL and scipy all live. Launched by path; imports only the
standard library, the GIS stack and the shared ``qgis_mcp_protocol``.

Both operations here exist because of failures that are *silent* --
``requirements.md`` §5 groups REQ-11, REQ-12 and REQ-13 for exactly that
reason: "each guards a failure that is silent -- producing plausible-looking
output that is wrong in a way neither the agent nor a casual look at the map
will catch."

**export_heightmap** (REQ-11). Three of the four clauses guard an invisible
error. 8-bit output gives 256 elevation steps, which across a 300 m range is
1.2 m per step and reads as stair-stepping on any rendered hillside. Nodata
left in the normalisation range becomes the minimum, crushing real terrain into
a fraction of the output range -- and the file's metadata looks perfectly
normal. A geographic-CRS DEM yields a displacement ratio computed from degrees,
which at UK latitudes stretches terrain east-west by roughly 62%: plausible
terrain, not the shape of that place.

**model_sea_level_change** (REQ-13). Thresholding, never mutation: the source
DEM is opened read-only and the outputs are new files. Sea level is modelled in
single-metre increments, so a vertical-datum mismatch of a metre or two is a
large fraction of the signal -- hence the warning when the datum is unknown.

Usage: ``python terrain.py <request.json>``
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from qgis_mcp_protocol import ErrorCode, ExecutionMode, encode, error, ok

OPERATIONS = ("export_heightmap", "model_sea_level_change", "transform_bbox")

#: Operations that work on a raster. ``transform_bbox`` is pure coordinate
#: maths and needs no source file.
RASTER_OPERATIONS = ("export_heightmap", "model_sea_level_change")

#: Full range of an unsigned 16-bit channel. REQ-11 clause 1 requires at least
#: 16 bits; this is the floor, not a preference.
UINT16_MAX = 65535

#: Output formats, and the data type each carries.
HEIGHTMAP_FORMATS = {
    # What Blender's displacement modifier wants, and the reason 16-bit is the
    # default: PNG supports it, and GDAL's PNG driver is CreateCopy-only, so
    # the raster is staged in memory first.
    "png": ("PNG", "UInt16"),
    "tif": ("GTiff", "UInt16"),
    # Lossless, for a pipeline that would rather carry metres than a
    # normalised integer.
    "tif32": ("GTiff", "Float32"),
}

CONNECTIVITY_BATHTUB = "bathtub"
CONNECTIVITY_OCEAN = "ocean"


def _emit(envelope) -> None:
    sys.stdout.write("\n" + encode(envelope) + "\n")
    sys.stdout.flush()


def _fail(code: str, message: str, **detail):
    _emit(error(code, message, mode=ExecutionMode.HEADLESS, **detail))


def _open_dem(path: str):
    """Open a DEM read-only, and read its grid and georeferencing.

    Read-only is load-bearing for ``model_sea_level_change``: task 5.4
    specifies a test asserting the source DEM's statistics are unchanged after
    the call, and opening in update mode would make that depend on nothing
    happening to write rather than on nothing being able to.
    """
    import numpy as np
    from osgeo import gdal, osr

    gdal.UseExceptions()
    dataset = gdal.Open(path, gdal.GA_ReadOnly)
    band = dataset.GetRasterBand(1)
    array = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()

    if nodata is None:
        mask = np.zeros(array.shape, dtype=bool)
    else:
        mask = array == nodata
    # NaN is nodata regardless of whether a sentinel is declared -- a
    # reprojected raster commonly carries NaN edges with no nodata value set,
    # and leaving them in the range is the same bug as leaving a sentinel in.
    mask |= ~np.isfinite(array)

    srs = osr.SpatialReference()
    projection = dataset.GetProjection()
    if projection:
        srs.ImportFromWkt(projection)

    geotransform = dataset.GetGeoTransform()
    authority = None
    if projection and srs.GetAuthorityName(None) and srs.GetAuthorityCode(None):
        authority = f"{srs.GetAuthorityName(None)}:{srs.GetAuthorityCode(None)}"

    return {
        "dataset": dataset,
        "array": array,
        "mask": mask,
        "nodata": nodata,
        "width": dataset.RasterXSize,
        "height": dataset.RasterYSize,
        "geotransform": geotransform,
        "projection": projection,
        "crs": authority,
        "is_geographic": bool(projection) and bool(srs.IsGeographic()),
        "pixel_width": abs(geotransform[1]),
        "pixel_height": abs(geotransform[5]),
    }


def _write_raster(path, array, *, dtype, geotransform, projection, nodata=None, driver="GTiff"):
    """Write an array, staging through memory when the driver cannot Create."""
    from osgeo import gdal

    gdal_type = {
        "UInt16": gdal.GDT_UInt16,
        "Float32": gdal.GDT_Float32,
        "Byte": gdal.GDT_Byte,
    }[dtype]
    height, width = array.shape

    memory = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal_type)
    memory.SetGeoTransform(geotransform)
    if projection:
        memory.SetProjection(projection)
    band = memory.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(float(nodata))
    band.WriteArray(array)
    band.FlushCache()

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    gdal.GetDriverByName(driver).CreateCopy(path, memory, 0)
    memory = None
    return path


# ---------------------------------------------------------------------------
# 5.2 export_heightmap (REQ-11)
# ---------------------------------------------------------------------------


def op_export_heightmap(request: dict) -> dict:
    import numpy as np
    from osgeo import gdal

    source = request["source"]
    destination = request["destination"]
    fmt = (request.get("format") or "png").lower()
    nodata_fill = (request.get("nodata_fill") or "min").lower()
    allow_geographic = bool(request.get("allow_geographic", False))

    if fmt not in HEIGHTMAP_FORMATS:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"format must be one of {sorted(HEIGHTMAP_FORMATS)}; got {fmt!r}",
        )
    driver, dtype = HEIGHTMAP_FORMATS[fmt]

    dem = _open_dem(source)

    # REQ-11 clause 4. Refused rather than silently reprojected: an export is
    # the wrong place to change a layer's CRS, and the caller who reprojects
    # deliberately gets to choose the resampling.
    if dem["is_geographic"] and not allow_geographic:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"this DEM is in a geographic CRS ({dem['crs'] or 'unknown'}), so "
            f"its pixels are degrees rather than metres and a displacement "
            f"ratio derived from them would be wrong -- at UK latitudes it "
            f"stretches terrain east-west by roughly 62%, which looks like "
            f"plausible terrain and is not the shape of that place (REQ-11). "
            f"Reproject to a projected CRS first: download_and_register_layer "
            f"does it on acquisition, or run gdal:warpreproject.",
            crs=dem["crs"],
            is_geographic=True,
        )
    if not dem["projection"]:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            "this DEM has no CRS at all, so its ground extent in metres cannot "
            "be determined and the displacement ratio would be meaningless.",
        )

    array = dem["array"]
    mask = dem["mask"]
    valid = array[~mask]
    if valid.size == 0:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            "every cell in this DEM is nodata, so there is no elevation range "
            "to normalise.",
        )

    # REQ-11 clause 2: the range comes from valid cells only. This single line
    # is the one the whole requirement is about -- including the sentinel would
    # leave the file looking entirely normal.
    source_min = float(valid.min())
    source_max = float(valid.max())

    filled = array.copy()
    nodata_cells = int(mask.sum())
    if nodata_cells:
        if nodata_fill == "interpolate":
            # Interpolating across small holes beats a flat plateau, but only
            # for small holes -- across a large gap it invents terrain.
            interpolated = _interpolate_nodata(dem, array, mask)
            filled = interpolated
            remaining = ~np.isfinite(filled)
            filled[remaining] = source_min
        elif nodata_fill == "min":
            filled[mask] = source_min
        else:
            raise _Refusal(
                ErrorCode.BAD_REQUEST,
                f"nodata_fill must be 'min' or 'interpolate'; got "
                f"{nodata_fill!r}",
            )

    span = source_max - source_min
    if span <= 0:
        # A flat DEM is legitimate and normalising it would divide by zero.
        # Mid-grey keeps a displacement modifier from producing a crease.
        normalised = np.full(filled.shape, UINT16_MAX // 2, dtype="float64")
        flat = True
    else:
        normalised = (filled - source_min) / span
        flat = False

    if dtype == "Float32":
        # Metres, not a normalised integer: nothing to scale and nothing lost.
        out_array = filled.astype("float32")
    else:
        out_array = np.clip(normalised * UINT16_MAX, 0, UINT16_MAX).astype("uint16")

    _write_raster(
        destination,
        out_array,
        dtype=dtype,
        geotransform=dem["geotransform"],
        projection=dem["projection"],
        driver=driver,
    )

    # REQ-11 clause 3. Without these the vertical exaggeration is set by eye
    # and is irreproducible between exports: a heightmap image carries no
    # absolute scale.
    extent_width_m = dem["width"] * dem["pixel_width"]
    extent_height_m = dem["height"] * dem["pixel_height"]
    ratio = (span / extent_width_m) if extent_width_m else None

    result = {
        "path": destination,
        "format": fmt,
        "driver": driver,
        "data_type": dtype,
        "bits_per_channel": 32 if dtype == "Float32" else 16,
        "width": dem["width"],
        "height": dem["height"],
        "source_crs": dem["crs"],
        # The REQ-11 clause 3 payload.
        "source_elevation_min_m": source_min,
        "source_elevation_max_m": source_max,
        "source_elevation_range_m": span,
        "extent_width_m": extent_width_m,
        "extent_height_m": extent_height_m,
        "aspect_ratio": (
            extent_width_m / extent_height_m if extent_height_m else None
        ),
        "displacement_ratio": ratio,
        "displacement_note": (
            "displacement strength for a plane one Blender unit wide: set the "
            "plane's X size to 1, its Y size to the reciprocal of aspect_ratio, "
            "and the displacement strength to displacement_ratio."
        ),
        "nodata_cells": nodata_cells,
        "nodata_fill": nodata_fill if nodata_cells else None,
        "valid_cells": int(valid.size),
        "flat": flat,
        "warnings": [],
    }

    if nodata_cells:
        result["warnings"].append(
            f"{nodata_cells} of {array.size} cells were nodata and were filled "
            f"using '{nodata_fill}'. They are excluded from the elevation "
            f"range, so the terrain is scaled correctly, but the filled areas "
            f"are not measured ground."
        )
    if flat:
        result["warnings"].append(
            "this DEM has no elevation variation, so the heightmap is uniform "
            "mid-grey and a displacement ratio cannot be derived."
        )
    return result


def _interpolate_nodata(dem, array, mask):
    """Fill nodata by interpolation, via GDAL's own FillNodata."""
    import numpy as np
    from osgeo import gdal

    height, width = array.shape
    memory = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Float32)
    memory.SetGeoTransform(dem["geotransform"])
    if dem["projection"]:
        memory.SetProjection(dem["projection"])
    band = memory.GetRasterBand(1)
    staged = array.astype("float32").copy()
    sentinel = -3.4e38
    staged[mask] = sentinel
    band.SetNoDataValue(sentinel)
    band.WriteArray(staged)
    band.FlushCache()
    gdal.FillNodata(band, None, maxSearchDist=max(width, height), smoothingIterations=0)
    out = band.ReadAsArray().astype("float64")
    memory = None
    out[out <= sentinel / 2] = np.nan
    return out


# ---------------------------------------------------------------------------
# 5.4 model_sea_level_change (REQ-13)
# ---------------------------------------------------------------------------


def op_model_sea_level_change(request: dict) -> dict:
    import numpy as np
    from osgeo import gdal, ogr, osr

    source = request["source"]
    sea_level = float(request.get("sea_level_m", 0.0))
    connectivity = (request.get("connectivity") or CONNECTIVITY_BATHTUB).lower()
    output_dir = request["output_dir"]
    prefix = request.get("prefix") or "sea_level"
    want_depth = bool(request.get("depth_raster", False))
    vertical_datum = request.get("vertical_datum")

    if connectivity not in (CONNECTIVITY_BATHTUB, CONNECTIVITY_OCEAN):
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"connectivity must be {CONNECTIVITY_BATHTUB!r} or "
            f"{CONNECTIVITY_OCEAN!r}; got {connectivity!r}",
        )

    dem = _open_dem(source)
    array = dem["array"]
    mask = dem["mask"]
    valid = ~mask

    os.makedirs(output_dir, exist_ok=True)

    # Thresholding, not mutation. The source was opened read-only and every
    # output below is a new file.
    below = (array <= sea_level) & valid
    bathtub_cells = int(below.sum())

    if connectivity == CONNECTIVITY_OCEAN:
        water = _ocean_connected(below)
    else:
        water = below
    water_cells = int(water.sum())

    cell_area = dem["pixel_width"] * dem["pixel_height"]
    land_cells = int((valid & ~water).sum())

    # --- water mask raster -------------------------------------------------
    mask_path = os.path.join(output_dir, f"{prefix}_water_mask.tif")
    mask_array = np.zeros(array.shape, dtype="uint8")
    mask_array[water] = 1
    # Nodata cells are neither land nor water, and saying so beats calling
    # them land -- an unsurveyed hole is not dry ground.
    mask_array[mask] = 255
    _write_raster(
        mask_path,
        mask_array,
        dtype="Byte",
        geotransform=dem["geotransform"],
        projection=dem["projection"],
        nodata=255,
    )

    # --- polygonised coastline --------------------------------------------
    polygon_path = os.path.join(output_dir, f"{prefix}_water.gpkg")
    polygon_count, polygon_area = _polygonise(
        polygon_path, mask_array, dem, value=1
    )

    steps = [
        {"step": "threshold", "sea_level_m": sea_level, "connectivity": connectivity},
        {"step": "water_mask", "path": mask_path},
        {"step": "polygonise", "path": polygon_path, "polygons": polygon_count},
    ]

    # --- optional depth raster --------------------------------------------
    depth_path = None
    if want_depth:
        depth_path = os.path.join(output_dir, f"{prefix}_depth.tif")
        depth = np.full(array.shape, np.nan, dtype="float32")
        depth[water] = (sea_level - array[water]).astype("float32")
        _write_raster(
            depth_path,
            depth,
            dtype="Float32",
            geotransform=dem["geotransform"],
            projection=dem["projection"],
            nodata=float("nan"),
        )
        steps.append({"step": "depth_raster", "path": depth_path})

    result = {
        "sea_level_m": sea_level,
        "connectivity": connectivity,
        "source": source,
        "source_crs": dem["crs"],
        "water_mask_path": mask_path,
        "water_polygons_path": polygon_path,
        "depth_raster_path": depth_path,
        "water_cells": water_cells,
        "land_cells": land_cells,
        "nodata_cells": int(mask.sum()),
        "cell_area_m2": cell_area,
        "water_area_m2": water_cells * cell_area,
        "land_area_m2": land_cells * cell_area,
        "water_polygon_count": polygon_count,
        "water_polygon_area_m2": polygon_area,
        "steps": steps,
        "warnings": [],
    }

    if connectivity == CONNECTIVITY_OCEAN:
        # The difference between the two modes is the whole point of the
        # option, so it is reported rather than left to be inferred.
        result["cells_below_sea_level"] = bathtub_cells
        result["landlocked_cells_excluded"] = bathtub_cells - water_cells
        result["landlocked_area_m2"] = (bathtub_cells - water_cells) * cell_area

    # REQ-13: warn on an unknown datum and proceed.
    if not vertical_datum or str(vertical_datum).lower() == "unknown":
        result["warnings"].append(
            "this DEM's vertical datum is unknown. Sea level is modelled in "
            "single-metre increments, so a datum offset of a metre or two is a "
            "large fraction of the signal -- and ellipsoidal heights would be "
            "wrong by roughly 50 m in the UK. The result is produced, but "
            "check the datum before relying on the coastline (REQ-13)."
        )
        result["vertical_datum"] = None
    else:
        result["vertical_datum"] = str(vertical_datum)
        if "ellipsoid" in str(vertical_datum).lower():
            result["warnings"].append(
                f"this DEM uses {vertical_datum} heights rather than "
                f"orthometric ones, so a sea level of {sea_level} m is not "
                f"measured from mean sea level. In the UK the offset is "
                f"roughly 50 m. Convert the datum before trusting this."
            )

    if dem["is_geographic"]:
        result["warnings"].append(
            f"this DEM is in a geographic CRS ({dem['crs']}), so the reported "
            f"areas are in square degrees rather than square metres and are "
            f"not comparable to anything. Reproject before measuring."
        )
    return result


def _ocean_connected(below):
    """Water cells reachable from the edge of the grid.

    Four-connectivity, not eight: water does not pass diagonally through the
    corner between two land cells, and allowing it would flood basins that are
    in fact enclosed -- which is precisely the distinction this option exists
    to make.
    """
    import numpy as np
    from scipy import ndimage

    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)
    labels, count = ndimage.label(below, structure=structure)
    if count == 0:
        return np.zeros(below.shape, dtype=bool)

    edge_labels = set()
    for strip in (labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]):
        edge_labels.update(int(value) for value in np.unique(strip) if value)
    if not edge_labels:
        return np.zeros(below.shape, dtype=bool)
    return np.isin(labels, list(edge_labels))


def _polygonise(path, mask_array, dem, *, value=1):
    """Polygonise the cells equal to ``value``, returning count and total area."""
    import numpy as np
    from osgeo import gdal, ogr, osr

    height, width = mask_array.shape
    selected = (mask_array == value).astype("uint8")

    memory = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Byte)
    memory.SetGeoTransform(dem["geotransform"])
    if dem["projection"]:
        memory.SetProjection(dem["projection"])
    band = memory.GetRasterBand(1)
    band.WriteArray(selected)
    band.FlushCache()

    if os.path.exists(path):
        os.remove(path)
    driver = ogr.GetDriverByName("GPKG")
    datasource = driver.CreateDataSource(path)
    srs = None
    if dem["projection"]:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(dem["projection"])
    layer = datasource.CreateLayer("water", srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))

    # The band doubles as its own mask, so only cells equal to `value` are
    # polygonised -- without it GDAL emits a polygon for the zeros too, and the
    # count would include the land.
    gdal.Polygonize(band, band, layer, 0)

    count = 0
    area = 0.0
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None:
            continue
        count += 1
        area += geometry.GetArea()
    datasource = None
    memory = None
    return count, area


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _Refusal(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def op_transform_bbox(request: dict) -> dict:
    """Transform a bounding box between CRSs, returning two rectangles.

    ``design.md`` §5: "AOI geometry travels as 4326; clip and export extents
    are in the target CRS", and it names the consequence -- "a reprojected 4326
    bbox is a curved quadrilateral rather than a rectangle, so clipping to it
    leaves a ragged edge and nodata to handle downstream".

    So both answers are returned. The **outer** rectangle bounds the whole
    transformed quadrilateral and keeps every pixel, at the cost of nodata in
    the corners. The **inner** rectangle is the largest axis-aligned rectangle
    that fits inside it, giving a clean edge at the cost of a thin sliver -- and
    that is what a heightmap wants, since REQ-11's displacement ratio is
    computed from the extent and nodata corners would distort it.

    Edges are sampled rather than only the corners: a projection curves the
    sides, so the extreme point of a transformed edge is usually in its middle,
    and using corners alone would give an outer box that clips real data.
    """
    from osgeo import osr

    bbox = request.get("bbox")
    source_crs = request.get("source_crs") or "EPSG:4326"
    target_crs = request.get("target_crs")
    samples = int(request.get("samples", 16))

    if not bbox or len(bbox) != 4:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"bbox must be [xmin, ymin, xmax, ymax]; got {bbox!r}",
        )
    if not target_crs:
        raise _Refusal(ErrorCode.BAD_REQUEST, "target_crs is required")

    source = osr.SpatialReference()
    target = osr.SpatialReference()
    for srs, text, label in (
        (source, source_crs, "source_crs"),
        (target, target_crs, "target_crs"),
    ):
        if srs.SetFromUserInput(str(text)) != 0:
            raise _Refusal(
                ErrorCode.BAD_REQUEST, f"{label} is not a usable CRS: {text!r}"
            )
    # Authority-compliant axis order would give lat/lon for EPSG:4326, which
    # is not the order anything else in this server uses.
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    transform = osr.CoordinateTransformation(source, target)
    xmin, ymin, xmax, ymax = (float(value) for value in bbox)

    def edge(count, fixed, axis):
        step = count - 1
        for index in range(count):
            fraction = index / step if step else 0.0
            if axis == "x":
                yield xmin + (xmax - xmin) * fraction, fixed
            else:
                yield fixed, ymin + (ymax - ymin) * fraction

    bottom = list(edge(samples, ymin, "x"))
    top = list(edge(samples, ymax, "x"))
    left = list(edge(samples, xmin, "y"))
    right = list(edge(samples, xmax, "y"))

    def project(points):
        out = []
        for x, y in points:
            px, py, *_ = transform.TransformPoint(x, y)
            out.append((px, py))
        return out

    bottom_p, top_p, left_p, right_p = (
        project(bottom), project(top), project(left), project(right)
    )
    everything = bottom_p + top_p + left_p + right_p
    xs = [point[0] for point in everything]
    ys = [point[1] for point in everything]

    outer = [min(xs), min(ys), max(xs), max(ys)]
    # The inner rectangle is bounded by the innermost extreme of each side.
    inner = [
        max(point[0] for point in left_p),
        max(point[1] for point in bottom_p),
        min(point[0] for point in right_p),
        min(point[1] for point in top_p),
    ]

    valid_inner = inner[0] < inner[2] and inner[1] < inner[3]
    result = {
        "bbox": [xmin, ymin, xmax, ymax],
        "source_crs": source_crs,
        "target_crs": target_crs,
        "outer": outer,
        "inner": inner if valid_inner else None,
        "outer_width_m": outer[2] - outer[0],
        "outer_height_m": outer[3] - outer[1],
        "samples_per_edge": samples,
        "warnings": [],
    }
    if not valid_inner:
        result["warnings"].append(
            "the transformed area is too distorted for an inscribed rectangle; "
            "use the outer bounds and expect nodata at the edges."
        )
    return result


HANDLERS = {
    "export_heightmap": op_export_heightmap,
    "model_sea_level_change": op_model_sea_level_change,
    "transform_bbox": op_transform_bbox,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _fail(ErrorCode.BAD_REQUEST, "terrain expects one argument, a request file")
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _fail(ErrorCode.BAD_REQUEST, f"could not read request file: {exc}")
        return 2

    operation = request.get("operation")
    if operation not in HANDLERS:
        _fail(
            ErrorCode.BAD_REQUEST,
            f"unknown operation {operation!r}",
            supported_operations=list(OPERATIONS),
        )
        return 2

    if operation in RASTER_OPERATIONS:
        source = request.get("source")
        if not source or not os.path.exists(source):
            _fail(
                ErrorCode.BAD_REQUEST,
                f"source raster does not exist: {source}",
                source=source,
            )
            return 1

    try:
        result = HANDLERS[operation](request)
    except _Refusal as refusal:
        _fail(refusal.code, str(refusal), operation=operation, **refusal.detail)
        return 1
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        _fail(
            ErrorCode.CHILD_FAILED,
            f"{type(exc).__name__}: {exc}",
            operation=operation,
            traceback=traceback.format_exc(),
        )
        return 1

    _emit(ok(result, mode=ExecutionMode.HEADLESS, operation=operation))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
