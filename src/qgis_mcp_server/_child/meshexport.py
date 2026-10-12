"""Exporting vector layers for Blender (task 5.5).

``export_vector_layer_for_mesh``: roads, coastlines and contours out to a
mesh-friendly format, optionally draped over a DEM so they sit on the terrain a
heightmap produced rather than flat at zero.

Executes in the **child** process. Uses OGR and GDAL directly rather than
PyQGIS -- reading geometry and sampling a raster need neither a QgsApplication
nor a project, and avoiding both keeps this the fastest of the child scripts.

Two formats, and the spec's "e.g. GeoJSON with elevation-sampled Z values, or
OBJ/SVG" is why SVG is absent: a Print Layout export through
``render_map_to_path`` already produces publication SVG, and a hand-written one
would duplicate that badly. GeoJSON carries Z for Blender's GIS importers; OBJ
goes in directly as curves.

Usage: ``python meshexport.py <request.json>``
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from qgis_mcp_protocol import ErrorCode, ExecutionMode, encode, error, ok

OPERATIONS = ("export_vector_for_mesh",)

MESH_FORMATS = ("geojson", "obj")


def _emit(envelope) -> None:
    sys.stdout.write("\n" + encode(envelope) + "\n")
    sys.stdout.flush()


def _fail(code: str, message: str, **detail):
    _emit(error(code, message, mode=ExecutionMode.HEADLESS, **detail))


class _Refusal(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _read_dem(path: str):
    """Read a DEM's grid, mask and georeferencing.

    A small reader of its own rather than an import from ``terrain.py``: the
    child scripts are copied into one staging directory and could import each
    other, but a dependency between two files that are only incidentally
    neighbours is worse than twenty lines repeated.
    """
    import numpy as np
    from osgeo import gdal, osr

    gdal.UseExceptions()
    dataset = gdal.Open(path, gdal.GA_ReadOnly)
    band = dataset.GetRasterBand(1)
    array = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()

    mask = np.zeros(array.shape, dtype=bool) if nodata is None else array == nodata
    mask |= ~np.isfinite(array)

    projection = dataset.GetProjection()
    srs = osr.SpatialReference()
    if projection:
        srs.ImportFromWkt(projection)

    return {
        "array": array,
        "mask": mask,
        "geotransform": dataset.GetGeoTransform(),
        "is_geographic": bool(projection) and bool(srs.IsGeographic()),
        "crs": (
            f"{srs.GetAuthorityName(None)}:{srs.GetAuthorityCode(None)}"
            if projection and srs.GetAuthorityCode(None)
            else None
        ),
    }


def _sample(dem, x: float, y: float):
    """Bilinear elevation at a map coordinate, or ``None`` where there is none.

    Bilinear rather than nearest: these vertices become a curve draped over
    terrain, and nearest sampling puts visible stair-steps along it at exactly
    the scale a viewer notices.

    Sample points are pixel *centres*. That half-pixel offset is the detail
    that otherwise shifts every sample by half a cell -- 15 m on a 30 m DEM,
    which is enough to drop a ridge-top road onto the slope beside it.
    """
    import numpy as np

    origin_x, pixel_w, _, origin_y, _, pixel_h = dem["geotransform"]
    if pixel_w == 0 or pixel_h == 0:
        return None

    column = (x - origin_x) / pixel_w - 0.5
    row = (y - origin_y) / pixel_h - 0.5

    array, mask = dem["array"], dem["mask"]
    height, width = array.shape
    if not (-0.5 <= column <= width - 0.5 and -0.5 <= row <= height - 0.5):
        return None

    base_col = int(np.floor(column))
    base_row = int(np.floor(row))

    total = 0.0
    accumulated = 0.0
    for row_offset in (0, 1):
        for col_offset in (0, 1):
            rr, cc = base_row + row_offset, base_col + col_offset
            if not (0 <= rr < height and 0 <= cc < width) or mask[rr, cc]:
                continue
            weight = (1.0 - abs(row - rr)) * (1.0 - abs(column - cc))
            if weight <= 0:
                continue
            total += weight
            accumulated += weight * float(array[rr, cc])
    if total <= 0:
        return None
    return accumulated / total


def _rings(geometry):
    """Flatten any geometry into a list of coordinate sequences."""
    name = geometry.GetGeometryName()
    if name.startswith("POINT"):
        return [[(geometry.GetX(), geometry.GetY())]]
    if name.startswith("LINESTRING") or name == "LINEARRING":
        return [
            [
                (geometry.GetX(index), geometry.GetY(index))
                for index in range(geometry.GetPointCount())
            ]
        ]
    collected = []
    for index in range(geometry.GetGeometryCount()):
        collected.extend(_rings(geometry.GetGeometryRef(index)))
    return collected


def op_export_vector_for_mesh(request: dict) -> dict:
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    source = request["source"]
    layer_name = request.get("layer")
    destination = request["destination"]
    fmt = (request.get("format") or "geojson").lower()
    dem_path = request.get("dem")
    z_scale = float(request.get("z_scale", 1.0))
    local_requested = request.get("local_origin")

    if fmt not in MESH_FORMATS:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"format must be one of {MESH_FORMATS}; got {fmt!r}",
        )

    datasource = ogr.Open(source)
    if datasource is None:
        raise _Refusal(
            ErrorCode.BAD_REQUEST, f"could not open vector source: {source}"
        )

    if layer_name:
        layer = datasource.GetLayerByName(str(layer_name))
        if layer is None:
            raise _Refusal(
                ErrorCode.BAD_REQUEST,
                f"no layer named {layer_name!r} in {source}",
                available_layers=[
                    datasource.GetLayerByIndex(index).GetName()
                    for index in range(datasource.GetLayerCount())
                ],
            )
    else:
        layer = datasource.GetLayerByIndex(0)

    spatial_ref = layer.GetSpatialRef()
    crs = None
    if spatial_ref is not None and spatial_ref.GetAuthorityCode(None):
        crs = (
            f"{spatial_ref.GetAuthorityName(None)}:"
            f"{spatial_ref.GetAuthorityCode(None)}"
        )
    if spatial_ref is not None and spatial_ref.IsGeographic():
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"this layer is in a geographic CRS ({crs or 'unknown'}), so its "
            f"coordinates are degrees rather than metres. A mesh built from "
            f"them is stretched east-west -- roughly 62% at UK latitudes, the "
            f"same distortion REQ-11 refuses for heightmaps. Reproject to a "
            f"projected CRS first.",
            crs=crs,
        )

    dem = _read_dem(dem_path) if dem_path else None
    if dem is not None and dem["is_geographic"]:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            "the DEM is in a geographic CRS and the vector layer is projected, "
            "so sampling would compare coordinates in different units and "
            "silently return elevations from the wrong places. Reproject the "
            "DEM first.",
            dem_crs=dem["crs"],
            layer_crs=crs,
        )
    if dem is not None and crs and dem["crs"] and dem["crs"] != crs:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"the layer is in {crs} and the DEM in {dem['crs']}. Sampling "
            f"across them would read elevations from the wrong locations -- "
            f"plausible numbers, wrong places. Reproject one to match.",
            dem_crs=dem["crs"],
            layer_crs=crs,
        )

    features = []
    sampled = 0
    unsampled = 0
    extent = [None, None, None, None]

    definition = layer.GetLayerDefn()
    field_names = [
        definition.GetFieldDefn(index).GetName()
        for index in range(definition.GetFieldCount())
    ]

    layer.ResetReading()
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None:
            continue
        parts = []
        for ring in _rings(geometry):
            points = []
            for x, y in ring:
                z = 0.0
                if dem is not None:
                    value = _sample(dem, x, y)
                    if value is None:
                        unsampled += 1
                    else:
                        sampled += 1
                        z = value * z_scale
                points.append([x, y, z])
                extent[0] = x if extent[0] is None else min(extent[0], x)
                extent[1] = y if extent[1] is None else min(extent[1], y)
                extent[2] = x if extent[2] is None else max(extent[2], x)
                extent[3] = y if extent[3] is None else max(extent[3], y)
            if points:
                parts.append(points)
        if parts:
            features.append(
                {
                    "parts": parts,
                    "geometry_name": geometry.GetGeometryName(),
                    "attributes": {
                        name: feature.GetField(name) for name in field_names
                    },
                }
            )

    if not features:
        raise _Refusal(
            ErrorCode.BAD_REQUEST, "the layer has no usable geometry to export."
        )

    # OBJ defaults to local coordinates. Blender's single-precision floats lose
    # centimetre accuracy at a 440,000 m easting, and that shows up as jitter
    # along a road rather than as an error anywhere.
    use_local = bool(local_requested) if local_requested is not None else fmt == "obj"
    origin = [extent[0], extent[1]] if use_local else [0.0, 0.0]

    extra = (
        _write_geojson(destination, features, origin, crs)
        if fmt == "geojson"
        else _write_obj(destination, features, origin, crs)
    )

    warnings = []
    if dem_path is None:
        warnings.append(
            "no DEM was given, so every vertex has Z = 0 and the result is "
            "flat. Pass dem to drape it over terrain."
        )
    if unsampled:
        warnings.append(
            f"{unsampled} of {sampled + unsampled} vertices fell outside the "
            f"DEM or on nodata and were given Z = 0, so they sit at the base "
            f"of the mesh rather than on it. Clip the layer to the DEM's "
            f"extent first if that matters."
        )
    if use_local:
        warnings.append(
            f"coordinates are local metres relative to {origin[0]:.1f}, "
            f"{origin[1]:.1f}. Use the same origin for every layer that must "
            f"line up, and add it back to return to projected coordinates."
        )

    return {
        "path": destination,
        "format": fmt,
        "source": source,
        "layer": layer.GetName(),
        "crs": crs,
        "feature_count": len(features),
        "vertex_count": sum(
            len(part) for item in features for part in item["parts"]
        ),
        "extent": extent,
        "local_coordinates": use_local,
        "local_origin": origin if use_local else None,
        "dem": dem_path,
        "z_scale": z_scale,
        "vertices_sampled": sampled,
        "vertices_outside_dem": unsampled,
        "file_bytes": os.path.getsize(destination),
        "warnings": warnings,
        **extra,
    }


def _write_geojson(destination, features, origin, crs):
    collection = {"type": "FeatureCollection", "features": []}
    for item in features:
        parts = [
            [[x - origin[0], y - origin[1], z] for x, y, z in part]
            for part in item["parts"]
        ]
        if len(parts) == 1 and len(parts[0]) == 1:
            geometry = {"type": "Point", "coordinates": parts[0][0]}
        elif len(parts) == 1:
            geometry = {"type": "LineString", "coordinates": parts[0]}
        else:
            geometry = {"type": "MultiLineString", "coordinates": parts}
        collection["features"].append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": item["attributes"],
            }
        )

    # RFC 7946 removed GeoJSON's `crs` member and assumes WGS84, which this is
    # not. Putting the CRS in a `crs` member would be ignored by a conforming
    # reader or, worse, read as a claim the coordinates are lat/lon -- so it
    # goes in a namespaced member a reader can find and nothing will misread.
    collection["qgis_mcp"] = {
        "crs": crs,
        "coordinates": "local metres" if origin != [0.0, 0.0] else "projected",
        "local_origin": origin,
        "note": (
            "coordinates are [x, y, z] with z in metres. This is NOT WGS84 "
            "GeoJSON -- see crs."
        ),
    }
    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(collection, handle)
    return {}


def _write_obj(destination, features, origin, crs):
    """Write Wavefront OBJ: ``v`` vertices then ``l`` polylines, 1-indexed.

    Z-up, which is Blender's convention, so easting maps to X, northing to Y
    and elevation to Z with no axis juggling on import.
    """
    lines = [
        "# exported by qgis-mcp for Blender",
        f"# crs: {crs}",
        f"# local origin: {origin[0]:.3f}, {origin[1]:.3f}",
        "# coordinates are metres, Z up",
    ]
    index = 1
    polylines = []
    for item in features:
        for part in item["parts"]:
            start = index
            for x, y, z in part:
                lines.append(f"v {x - origin[0]:.4f} {y - origin[1]:.4f} {z:.4f}")
                index += 1
            if len(part) > 1:
                polylines.append(
                    "l " + " ".join(str(value) for value in range(start, index))
                )
    lines.extend(polylines)

    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return {"obj_polyline_count": len(polylines)}


HANDLERS = {"export_vector_for_mesh": op_export_vector_for_mesh}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _fail(ErrorCode.BAD_REQUEST, "meshexport expects one argument, a request file")
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

    source = request.get("source")
    if not source or not os.path.exists(str(source).split("|", 1)[0]):
        _fail(
            ErrorCode.BAD_REQUEST,
            f"vector source does not exist: {source}",
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
