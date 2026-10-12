"""Recorded truth for the synthetic fixtures. Hand-computed, not derived.

Task 0's acceptance condition is that "every fixture property a later test will
assert against is documented alongside the fixture ... so tests assert against
recorded truth rather than against whatever the file happens to contain."

That rules out the obvious shortcut. If the generator wrote a manifest by
measuring its own output, and tests then asserted against that manifest, the
suite would prove only that measurement is repeatable -- a generator bug would
be recorded as truth and verified against itself. So the numbers below are
derived by hand from the declared pattern, this module is the single source
tests import, and ``make_fixtures.py`` both builds *from* these constants and
verifies its output *against* them. A mismatch fails fixture generation rather
than quietly becoming the new expectation.

Everything here is exact. The DEM pattern is integer arithmetic on float32
values with no interpolation anywhere, so there are no floating-point
tolerances to negotiate except where noted.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Shared georeferencing
# ---------------------------------------------------------------------------

#: The main fixtures sit on a real Ordnance Survey 1 km grid square, SU4080,
#: so CRS tests have a real-world answer (REQ-12 tier 1 resolves a UK AOI to
#: EPSG:27700) without needing a download. The *values* are synthetic; only the
#: coordinates are real.
GRID_SQUARE = "SU4080"
PRIMARY_CRS = "EPSG:27700"

#: Top-left corner, easting/northing. GDAL geotransforms are north-up, so this
#: is the *north* edge and row 0 of the DEM is the northernmost row.
ORIGIN_EASTING = 440000.0
ORIGIN_NORTHING = 181000.0

PIXEL_SIZE_M = 20.0
DEM_WIDTH = 50
DEM_HEIGHT = 50

#: 50 cells x 20 m = 1000 m square.
EXTENT_WIDTH_M = DEM_WIDTH * PIXEL_SIZE_M
EXTENT_HEIGHT_M = DEM_HEIGHT * PIXEL_SIZE_M
EXTENT = (
    ORIGIN_EASTING,
    ORIGIN_NORTHING - EXTENT_HEIGHT_M,
    ORIGIN_EASTING + EXTENT_WIDTH_M,
    ORIGIN_NORTHING,
)
CELL_AREA_M2 = PIXEL_SIZE_M * PIXEL_SIZE_M

# ---------------------------------------------------------------------------
# The primary DEM: test-data/generated/dem_su4080_27700.tif
# ---------------------------------------------------------------------------
#
# The pattern, in row/column terms with row 0 northernmost:
#
#     base[row, col] = 100.0 + 2.0 * row + 1.0 * col
#
# then, in this order:
#
#     hill    rows 10:15, cols 10:15   base + 200.0
#     coast   rows 20:30, cols  0:5    = -3.0   (touches the west edge)
#     basin   rows 30:35, cols 30:35   = -5.0   (enclosed by land)
#     nodata  rows  0:3,  cols 47:50   = NODATA
#
# The four regions do not overlap, which is what makes every count below a
# simple multiplication.

DEM_NAME = "dem_su4080_27700.tif"
DEM_NODATA = -9999.0
DEM_DTYPE = "float32"
DEM_BAND_COUNT = 1

BASE_INTERCEPT = 100.0
BASE_ROW_COEFF = 2.0
BASE_COL_COEFF = 1.0

HILL_ROWS = (10, 15)
HILL_COLS = (10, 15)
HILL_UPLIFT = 200.0

COAST_ROWS = (20, 30)
COAST_COLS = (0, 5)
COAST_ELEVATION = -3.0

BASIN_ROWS = (30, 35)
BASIN_COLS = (30, 35)
BASIN_ELEVATION = -5.0

NODATA_ROWS = (0, 3)
NODATA_COLS = (47, 50)

DEM_TOTAL_CELLS = DEM_WIDTH * DEM_HEIGHT                       # 2500
NODATA_CELL_COUNT = 3 * 3                                      # 9
VALID_CELL_COUNT = DEM_TOTAL_CELLS - NODATA_CELL_COUNT         # 2491

HILL_CELL_COUNT = 5 * 5                                        # 25
COAST_CELL_COUNT = 10 * 5                                      # 50
BASIN_CELL_COUNT = 5 * 5                                       # 25

#: Minimum over *valid* cells: the basin floor. The nodata sentinel (-9999) is
#: lower, which is the point -- see ``REQ11_NODATA_TRAP`` below.
DEM_VALID_MIN = BASIN_ELEVATION                                # -5.0

#: Maximum: the hill's south-east corner, row 14 / col 14.
#:     100.0 + 2.0 * 14 + 1.0 * 14 + 200.0 = 342.0
#: The base ramp's own maximum, at row 49 / col 49, is only 247.0.
DEM_VALID_MAX = (
    BASE_INTERCEPT
    + BASE_ROW_COEFF * (HILL_ROWS[1] - 1)
    + BASE_COL_COEFF * (HILL_COLS[1] - 1)
    + HILL_UPLIFT
)                                                              # 342.0
DEM_BASE_RAMP_MAX = (
    BASE_INTERCEPT
    + BASE_ROW_COEFF * (DEM_HEIGHT - 1)
    + BASE_COL_COEFF * (DEM_WIDTH - 1)
)                                                              # 247.0

DEM_VALID_RANGE = DEM_VALID_MAX - DEM_VALID_MIN                # 347.0

#: REQ-11 part 3: the displacement ratio for a plane one Blender unit wide.
#:     (342.0 - -5.0) / 1000.0 = 0.347
DEM_DISPLACEMENT_RATIO = (DEM_VALID_MAX - DEM_VALID_MIN) / EXTENT_WIDTH_M


def _base_sum(rows: tuple[int, int], cols: tuple[int, int]) -> float:
    """Sum of the *base ramp* over a half-open rectangular region.

    Computed from the declared pattern, never from a generated file -- which is
    what keeps this module a declaration rather than a measurement. Written as
    a loop because the closed form is easy to get subtly wrong by hand, and a
    wrong constant here would be indistinguishable from a correct one.
    """
    return float(
        sum(
            BASE_INTERCEPT + BASE_ROW_COEFF * row + BASE_COL_COEFF * col
            for row in range(*rows)
            for col in range(*cols)
        )
    )


_ALL = ((0, DEM_HEIGHT), (0, DEM_WIDTH))

#: Sum over valid cells.
#:
#: Worth having because it checks every cell, where the min and max check only
#: two. A pattern written with a transposed region or an off-by-one would still
#: produce a range of -5.0 to 342.0 while summing to something else entirely.
#:
#: base ramp over the whole grid            433750
#:   + hill uplift (200 x 25 cells)          +5000
#:   - coastal inlet replaced by -3.0        -7700
#:   - basin replaced by -5.0                -5025
#:   - nodata region excluded                -1350
#:                                        = 424675
DEM_VALID_SUM = (
    _base_sum(*_ALL)
    + HILL_UPLIFT * HILL_CELL_COUNT
    - _base_sum(COAST_ROWS, COAST_COLS)
    + COAST_ELEVATION * COAST_CELL_COUNT
    - _base_sum(BASIN_ROWS, BASIN_COLS)
    + BASIN_ELEVATION * BASIN_CELL_COUNT
    - _base_sum(NODATA_ROWS, NODATA_COLS)
)                                                              # 424675.0

#: Mean over valid cells, as Processing's raster statistics reports it.
DEM_VALID_MEAN = DEM_VALID_SUM / VALID_CELL_COUNT

#: The REQ-11 trap, stated as a number so a test can assert the *wrong* answer
#: is not being produced. An implementation that normalises without masking
#: nodata spans -9999.0 to 342.0 instead of -5.0 to 342.0, which compresses all
#: real terrain into (342 - -5) / (342 - -9999) of the output range -- about
#: 3.4%. Rendered, that is a flat plane. It is not visible in the file metadata.
REQ11_NODATA_TRAP = {
    "naive_min": DEM_NODATA,
    "correct_min": DEM_VALID_MIN,
    "terrain_fraction_of_range_if_naive": (DEM_VALID_MAX - DEM_VALID_MIN)
    / (DEM_VALID_MAX - DEM_NODATA),
}

# ---------------------------------------------------------------------------
# Sea-level modelling truth (REQ-13 / task 5.4)
# ---------------------------------------------------------------------------
#
# Task 0 requires a landlocked basin below sea level because it is "the only way
# to test model_sea_level_change's connectivity option in both directions". The
# coastal inlet is its counterpart: without somewhere the sea actually reaches,
# ocean-connected and bathtub would agree trivially on every input and the test
# would pass while proving nothing.

SEA_LEVEL_THRESHOLD_M = 0.0

#: Bathtub floods everything below the threshold, connected or not.
BATHTUB_FLOODED_CELLS = COAST_CELL_COUNT + BASIN_CELL_COUNT    # 75
BATHTUB_FLOODED_AREA_M2 = BATHTUB_FLOODED_CELLS * CELL_AREA_M2 # 30000.0

#: Ocean-connected floods only what the sea can reach from the DEM edge. The
#: basin is ringed by land of at least 188 m, so it stays dry.
OCEAN_CONNECTED_FLOODED_CELLS = COAST_CELL_COUNT               # 50
OCEAN_CONNECTED_FLOODED_AREA_M2 = OCEAN_CONNECTED_FLOODED_CELLS * CELL_AREA_M2

#: The difference between the two modes is exactly the basin. A test asserting
#: this catches an implementation that ignores `connectivity` outright.
CONNECTIVITY_DIFFERENCE_CELLS = BASIN_CELL_COUNT               # 25

#: Lowest land elevation in the ring enclosing the basin, at its north-west
#: diagonal neighbour -- row 29 / col 29:
#:     100.0 + 2.0 * 29 + 1.0 * 29 = 187.0
#: The ring is 8-connected, so the diagonal corners count. They are also the
#: lowest cells in it, which is the reason to be explicit: an enclosure checked
#: only against the four edge-adjacent runs would be checking a ring one cell
#: higher than the real one.
BASIN_LOWEST_SURROUNDING_ELEVATION = (
    BASE_INTERCEPT + BASE_ROW_COEFF * 29 + BASE_COL_COEFF * 29
)                                                              # 187.0

#: No vertical datum is declared on the synthetic DEM, which makes it the
#: fixture for REQ-13's "unknown datum" warning path.
DEM_VERTICAL_DATUM = None

# ---------------------------------------------------------------------------
# The geographic DEM: test-data/generated/dem_wgs84_geographic.tif
# ---------------------------------------------------------------------------
#
# Serves two narrow purposes: REQ-11 part 4 (a geographic-CRS DEM must be
# rejected or reprojected, never turned into a ratio computed from degrees) and
# REQ-12 tier 1 (a UK AOI resolves to EPSG:27700).
#
# Its bounds are *declared* in 4326 rather than transformed from the 27700
# extent. Transforming would put computed lat/lon in a file that is supposed to
# hold hand-checkable constants, and would invite tests to assume the two DEMs
# are co-registered. They are not, and nothing should rely on them overlapping.

GEOGRAPHIC_DEM_NAME = "dem_wgs84_geographic.tif"
GEOGRAPHIC_CRS = "EPSG:4326"
GEOGRAPHIC_DEM_WIDTH = 20
GEOGRAPHIC_DEM_HEIGHT = 20
#: Over land in Hampshire, England -- inside the UK, so tier 1 applies.
GEOGRAPHIC_EXTENT = (-1.2700, 51.1700, -1.2550, 51.1800)
GEOGRAPHIC_DEM_MIN = 50.0
GEOGRAPHIC_DEM_MAX = (
    GEOGRAPHIC_DEM_MIN
    + BASE_ROW_COEFF * (GEOGRAPHIC_DEM_HEIGHT - 1)
    + BASE_COL_COEFF * (GEOGRAPHIC_DEM_WIDTH - 1)
)                                                              # 107.0
GEOGRAPHIC_DEM_HAS_NODATA = False

# ---------------------------------------------------------------------------
# Vector fixtures: test-data/generated/vectors.gpkg
# ---------------------------------------------------------------------------

VECTOR_GPKG_NAME = "vectors.gpkg"

#: Attribute values are independent data, deliberately *not* sampled from the
#: DEM. Tying them to it would couple two fixtures and make an error in one
#: look like truth in the other.
SURVEY_POINTS_LAYER = "survey_points"
SURVEY_POINTS_FIELDS = {"id": "Integer", "name": "String", "elevation_m": "Real"}
SURVEY_POINTS = [
    {"id": 1, "name": "Alpha", "elevation_m": 120.5, "x": 440100.0, "y": 180100.0},
    {"id": 2, "name": "Bravo", "elevation_m": 245.0, "x": 440300.0, "y": 180500.0},
    {"id": 3, "name": "Charlie", "elevation_m": 318.25, "x": 440500.0, "y": 180500.0},
    {"id": 4, "name": "Delta", "elevation_m": 95.75, "x": 440700.0, "y": 180200.0},
    {"id": 5, "name": "Echo", "elevation_m": 160.0, "x": 440900.0, "y": 180900.0},
]
SURVEY_POINTS_COUNT = len(SURVEY_POINTS)                       # 5

#: A clip mask for task 4.2. 400 m x 300 m, so its area is exact.
AOI_BOUNDARY_LAYER = "aoi_boundary"
AOI_BOUNDARY_FIELDS = {"id": "Integer", "name": "String"}
AOI_BOUNDARY_RING = [
    (440200.0, 180300.0),
    (440600.0, 180300.0),
    (440600.0, 180600.0),
    (440200.0, 180600.0),
    (440200.0, 180300.0),
]
AOI_BOUNDARY_COUNT = 1
AOI_BOUNDARY_WIDTH_M = 400.0
AOI_BOUNDARY_HEIGHT_M = 300.0
AOI_BOUNDARY_AREA_M2 = AOI_BOUNDARY_WIDTH_M * AOI_BOUNDARY_HEIGHT_M   # 120000.0
AOI_BOUNDARY_PERIMETER_M = 2 * (AOI_BOUNDARY_WIDTH_M + AOI_BOUNDARY_HEIGHT_M)

#: Bravo (440300, 180500) and Charlie (440500, 180500) fall inside; Alpha,
#: Delta and Echo do not. Clipping the points to the boundary must leave 2.
SURVEY_POINTS_INSIDE_AOI = ["Bravo", "Charlie"]
SURVEY_POINTS_INSIDE_AOI_COUNT = 2

#: The boundary covers 400 m x 300 m = 20 x 15 whole cells of the 20 m DEM, and
#: its edges fall exactly on cell boundaries, so a raster clip has no partial
#: cells to round. That is deliberate: a fixture whose clip result depends on
#: the clipper's rounding rule cannot test the clipper.
AOI_BOUNDARY_DEM_CELLS = 20 * 15                               # 300

#: Lengths chosen to be exact: a straight 500 m run, and a two-segment line of
#: 300 m + 400 m.
TRACKS_LAYER = "tracks"
TRACKS_FIELDS = {"id": "Integer", "name": "String", "surface": "String"}
TRACKS = [
    {
        "id": 1,
        "name": "track_a",
        "surface": "gravel",
        "vertices": [(440050.0, 180050.0), (440550.0, 180050.0)],
        "length_m": 500.0,
    },
    {
        "id": 2,
        "name": "track_b",
        "surface": "tarmac",
        "vertices": [
            (440600.0, 180800.0),
            (440900.0, 180800.0),
            (440900.0, 180400.0),
        ],
        "length_m": 700.0,
    },
]
TRACKS_COUNT = len(TRACKS)                                     # 2
TRACKS_TOTAL_LENGTH_M = sum(track["length_m"] for track in TRACKS)  # 1200.0

# ---------------------------------------------------------------------------
# The fixture project: test-data/generated/fixture_project.qgz
# ---------------------------------------------------------------------------

PROJECT_NAME = "fixture_project.qgz"
PROJECT_TITLE = "QGIS MCP synthetic fixtures (SU4080)"
PROJECT_CRS = PRIMARY_CRS
#: Layer names as registered in the project, in load order.
PROJECT_LAYER_NAMES = [
    "dem_su4080",
    SURVEY_POINTS_LAYER,
    AOI_BOUNDARY_LAYER,
    TRACKS_LAYER,
]
PROJECT_LAYER_COUNT = len(PROJECT_LAYER_NAMES)                 # 4
PROJECT_RASTER_LAYER_COUNT = 1
PROJECT_VECTOR_LAYER_COUNT = 3
PROJECT_LAYOUT_COUNT = 0

#: The three vector layers sit in a group, and one layer is unchecked.
#:
#: Both exist so that ``get_layers_tree`` has something to report. A flat,
#: all-visible project cannot distinguish a tool that walks the layer tree from
#: one that simply lists ``mapLayers()`` in arbitrary order -- the two would
#: agree on every assertion.
PROJECT_GROUP_NAME = "vectors"
PROJECT_HIDDEN_LAYER = TRACKS_LAYER

#: The layer tree as built, top to bottom. Order is set explicitly by the
#: generator rather than left to ``addMapLayer``'s insert-at-top default, so
#: that it is a declared property rather than an incidental one.
PROJECT_TREE = [
    {"kind": "layer", "name": "dem_su4080", "visible": True},
    {
        "kind": "group",
        "name": PROJECT_GROUP_NAME,
        "visible": True,
        "children": [
            {"kind": "layer", "name": SURVEY_POINTS_LAYER, "visible": True},
            {"kind": "layer", "name": AOI_BOUNDARY_LAYER, "visible": True},
            {"kind": "layer", "name": TRACKS_LAYER, "visible": False},
        ],
    },
]

# ---------------------------------------------------------------------------
# Layer introspection expectations (task 2)
# ---------------------------------------------------------------------------

#: The GeoPackage primary key, which PyQGIS surfaces as an ordinary field.
#:
#: Worth recording because the two readers disagree. OGR treats ``fid`` as the
#: feature id and leaves it out of the field list, so ``test_fixtures.py``
#: asserts the declared schema exactly; ``QgsVectorLayer.fields()`` includes
#: it. Neither is wrong, and a test that assumed they agreed would fail for a
#: reason that looks like a fixture fault.
GPKG_FID_FIELD = "fid"
GPKG_FID_TYPE = "Integer64"


def pyqgis_fields(declared: dict[str, str]) -> dict[str, str]:
    """Field schema as PyQGIS reports it: the declared fields plus ``fid``."""
    return {GPKG_FID_FIELD: GPKG_FID_TYPE, **declared}


#: Per-layer properties ``get_layer_summary`` must report, keyed by layer name.
#:
#: Renderer types are QGIS's defaults for a layer added without styling. They
#: are asserted rather than ignored so that a change in QGIS's default is
#: reported as a change, not absorbed silently.
LAYER_SUMMARIES: dict[str, dict] = {
    "dem_su4080": {
        "layer_type": "Raster",
        "crs": PRIMARY_CRS,
        "extent": list(EXTENT),
        "width": DEM_WIDTH,
        "height": DEM_HEIGHT,
        "band_count": DEM_BAND_COUNT,
        "data_type": "Float32",
        "nodata_value": DEM_NODATA,
        "renderer_type": "singlebandgray",
    },
    SURVEY_POINTS_LAYER: {
        "layer_type": "Vector",
        "crs": PRIMARY_CRS,
        "geometry_type": "Point",
        "feature_count": SURVEY_POINTS_COUNT,
        "fields": pyqgis_fields(SURVEY_POINTS_FIELDS),
        "renderer_type": "singleSymbol",
    },
    AOI_BOUNDARY_LAYER: {
        "layer_type": "Vector",
        "crs": PRIMARY_CRS,
        "geometry_type": "Polygon",
        "feature_count": AOI_BOUNDARY_COUNT,
        "fields": pyqgis_fields(AOI_BOUNDARY_FIELDS),
        "renderer_type": "singleSymbol",
    },
    TRACKS_LAYER: {
        "layer_type": "Vector",
        "crs": PRIMARY_CRS,
        "geometry_type": "LineString",
        "feature_count": TRACKS_COUNT,
        "fields": pyqgis_fields(TRACKS_FIELDS),
        "renderer_type": "singleSymbol",
    },
}

# ---------------------------------------------------------------------------
# Processing expectations (task 3)
# ---------------------------------------------------------------------------
#
# Hand-calculable outputs for known algorithms on these fixtures, which is what
# tasks.md task 3 asks its test to assert: "known algorithm + fixture input ->
# hand-calculable expected output".

#: Enum option numbers for native:buffer. Not guessable -- they come from the
#: algorithm's own parameter definition, and the geometry below depends on them.
BUFFER_JOIN_STYLE_MITER = 1
BUFFER_END_CAP_FLAT = 1
BUFFER_DISTANCE_M = 10.0

#: Buffering the AOI rectangle outward by d with *miter* joins gives a larger
#: rectangle -- a miter join at a convex right angle extends to a sharp corner,
#: filling the full d x d square. So the result is exactly
#: (400 + 2d) x (300 + 2d) = 420 x 320, with no arcs to approximate.
#:
#: Round joins would make the corners quarter-circles and the area
#: 120000 + 1400d + pi*d^2, which no longer has an exact decimal value -- hence
#: the miter.
AOI_BUFFERED_WIDTH_M = AOI_BOUNDARY_WIDTH_M + 2 * BUFFER_DISTANCE_M     # 420.0
AOI_BUFFERED_HEIGHT_M = AOI_BOUNDARY_HEIGHT_M + 2 * BUFFER_DISTANCE_M   # 320.0
AOI_BUFFERED_AREA_M2 = AOI_BUFFERED_WIDTH_M * AOI_BUFFERED_HEIGHT_M     # 134400.0
AOI_BUFFERED_EXTENT = (
    AOI_BOUNDARY_RING[0][0] - BUFFER_DISTANCE_M,
    AOI_BOUNDARY_RING[0][1] - BUFFER_DISTANCE_M,
    AOI_BOUNDARY_RING[1][0] + BUFFER_DISTANCE_M,
    AOI_BOUNDARY_RING[2][1] + BUFFER_DISTANCE_M,
)                                     # (440190.0, 180290.0, 440610.0, 180610.0)

#: What native:rasterlayerstatistics must report for the primary DEM. These are
#: the same numbers declared above, restated under the keys the algorithm uses,
#: so a test can compare the two directly. COUNT excluding the nodata cells is
#: the independent confirmation that Processing honours the sentinel.
RASTER_STATISTICS = {
    "COUNT": VALID_CELL_COUNT,        # 2491
    "MIN": DEM_VALID_MIN,             # -5.0
    "MAX": DEM_VALID_MAX,             # 342.0
    "RANGE": DEM_VALID_RANGE,         # 347.0
    "SUM": DEM_VALID_SUM,             # 424675.0
    "MEAN": DEM_VALID_MEAN,
}


# ---------------------------------------------------------------------------
# Clipping expectations (task 4.2)
# ---------------------------------------------------------------------------

#: The AOI boundary's extent in the conventional (xmin, ymin, xmax, ymax)
#: order -- the one GeoJSON, OGC, GDAL and shapely all use.
AOI_BBOX = (
    AOI_BOUNDARY_RING[0][0],
    AOI_BOUNDARY_RING[0][1],
    AOI_BOUNDARY_RING[1][0],
    AOI_BOUNDARY_RING[2][1],
)                                     # (440200.0, 180300.0, 440600.0, 180600.0)

#: The same box as QGIS itself wants it: xmin,xmax,ymin,ymax.
#:
#: Recorded because the difference is invisible at a glance and silent at
#: runtime. Passing the tuple above to a clip algorithm unconverted returns a
#: 12995 x 13000 raster with easting and northing swapped, reported as success.
AOI_QGIS_EXTENT_STRING = (
    f"{AOI_BBOX[0]},{AOI_BBOX[2]},{AOI_BBOX[1]},{AOI_BBOX[3]} [{PRIMARY_CRS}]"
)

#: The DEM clipped to AOI_BBOX. The AOI's edges fall exactly on 20 m cell
#: boundaries, so there are no partial cells to round:
#:     400 m / 20 m = 20 columns, 300 m / 20 m = 15 rows
CLIPPED_DEM_WIDTH = int(AOI_BOUNDARY_WIDTH_M / PIXEL_SIZE_M)       # 20
CLIPPED_DEM_HEIGHT = int(AOI_BOUNDARY_HEIGHT_M / PIXEL_SIZE_M)     # 15
CLIPPED_DEM_CELLS = CLIPPED_DEM_WIDTH * CLIPPED_DEM_HEIGHT         # 300
#: Top-left corner, north-up.
CLIPPED_DEM_ORIGIN = (AOI_BBOX[0], AOI_BBOX[3])     # (440200.0, 180600.0)

#: The clipped window covers rows 20-34 and columns 10-29, which misses every
#: special region: the hill is rows 10-14, the coastal inlet columns 0-4, the
#: basin columns 30-34, the nodata block rows 0-2. So the clip is pure base
#: ramp, and its range follows from the formula alone:
#:     min at row 20 / col 10 = 100 + 40 + 10 = 150
#:     max at row 34 / col 29 = 100 + 68 + 29 = 197
CLIPPED_DEM_MIN = BASE_INTERCEPT + BASE_ROW_COEFF * 20 + BASE_COL_COEFF * 10
CLIPPED_DEM_MAX = BASE_INTERCEPT + BASE_ROW_COEFF * 34 + BASE_COL_COEFF * 29
#: No nodata survives the clip, which is itself worth asserting -- it confirms
#: the clip took the window it was asked for rather than a shifted one.
CLIPPED_DEM_NODATA_CELLS = 0

#: A bbox clipping one track part-way, to test geometry truncation rather than
#: feature selection. track_a runs west-east at y=180050 from x=440050 to
#: x=440550; this box keeps x from 440100 to 440300, so exactly 200 m of it
#: survives. track_b lies entirely east of the box.
TRACK_CLIP_BBOX = (440100.0, 180000.0, 440300.0, 180100.0)
TRACK_CLIP_FEATURE_COUNT = 1
TRACK_CLIP_LENGTH_M = 200.0


# ---------------------------------------------------------------------------
# Expected output of all fixtures
# ---------------------------------------------------------------------------

ALL_FIXTURE_FILES = [
    DEM_NAME,
    GEOGRAPHIC_DEM_NAME,
    VECTOR_GPKG_NAME,
    PROJECT_NAME,
]
