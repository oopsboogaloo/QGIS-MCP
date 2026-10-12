"""Build the synthetic fixtures declared in ``truth.py``, then verify them.

Task 0. Run it from anywhere:

    python test-data/make_fixtures.py

GDAL, numpy and PyQGIS are all needed, and QGIS bundles all three. Rather than
asking for a second GDAL in the server's virtualenv -- a famously awkward
install on Windows, and a second copy to keep in step with the first -- this
script re-executes itself under the QGIS-bundled interpreter when it finds it
is running somewhere without them. It reuses the same install resolution and
child-environment construction the server uses, so a fixture build that works
is also evidence that the headless path is configured correctly.

**Build, then verify against the declaration.** Every value comes from
``truth.py``; nothing is measured and recorded as expected. After writing, each
file is reopened and checked against those same constants, so a generator bug
fails the build instead of silently redefining what the fixtures are.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
OUTPUT_DIR = SCRIPT_DIR / "generated"

#: Guards the re-exec below against recursing if the QGIS interpreter somehow
#: also lacks GDAL -- better a clear ImportError than an endless fork.
CHILD_MARKER = "QGIS_MCP_FIXTURE_CHILD"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import truth  # noqa: E402  (sys.path must be set first)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _have_gis_stack() -> bool:
    try:
        import numpy  # noqa: F401
        from osgeo import gdal  # noqa: F401
        from qgis.core import QgsApplication  # noqa: F401
    except Exception:
        return False
    return True


def _reexec_under_qgis() -> int:
    """Re-run this script under the QGIS-bundled interpreter."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from qgis_mcp_server.config import Config
        from qgis_mcp_server.qgis_install import QgisNotFound
    except ImportError as exc:
        print(
            f"cannot import the server package to find QGIS ({exc}).\n"
            f"Either run this script under the QGIS Python directly, or run it "
            f"from a checkout where src/ is present.",
            file=sys.stderr,
        )
        return 2

    try:
        install = Config.load().install
    except QgisNotFound as exc:
        print(f"cannot locate QGIS:\n{exc}", file=sys.stderr)
        return 2

    print(f"re-executing under {install.label} ({install.python_exe})", flush=True)
    env = install.child_env()
    env[CHILD_MARKER] = "1"
    completed = subprocess.run(
        [str(install.python_exe), str(Path(__file__).resolve())],
        env=env,
        check=False,
    )
    return completed.returncode


# ---------------------------------------------------------------------------
# Raster fixtures
# ---------------------------------------------------------------------------


def _build_dem_array():
    """The declared pattern, as a float32 array.

    Region order matters: hill adds to the base ramp, the others overwrite it.
    Applied in the same order ``truth.py`` documents.
    """
    import numpy as np

    rows = np.arange(truth.DEM_HEIGHT, dtype="float32").reshape(-1, 1)
    cols = np.arange(truth.DEM_WIDTH, dtype="float32").reshape(1, -1)
    dem = (
        truth.BASE_INTERCEPT
        + truth.BASE_ROW_COEFF * rows
        + truth.BASE_COL_COEFF * cols
    ).astype("float32")

    r0, r1 = truth.HILL_ROWS
    c0, c1 = truth.HILL_COLS
    dem[r0:r1, c0:c1] += truth.HILL_UPLIFT

    r0, r1 = truth.COAST_ROWS
    c0, c1 = truth.COAST_COLS
    dem[r0:r1, c0:c1] = truth.COAST_ELEVATION

    r0, r1 = truth.BASIN_ROWS
    c0, c1 = truth.BASIN_COLS
    dem[r0:r1, c0:c1] = truth.BASIN_ELEVATION

    r0, r1 = truth.NODATA_ROWS
    c0, c1 = truth.NODATA_COLS
    dem[r0:r1, c0:c1] = truth.DEM_NODATA

    return dem


def _write_raster(path: Path, array, *, crs: str, geotransform, nodata=None) -> None:
    from osgeo import gdal, osr

    gdal.UseExceptions()
    driver = gdal.GetDriverByName("GTiff")
    height, width = array.shape
    dataset = driver.Create(str(path), width, height, 1, gdal.GDT_Float32)
    try:
        dataset.SetGeoTransform(geotransform)
        srs = osr.SpatialReference()
        srs.SetFromUserInput(crs)
        dataset.SetProjection(srs.ExportToWkt())
        band = dataset.GetRasterBand(1)
        if nodata is not None:
            band.SetNoDataValue(float(nodata))
        band.WriteArray(array)
        band.FlushCache()
    finally:
        dataset = None


def build_dem() -> Path:
    path = OUTPUT_DIR / truth.DEM_NAME
    geotransform = (
        truth.ORIGIN_EASTING,
        truth.PIXEL_SIZE_M,
        0.0,
        truth.ORIGIN_NORTHING,
        0.0,
        -truth.PIXEL_SIZE_M,
    )
    _write_raster(
        path,
        _build_dem_array(),
        crs=truth.PRIMARY_CRS,
        geotransform=geotransform,
        nodata=truth.DEM_NODATA,
    )
    return path


def build_geographic_dem() -> Path:
    """A small geographic-CRS DEM, for REQ-11.4 and REQ-12 tier 1."""
    import numpy as np

    path = OUTPUT_DIR / truth.GEOGRAPHIC_DEM_NAME
    rows = np.arange(truth.GEOGRAPHIC_DEM_HEIGHT, dtype="float32").reshape(-1, 1)
    cols = np.arange(truth.GEOGRAPHIC_DEM_WIDTH, dtype="float32").reshape(1, -1)
    array = (
        truth.GEOGRAPHIC_DEM_MIN
        + truth.BASE_ROW_COEFF * rows
        + truth.BASE_COL_COEFF * cols
    ).astype("float32")

    min_lon, min_lat, max_lon, max_lat = truth.GEOGRAPHIC_EXTENT
    geotransform = (
        min_lon,
        (max_lon - min_lon) / truth.GEOGRAPHIC_DEM_WIDTH,
        0.0,
        max_lat,
        0.0,
        -(max_lat - min_lat) / truth.GEOGRAPHIC_DEM_HEIGHT,
    )
    _write_raster(
        path, array, crs=truth.GEOGRAPHIC_CRS, geotransform=geotransform
    )
    return path


# ---------------------------------------------------------------------------
# Vector fixtures
# ---------------------------------------------------------------------------


def build_vectors() -> Path:
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    path = OUTPUT_DIR / truth.VECTOR_GPKG_NAME
    if path.exists():
        path.unlink()

    srs = osr.SpatialReference()
    srs.SetFromUserInput(truth.PRIMARY_CRS)
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    if datasource is None:
        raise RuntimeError(f"could not create GeoPackage at {path}")

    field_types = {
        "Integer": ogr.OFTInteger,
        "String": ogr.OFTString,
        "Real": ogr.OFTReal,
    }

    def add_layer(name, geom_type, fields):
        layer = datasource.CreateLayer(name, srs, geom_type)
        for field_name, field_type in fields.items():
            layer.CreateField(ogr.FieldDefn(field_name, field_types[field_type]))
        return layer

    points = add_layer(
        truth.SURVEY_POINTS_LAYER, ogr.wkbPoint, truth.SURVEY_POINTS_FIELDS
    )
    for record in truth.SURVEY_POINTS:
        feature = ogr.Feature(points.GetLayerDefn())
        feature.SetField("id", record["id"])
        feature.SetField("name", record["name"])
        feature.SetField("elevation_m", record["elevation_m"])
        geometry = ogr.Geometry(ogr.wkbPoint)
        geometry.AddPoint_2D(record["x"], record["y"])
        feature.SetGeometry(geometry)
        points.CreateFeature(feature)

    boundary = add_layer(
        truth.AOI_BOUNDARY_LAYER, ogr.wkbPolygon, truth.AOI_BOUNDARY_FIELDS
    )
    feature = ogr.Feature(boundary.GetLayerDefn())
    feature.SetField("id", 1)
    feature.SetField("name", "aoi")
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in truth.AOI_BOUNDARY_RING:
        ring.AddPoint_2D(x, y)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    feature.SetGeometry(polygon)
    boundary.CreateFeature(feature)

    tracks = add_layer(
        truth.TRACKS_LAYER, ogr.wkbLineString, truth.TRACKS_FIELDS
    )
    for record in truth.TRACKS:
        feature = ogr.Feature(tracks.GetLayerDefn())
        feature.SetField("id", record["id"])
        feature.SetField("name", record["name"])
        feature.SetField("surface", record["surface"])
        line = ogr.Geometry(ogr.wkbLineString)
        for x, y in record["vertices"]:
            line.AddPoint_2D(x, y)
        feature.SetGeometry(line)
        tracks.CreateFeature(feature)

    datasource = None
    return path


# ---------------------------------------------------------------------------
# Project fixture
# ---------------------------------------------------------------------------


def build_project(dem_path: Path, gpkg_path: Path) -> Path:
    """Write the fixture ``.qgz`` referencing the fixtures just built.

    Layer paths are written relative to the project, so the generated directory
    stays portable -- a project holding absolute paths would resolve only on
    the machine that built it, and would make REQ-2's "supply a project path"
    contract untestable anywhere else.
    """
    from qgis.core import (
        Qgis,
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsProject,
        QgsRasterLayer,
        QgsVectorLayer,
    )

    path = OUTPUT_DIR / truth.PROJECT_NAME
    if path.exists():
        path.unlink()

    QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", ""), True)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        project = QgsProject.instance()
        project.clear()
        project.setTitle(truth.PROJECT_TITLE)
        project.setCrs(QgsCoordinateReferenceSystem(truth.PROJECT_CRS))
        project.setFilePathStorage(Qgis.FilePathType.Relative)

        raster = QgsRasterLayer(str(dem_path), "dem_su4080")
        if not raster.isValid():
            raise RuntimeError(f"generated DEM did not load as a layer: {dem_path}")
        project.addMapLayer(raster)

        for layer_name in (
            truth.SURVEY_POINTS_LAYER,
            truth.AOI_BOUNDARY_LAYER,
            truth.TRACKS_LAYER,
        ):
            vector = QgsVectorLayer(
                f"{gpkg_path}|layername={layer_name}", layer_name, "ogr"
            )
            if not vector.isValid():
                raise RuntimeError(
                    f"generated layer {layer_name} did not load from {gpkg_path}"
                )
            project.addMapLayer(vector)

        if not project.write(str(path)):
            raise RuntimeError(f"could not write project to {path}")
        project.clear()
    finally:
        app.exitQgis()
    return path


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class VerificationError(AssertionError):
    """A generated fixture does not match ``truth.py``."""


def _equal(expected, actual, tolerance: float) -> bool:
    """Compare, allowing a tolerance only where a float is actually involved."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return abs(float(expected) - float(actual)) <= tolerance
        except (TypeError, ValueError):
            return False
    return expected == actual


def _check(failures: list[str], label: str, expected, actual, *, tolerance=0.0) -> None:
    """Record a mismatch, reporting expected against actual (REQ-T3).

    Collects rather than raising, so one run reports every discrepancy. Fixing
    fixtures one failure per run is how a half-hour job becomes an afternoon.
    """
    if _equal(expected, actual, tolerance):
        return
    failures.append(f"  {label}\n      expected: {expected!r}\n      actual:   {actual!r}")


def verify() -> None:
    import numpy as np
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    # QgsApplication.exitQgis(), called when the project was written, tears down
    # GDAL's driver registry along with everything else QGIS initialised. Any
    # gdal.Open() after that point fails with a bare "No driver registered",
    # which reads as a corrupt fixture rather than a teardown side effect.
    # Re-registering costs nothing and keeps verification independent of
    # whatever ran before it.
    gdal.AllRegister()
    failures: list[str] = []

    # --- primary DEM ---
    dem_path = OUTPUT_DIR / truth.DEM_NAME
    dataset = gdal.Open(str(dem_path))
    _check(failures, "DEM width", truth.DEM_WIDTH, dataset.RasterXSize)
    _check(failures, "DEM height", truth.DEM_HEIGHT, dataset.RasterYSize)
    _check(failures, "DEM band count", truth.DEM_BAND_COUNT, dataset.RasterCount)

    band = dataset.GetRasterBand(1)
    _check(failures, "DEM nodata value", truth.DEM_NODATA, band.GetNoDataValue())
    _check(
        failures,
        "DEM data type",
        "Float32",
        gdal.GetDataTypeName(band.DataType),
    )

    srs_authority = _authority_code(dataset.GetProjection())
    _check(failures, "DEM CRS", truth.PRIMARY_CRS, srs_authority)

    geotransform = dataset.GetGeoTransform()
    _check(failures, "DEM origin easting", truth.ORIGIN_EASTING, geotransform[0])
    _check(failures, "DEM origin northing", truth.ORIGIN_NORTHING, geotransform[3])
    _check(failures, "DEM pixel width", truth.PIXEL_SIZE_M, geotransform[1])
    _check(failures, "DEM pixel height", -truth.PIXEL_SIZE_M, geotransform[5])

    array = band.ReadAsArray()
    nodata_mask = array == truth.DEM_NODATA
    valid = array[~nodata_mask]
    _check(failures, "DEM total cells", truth.DEM_TOTAL_CELLS, int(array.size))
    _check(
        failures,
        "DEM nodata cell count",
        truth.NODATA_CELL_COUNT,
        int(nodata_mask.sum()),
    )
    _check(failures, "DEM valid cell count", truth.VALID_CELL_COUNT, int(valid.size))
    _check(failures, "DEM valid minimum", truth.DEM_VALID_MIN, float(valid.min()))
    _check(failures, "DEM valid maximum", truth.DEM_VALID_MAX, float(valid.max()))
    # The sum checks every valid cell, where min and max check two. A pattern
    # written with a transposed region or an off-by-one still yields a range of
    # -5.0 to 342.0 while summing to something else entirely.
    _check(
        failures,
        "DEM valid sum",
        truth.DEM_VALID_SUM,
        float(valid.sum()),
        tolerance=0.5,
    )

    # The two features that separate a correct terrain implementation from one
    # that merely looks correct, checked as counts rather than assumed present.
    below_sea = valid < truth.SEA_LEVEL_THRESHOLD_M
    _check(
        failures,
        "cells below sea level",
        truth.BATHTUB_FLOODED_CELLS,
        int(below_sea.sum()),
    )
    basin = array[
        truth.BASIN_ROWS[0]:truth.BASIN_ROWS[1],
        truth.BASIN_COLS[0]:truth.BASIN_COLS[1],
    ]
    _check(failures, "basin cell count", truth.BASIN_CELL_COUNT, int(basin.size))
    _check(
        failures,
        "basin is uniformly below sea level",
        True,
        bool((basin == truth.BASIN_ELEVATION).all()),
    )
    coast = array[
        truth.COAST_ROWS[0]:truth.COAST_ROWS[1],
        truth.COAST_COLS[0]:truth.COAST_COLS[1],
    ]
    _check(
        failures,
        "coastal inlet is uniformly below sea level",
        True,
        bool((coast == truth.COAST_ELEVATION).all()),
    )
    _check(
        failures,
        "coastal inlet reaches the west edge",
        True,
        bool((array[truth.COAST_ROWS[0]:truth.COAST_ROWS[1], 0] < 0).all()),
    )

    # The basin must be enclosed by land, or the connectivity test proves
    # nothing. Checked by inspecting the ring of cells around it.
    ring = _surrounding_ring(array, truth.BASIN_ROWS, truth.BASIN_COLS)
    _check(
        failures,
        "lowest land elevation surrounding the basin",
        truth.BASIN_LOWEST_SURROUNDING_ELEVATION,
        float(np.min(ring)),
    )
    _check(
        failures,
        "basin is fully enclosed by land above sea level",
        True,
        bool((ring > truth.SEA_LEVEL_THRESHOLD_M).all()),
    )
    dataset = None

    # --- geographic DEM ---
    geo_path = OUTPUT_DIR / truth.GEOGRAPHIC_DEM_NAME
    dataset = gdal.Open(str(geo_path))
    _check(
        failures, "geographic DEM width", truth.GEOGRAPHIC_DEM_WIDTH, dataset.RasterXSize
    )
    _check(
        failures,
        "geographic DEM height",
        truth.GEOGRAPHIC_DEM_HEIGHT,
        dataset.RasterYSize,
    )
    _check(
        failures,
        "geographic DEM CRS",
        truth.GEOGRAPHIC_CRS,
        _authority_code(dataset.GetProjection()),
    )
    geo_array = dataset.GetRasterBand(1).ReadAsArray()
    _check(
        failures, "geographic DEM minimum", truth.GEOGRAPHIC_DEM_MIN, float(geo_array.min())
    )
    _check(
        failures, "geographic DEM maximum", truth.GEOGRAPHIC_DEM_MAX, float(geo_array.max())
    )
    dataset = None

    # --- vectors ---
    gpkg_path = OUTPUT_DIR / truth.VECTOR_GPKG_NAME
    datasource = ogr.Open(str(gpkg_path))
    _check(
        failures, "GeoPackage layer count", 3, datasource.GetLayerCount()
    )

    expectations = [
        (truth.SURVEY_POINTS_LAYER, truth.SURVEY_POINTS_COUNT, truth.SURVEY_POINTS_FIELDS),
        (truth.AOI_BOUNDARY_LAYER, truth.AOI_BOUNDARY_COUNT, truth.AOI_BOUNDARY_FIELDS),
        (truth.TRACKS_LAYER, truth.TRACKS_COUNT, truth.TRACKS_FIELDS),
    ]
    for layer_name, expected_count, expected_fields in expectations:
        layer = datasource.GetLayerByName(layer_name)
        if layer is None:
            failures.append(f"  layer {layer_name} is missing from {gpkg_path.name}")
            continue
        _check(
            failures, f"{layer_name} feature count", expected_count, layer.GetFeatureCount()
        )
        definition = layer.GetLayerDefn()
        actual_fields = {
            definition.GetFieldDefn(index).GetName(): definition.GetFieldDefn(
                index
            ).GetTypeName()
            for index in range(definition.GetFieldCount())
        }
        _check(
            failures,
            f"{layer_name} field schema",
            dict(expected_fields),
            actual_fields,
        )

    boundary_layer = datasource.GetLayerByName(truth.AOI_BOUNDARY_LAYER)
    boundary_feature = boundary_layer.GetNextFeature()
    _check(
        failures,
        "AOI boundary area (m2)",
        truth.AOI_BOUNDARY_AREA_M2,
        boundary_feature.GetGeometryRef().GetArea(),
        tolerance=1e-6,
    )

    tracks_layer = datasource.GetLayerByName(truth.TRACKS_LAYER)
    total_length = 0.0
    by_name = {}
    for feature in tracks_layer:
        length = feature.GetGeometryRef().Length()
        by_name[feature.GetField("name")] = length
        total_length += length
    for record in truth.TRACKS:
        _check(
            failures,
            f"{record['name']} length (m)",
            record["length_m"],
            by_name.get(record["name"]),
            tolerance=1e-6,
        )
    _check(
        failures,
        "total track length (m)",
        truth.TRACKS_TOTAL_LENGTH_M,
        total_length,
        tolerance=1e-6,
    )

    # The documented clip expectation, verified against the real geometry so
    # that task 4.2 can assert "2 points inside" without re-deriving it.
    points_layer = datasource.GetLayerByName(truth.SURVEY_POINTS_LAYER)
    boundary_geometry = boundary_feature.GetGeometryRef()
    inside = sorted(
        feature.GetField("name")
        for feature in points_layer
        if feature.GetGeometryRef().Within(boundary_geometry)
    )
    _check(
        failures,
        "survey points inside the AOI boundary",
        sorted(truth.SURVEY_POINTS_INSIDE_AOI),
        inside,
    )
    datasource = None

    # --- project ---
    project_path = OUTPUT_DIR / truth.PROJECT_NAME
    if not project_path.exists():
        failures.append(f"  project file missing: {project_path}")

    if failures:
        raise VerificationError(
            "generated fixtures do not match truth.py:\n" + "\n".join(failures)
        )


def _surrounding_ring(array, rows, cols):
    """The one-cell ring immediately outside a rectangular region."""
    import numpy as np

    r0, r1 = rows
    c0, c1 = cols
    return np.concatenate(
        [
            array[r0 - 1, c0 - 1:c1 + 1],
            array[r1, c0 - 1:c1 + 1],
            array[r0:r1, c0 - 1],
            array[r0:r1, c1],
        ]
    )


def _authority_code(wkt: str) -> str | None:
    """``AUTHORITY:CODE`` for a WKT projection, or ``None``."""
    from osgeo import osr

    if not wkt:
        return None
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    authority = srs.GetAuthorityName(None)
    code = srs.GetAuthorityCode(None)
    if authority and code:
        return f"{authority}:{code}"
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"writing fixtures to {OUTPUT_DIR}")

    dem_path = build_dem()
    print(f"  {dem_path.name}")
    geo_path = build_geographic_dem()
    print(f"  {geo_path.name}")
    gpkg_path = build_vectors()
    print(f"  {gpkg_path.name}")
    project_path = build_project(dem_path, gpkg_path)
    print(f"  {project_path.name}")

    print("verifying against truth.py ...")
    verify()
    print("all fixture properties match the declared truth.")
    return 0


if __name__ == "__main__":
    if not _have_gis_stack():
        if os.environ.get(CHILD_MARKER):
            print(
                "the QGIS interpreter is missing numpy, GDAL or PyQGIS -- the "
                "install looks incomplete.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        raise SystemExit(_reexec_under_qgis())
    raise SystemExit(main())
