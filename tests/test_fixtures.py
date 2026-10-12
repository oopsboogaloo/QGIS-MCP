"""Task 0: the synthetic fixtures match their declared truth.

``make_fixtures.py`` already verifies its output as it writes. This suite
re-checks the same properties for a different reason: it fails when the
fixtures on disk are *stale* -- generated from an older ``truth.py``, or
half-written -- which the generator cannot detect because it is not running.
Without this, a later task would assert against drifted fixtures and the
mismatch would be blamed on the tool under test.

Properties are read through the headless path (REQ-T2), in batched calls --
each child costs a QGIS startup, so one call gathers many properties and the
assertions are made here where a failure can name the field.
"""

from __future__ import annotations

import pytest

from conftest import assert_ok, describe


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dem_properties(runner, fixture_dem, fixture_truth):
    """Every DEM property the suite asserts, from one child process.

    Read with GDAL and numpy rather than ``QgsRasterBandStats``, because the
    nodata handling is the thing under test: QGIS excludes nodata from its
    statistics automatically, which would hide whether the sentinel is where
    ``truth.py`` says it is.
    """
    code = f"""
import json
import numpy as np
from osgeo import gdal, osr

dataset = gdal.Open({str(fixture_dem)!r})
band = dataset.GetRasterBand(1)
array = band.ReadAsArray()
nodata = band.GetNoDataValue()
mask = array == nodata
valid = array[~mask]

srs = osr.SpatialReference()
srs.ImportFromWkt(dataset.GetProjection())
gt = dataset.GetGeoTransform()

basin = array[{fixture_truth.BASIN_ROWS[0]}:{fixture_truth.BASIN_ROWS[1]},
              {fixture_truth.BASIN_COLS[0]}:{fixture_truth.BASIN_COLS[1]}]
ring = np.concatenate([
    array[{fixture_truth.BASIN_ROWS[0]} - 1,
          {fixture_truth.BASIN_COLS[0]} - 1:{fixture_truth.BASIN_COLS[1]} + 1],
    array[{fixture_truth.BASIN_ROWS[1]},
          {fixture_truth.BASIN_COLS[0]} - 1:{fixture_truth.BASIN_COLS[1]} + 1],
    array[{fixture_truth.BASIN_ROWS[0]}:{fixture_truth.BASIN_ROWS[1]},
          {fixture_truth.BASIN_COLS[0]} - 1],
    array[{fixture_truth.BASIN_ROWS[0]}:{fixture_truth.BASIN_ROWS[1]},
          {fixture_truth.BASIN_COLS[1]}],
])

result = {{
    "width": dataset.RasterXSize,
    "height": dataset.RasterYSize,
    "bands": dataset.RasterCount,
    "dtype": gdal.GetDataTypeName(band.DataType),
    "nodata": float(nodata),
    "crs": "{{}}:{{}}".format(srs.GetAuthorityName(None), srs.GetAuthorityCode(None)),
    "origin_easting": gt[0],
    "origin_northing": gt[3],
    "pixel_width": gt[1],
    "pixel_height": gt[5],
    "total_cells": int(array.size),
    "nodata_cells": int(mask.sum()),
    "valid_cells": int(valid.size),
    "valid_min": float(valid.min()),
    "valid_max": float(valid.max()),
    "cells_below_sea_level": int((valid < 0).sum()),
    "basin_cells": int(basin.size),
    "basin_all_below_sea_level": bool((basin == {fixture_truth.BASIN_ELEVATION}).all()),
    "basin_ring_min": float(ring.min()),
    "basin_ring_all_land": bool((ring > 0).all()),
}}
"""
    envelope = runner.run_code(code)
    assert_ok(envelope, context_note="probing the DEM fixture")
    return envelope.result


@pytest.fixture(scope="module")
def vector_properties(runner, fixture_gpkg, fixture_truth):
    """Vector layer counts, schemas and exact geometry measures."""
    code = f"""
from osgeo import ogr

datasource = ogr.Open({str(fixture_gpkg)!r})
layers = {{}}
for index in range(datasource.GetLayerCount()):
    layer = datasource.GetLayerByIndex(index)
    definition = layer.GetLayerDefn()
    layers[layer.GetName()] = {{
        "count": layer.GetFeatureCount(),
        "fields": {{
            definition.GetFieldDefn(i).GetName(): definition.GetFieldDefn(i).GetTypeName()
            for i in range(definition.GetFieldCount())
        }},
    }}

boundary = datasource.GetLayerByName({fixture_truth.AOI_BOUNDARY_LAYER!r})
boundary_feature = boundary.GetNextFeature()
boundary_geometry = boundary_feature.GetGeometryRef()

tracks = datasource.GetLayerByName({fixture_truth.TRACKS_LAYER!r})
track_lengths = {{}}
for feature in tracks:
    track_lengths[feature.GetField("name")] = feature.GetGeometryRef().Length()

points = datasource.GetLayerByName({fixture_truth.SURVEY_POINTS_LAYER!r})
inside = []
attributes = {{}}
for feature in points:
    name = feature.GetField("name")
    attributes[name] = feature.GetField("elevation_m")
    if feature.GetGeometryRef().Within(boundary_geometry):
        inside.append(name)

result = {{
    "layers": layers,
    "boundary_area": boundary_geometry.GetArea(),
    "boundary_perimeter": boundary_geometry.Boundary().Length(),
    "track_lengths": track_lengths,
    "point_elevations": attributes,
    "points_inside_boundary": sorted(inside),
}}
"""
    envelope = runner.run_code(code)
    assert_ok(envelope, context_note="probing the vector fixtures")
    return envelope.result


# ---------------------------------------------------------------------------
# DEM structure
# ---------------------------------------------------------------------------


def test_dem_grid_and_georeferencing(dem_properties, fixture_truth):
    expected = {
        "width": fixture_truth.DEM_WIDTH,
        "height": fixture_truth.DEM_HEIGHT,
        "bands": fixture_truth.DEM_BAND_COUNT,
        "dtype": "Float32",
        "crs": fixture_truth.PRIMARY_CRS,
        "origin_easting": fixture_truth.ORIGIN_EASTING,
        "origin_northing": fixture_truth.ORIGIN_NORTHING,
        "pixel_width": fixture_truth.PIXEL_SIZE_M,
        "pixel_height": -fixture_truth.PIXEL_SIZE_M,
        "nodata": fixture_truth.DEM_NODATA,
    }
    actual = {key: dem_properties[key] for key in expected}
    assert actual == expected, (
        f"DEM georeferencing drifted from truth.py\n"
        f"    expected: {describe(expected)}\n"
        f"    actual:   {describe(actual)}"
    )


def test_dem_cell_counts(dem_properties, fixture_truth):
    expected = {
        "total_cells": fixture_truth.DEM_TOTAL_CELLS,
        "nodata_cells": fixture_truth.NODATA_CELL_COUNT,
        "valid_cells": fixture_truth.VALID_CELL_COUNT,
    }
    actual = {key: dem_properties[key] for key in expected}
    assert actual == expected, (
        f"DEM cell counts drifted from truth.py\n"
        f"    expected: {describe(expected)}\n"
        f"    actual:   {describe(actual)}"
    )


def test_dem_has_nodata_cells(dem_properties, fixture_truth):
    """Task 0 requires nodata cells, not merely permits them.

    They are what catches REQ-11's normalisation trap. A clean synthetic DEM
    lets that bug through undetected, so their absence is a fixture failure
    rather than a cosmetic difference.
    """
    assert dem_properties["nodata_cells"] == fixture_truth.NODATA_CELL_COUNT, (
        f"expected exactly {fixture_truth.NODATA_CELL_COUNT} nodata cells, got "
        f"{dem_properties['nodata_cells']}"
    )
    assert dem_properties["nodata_cells"] > 0, "the DEM must contain nodata cells"


def test_dem_elevation_range_excludes_nodata(dem_properties, fixture_truth):
    """The REQ-11 trap, asserted from both directions.

    The valid range must be the real one, and must not be the one a naive
    implementation would compute by leaving the sentinel in.
    """
    assert dem_properties["valid_min"] == fixture_truth.DEM_VALID_MIN, (
        f"valid minimum mismatch\n"
        f"    expected: {fixture_truth.DEM_VALID_MIN}\n"
        f"    actual:   {dem_properties['valid_min']}"
    )
    assert dem_properties["valid_max"] == fixture_truth.DEM_VALID_MAX, (
        f"valid maximum mismatch\n"
        f"    expected: {fixture_truth.DEM_VALID_MAX}\n"
        f"    actual:   {dem_properties['valid_max']}"
    )
    assert dem_properties["valid_min"] != fixture_truth.DEM_NODATA, (
        f"the valid minimum is the nodata sentinel ({fixture_truth.DEM_NODATA}), "
        f"which means nodata was not excluded -- exactly the REQ-11 failure the "
        f"fixture exists to catch."
    )


def test_dem_displacement_ratio_is_derivable(dem_properties, fixture_truth):
    """REQ-11 part 3: the ratio for a plane one Blender unit wide.

    Computed here from the probed range rather than read from truth.py, so the
    test confirms the declared ratio follows from the actual fixture.
    """
    derived = (
        dem_properties["valid_max"] - dem_properties["valid_min"]
    ) / fixture_truth.EXTENT_WIDTH_M
    assert derived == pytest.approx(fixture_truth.DEM_DISPLACEMENT_RATIO), (
        f"displacement ratio mismatch\n"
        f"    expected: {fixture_truth.DEM_DISPLACEMENT_RATIO}\n"
        f"    actual:   {derived}"
    )


# ---------------------------------------------------------------------------
# Sea level and connectivity
# ---------------------------------------------------------------------------


def test_bathtub_flood_extent(dem_properties, fixture_truth):
    assert dem_properties["cells_below_sea_level"] == fixture_truth.BATHTUB_FLOODED_CELLS, (
        f"cells below sea level mismatch\n"
        f"    expected: {fixture_truth.BATHTUB_FLOODED_CELLS}\n"
        f"    actual:   {dem_properties['cells_below_sea_level']}"
    )


def test_landlocked_basin_is_present_and_enclosed(dem_properties, fixture_truth):
    """Task 0's second required DEM feature.

    The basin is the only way to test ``model_sea_level_change``'s
    ``connectivity`` option in both directions, and it only works if it is
    genuinely enclosed -- so the enclosing ring is checked, not assumed.
    """
    assert dem_properties["basin_cells"] == fixture_truth.BASIN_CELL_COUNT, describe(
        dem_properties
    )
    assert dem_properties["basin_all_below_sea_level"] is True, (
        f"the basin must be uniformly below sea level\n{describe(dem_properties)}"
    )
    assert dem_properties["basin_ring_all_land"] is True, (
        f"the basin must be fully ringed by land above sea level, or "
        f"ocean-connected flooding would reach it and the connectivity test "
        f"would prove nothing.\n{describe(dem_properties)}"
    )
    assert (
        dem_properties["basin_ring_min"]
        == fixture_truth.BASIN_LOWEST_SURROUNDING_ELEVATION
    ), (
        f"lowest surrounding elevation mismatch\n"
        f"    expected: {fixture_truth.BASIN_LOWEST_SURROUNDING_ELEVATION}\n"
        f"    actual:   {dem_properties['basin_ring_min']}"
    )


def test_connectivity_modes_differ_by_exactly_the_basin(dem_properties, fixture_truth):
    """The number task 5.4 will assert against.

    Bathtub floods everything below the threshold; ocean-connected floods only
    what the sea reaches. The difference is the basin, and nothing else.
    """
    difference = (
        dem_properties["cells_below_sea_level"]
        - fixture_truth.OCEAN_CONNECTED_FLOODED_CELLS
    )
    assert difference == fixture_truth.CONNECTIVITY_DIFFERENCE_CELLS, (
        f"the two connectivity modes must differ by exactly the basin\n"
        f"    expected difference: {fixture_truth.CONNECTIVITY_DIFFERENCE_CELLS} cells\n"
        f"    actual difference:   {difference} cells"
    )


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------


def test_vector_layers_and_schemas(vector_properties, fixture_truth):
    expected = {
        fixture_truth.SURVEY_POINTS_LAYER: {
            "count": fixture_truth.SURVEY_POINTS_COUNT,
            "fields": dict(fixture_truth.SURVEY_POINTS_FIELDS),
        },
        fixture_truth.AOI_BOUNDARY_LAYER: {
            "count": fixture_truth.AOI_BOUNDARY_COUNT,
            "fields": dict(fixture_truth.AOI_BOUNDARY_FIELDS),
        },
        fixture_truth.TRACKS_LAYER: {
            "count": fixture_truth.TRACKS_COUNT,
            "fields": dict(fixture_truth.TRACKS_FIELDS),
        },
    }
    assert vector_properties["layers"] == expected, (
        f"vector layers drifted from truth.py\n"
        f"    expected: {describe(expected)}\n"
        f"    actual:   {describe(vector_properties['layers'])}"
    )


def test_aoi_boundary_measures_are_exact(vector_properties, fixture_truth):
    """A clip mask whose area depends on rounding cannot test a clipper."""
    assert vector_properties["boundary_area"] == pytest.approx(
        fixture_truth.AOI_BOUNDARY_AREA_M2
    ), (
        f"boundary area mismatch\n"
        f"    expected: {fixture_truth.AOI_BOUNDARY_AREA_M2}\n"
        f"    actual:   {vector_properties['boundary_area']}"
    )
    assert vector_properties["boundary_perimeter"] == pytest.approx(
        fixture_truth.AOI_BOUNDARY_PERIMETER_M
    ), (
        f"boundary perimeter mismatch\n"
        f"    expected: {fixture_truth.AOI_BOUNDARY_PERIMETER_M}\n"
        f"    actual:   {vector_properties['boundary_perimeter']}"
    )


def test_track_lengths_are_exact(vector_properties, fixture_truth):
    for record in fixture_truth.TRACKS:
        actual = vector_properties["track_lengths"].get(record["name"])
        assert actual == pytest.approx(record["length_m"]), (
            f"{record['name']} length mismatch\n"
            f"    expected: {record['length_m']}\n"
            f"    actual:   {actual}"
        )


def test_survey_point_attributes_match(vector_properties, fixture_truth):
    expected = {
        record["name"]: record["elevation_m"] for record in fixture_truth.SURVEY_POINTS
    }
    assert vector_properties["point_elevations"] == expected, (
        f"survey point attributes drifted from truth.py\n"
        f"    expected: {describe(expected)}\n"
        f"    actual:   {describe(vector_properties['point_elevations'])}"
    )


def test_documented_clip_expectation_holds(vector_properties, fixture_truth):
    """The number task 4.2 asserts against: 2 of 5 points inside the boundary."""
    assert (
        vector_properties["points_inside_boundary"]
        == sorted(fixture_truth.SURVEY_POINTS_INSIDE_AOI)
    ), (
        f"points inside the AOI boundary drifted from truth.py\n"
        f"    expected: {sorted(fixture_truth.SURVEY_POINTS_INSIDE_AOI)}\n"
        f"    actual:   {vector_properties['points_inside_boundary']}"
    )
    assert (
        len(vector_properties["points_inside_boundary"])
        == fixture_truth.SURVEY_POINTS_INSIDE_AOI_COUNT
    )


# ---------------------------------------------------------------------------
# The geographic DEM
# ---------------------------------------------------------------------------


def test_geographic_dem_is_geographic(runner, fixture_dir, fixture_truth):
    """REQ-11 part 4 needs a geographic DEM to reject; REQ-12 tier 1 needs a UK
    one. Confirm it is both, since a fixture quietly written in a projected CRS
    would make both tests pass for the wrong reason."""
    path = fixture_dir / fixture_truth.GEOGRAPHIC_DEM_NAME
    envelope = runner.run_code(
        f"from qgis.core import QgsRasterLayer\n"
        f"layer = QgsRasterLayer({str(path)!r}, 'geo')\n"
        f"crs = layer.crs()\n"
        f"extent = layer.extent()\n"
        f"result = {{\n"
        f"    'valid': layer.isValid(),\n"
        f"    'authid': crs.authid(),\n"
        f"    'geographic': crs.isGeographic(),\n"
        f"    'width': layer.width(),\n"
        f"    'height': layer.height(),\n"
        f"    'xmin': extent.xMinimum(),\n"
        f"    'ymin': extent.yMinimum(),\n"
        f"}}\n"
    )
    assert_ok(envelope, context_note="probing the geographic DEM")
    result = envelope.result

    assert result["valid"] is True, describe(result)
    assert result["authid"] == fixture_truth.GEOGRAPHIC_CRS, describe(result)
    assert result["geographic"] is True, (
        f"the geographic fixture must report a geographic CRS, or REQ-11.4 "
        f"would be tested against a projected raster.\n{describe(result)}"
    )
    assert result["width"] == fixture_truth.GEOGRAPHIC_DEM_WIDTH, describe(result)
    assert result["height"] == fixture_truth.GEOGRAPHIC_DEM_HEIGHT, describe(result)
    assert result["xmin"] == pytest.approx(
        fixture_truth.GEOGRAPHIC_EXTENT[0]
    ), describe(result)
    assert result["ymin"] == pytest.approx(
        fixture_truth.GEOGRAPHIC_EXTENT[1]
    ), describe(result)


# ---------------------------------------------------------------------------
# The project
# ---------------------------------------------------------------------------


def test_project_layers_all_resolve(runner, fixture_project, fixture_truth):
    """Every referenced layer must actually load.

    A project whose layers are written with absolute paths loads only on the
    machine that built it, and ``get_missing_layers`` in task 2 would have
    nothing meaningful to report against a fixture that is broken everywhere.
    """
    envelope = runner.run_code(
        "from qgis.core import QgsProject\n"
        "project = QgsProject.instance()\n"
        "result = {\n"
        "    'layers': sorted(l.name() for l in project.mapLayers().values()),\n"
        "    'invalid': sorted(\n"
        "        l.name() for l in project.mapLayers().values() if not l.isValid()\n"
        "    ),\n"
        "    'crs': project.crs().authid(),\n"
        "}\n",
        project_path=fixture_project,
    )
    assert_ok(envelope, context_note="loading the fixture project")
    result = envelope.result

    assert result["invalid"] == [], (
        f"these fixture layers failed to resolve: {result['invalid']}\n"
        f"regenerate with: python test-data/make_fixtures.py"
    )
    assert result["layers"] == sorted(fixture_truth.PROJECT_LAYER_NAMES), (
        f"project layers drifted from truth.py\n"
        f"    expected: {sorted(fixture_truth.PROJECT_LAYER_NAMES)}\n"
        f"    actual:   {result['layers']}"
    )
    assert result["crs"] == fixture_truth.PROJECT_CRS, describe(result)
