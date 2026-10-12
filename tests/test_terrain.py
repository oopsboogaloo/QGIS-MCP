"""Task 5: heightmap export, sea-level modelling, and the elevation wrapper.

The fixture DEM was built for these tests. `tasks.md` Task 0 required nodata
cells and a landlocked basin below sea level specifically because they are "the
cases separating a correct terrain implementation from one that merely looks
correct" — and this is where that pays off:

* REQ-11's nodata trap is checked against the real sentinel, so a normalisation
  that included it would compress all terrain into about 3.4% of the output
  range while the file's metadata stayed entirely normal.
* `connectivity` is checked in both directions against the basin, which is the
  only way to tell an implementation that honours the option from one that
  ignores it.

Offline throughout. `fetch_elevation_data` seeds its download from the
committed fixture, which drives the same reuse branch a real second download
takes, so no API quota is spent.
"""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

import pytest

from conftest import CollectingServer, assert_ok, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode
from qgis_mcp_server.config import Config
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.geocoding import (
    NominatimGeocoder,
    PlaceResolver,
    ResolutionCache,
)
from qgis_mcp_server.providers.catalogue import OPENTOPOGRAPHY_CREDENTIAL

FIXTURE_AOI = (-1.270, 51.170, -1.255, 51.180)
RESOLVES = "Andover, Hampshire, England"


@pytest.fixture(scope="module")
def terrain(context):
    return context.terrain


@pytest.fixture(scope="module")
def tools(context):
    from qgis_mcp_server.tools import terrain as terrain_tools

    server = CollectingServer()
    terrain_tools.register(server, context)
    return server


# ---------------------------------------------------------------------------
# 5.2 export_heightmap — REQ-11
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def heightmap(terrain, fixture_dem, tmp_path_factory):
    destination = tmp_path_factory.mktemp("heightmap") / "dem.png"
    envelope = terrain.export_heightmap(fixture_dem, destination)
    assert_ok(envelope, context_note="exporting the fixture DEM as a heightmap")
    return envelope.result, destination


def test_heightmap_is_at_least_16_bit(heightmap, runner):
    """REQ-11 clause 1, verified in the written file rather than the report.

    8-bit gives 256 steps: across the fixture's 347 m range that is 1.4 m per
    step, which reads as stair-stepping on any rendered hillside.
    """
    result, destination = heightmap
    assert result["bits_per_channel"] >= 16, describe(result)

    probe = runner.run_code(
        "from osgeo import gdal\n"
        f"dataset = gdal.Open({str(destination)!r})\n"
        "band = dataset.GetRasterBand(1)\n"
        "result = {\n"
        "    'data_type': gdal.GetDataTypeName(band.DataType),\n"
        "    'bits': gdal.GetDataTypeSize(band.DataType),\n"
        "    'width': dataset.RasterXSize,\n"
        "    'height': dataset.RasterYSize,\n"
        "}\n"
    )
    assert_ok(probe)
    assert probe.result["bits"] >= 16, (
        f"the written file is {probe.result['bits']}-bit; REQ-11 requires at "
        f"least 16\n{describe(probe.result)}"
    )
    assert probe.result["data_type"] == "UInt16", describe(probe.result)


def test_heightmap_reports_the_real_elevation_range(heightmap, fixture_truth):
    """REQ-11 clause 3: the range the image's 0..65535 actually maps to."""
    result, _ = heightmap
    assert result["source_elevation_min_m"] == fixture_truth.DEM_VALID_MIN, (
        f"minimum mismatch\n"
        f"    expected: {fixture_truth.DEM_VALID_MIN}\n"
        f"    actual:   {result['source_elevation_min_m']}"
    )
    assert result["source_elevation_max_m"] == fixture_truth.DEM_VALID_MAX, (
        f"maximum mismatch\n"
        f"    expected: {fixture_truth.DEM_VALID_MAX}\n"
        f"    actual:   {result['source_elevation_max_m']}"
    )
    assert result["source_elevation_range_m"] == fixture_truth.DEM_VALID_RANGE


def test_nodata_is_excluded_from_the_normalisation_range(heightmap, fixture_truth):
    """REQ-11 clause 2 — the assertion `tasks.md` singles out.

    "A fixture containing nodata normalises from the real range rather than the
    nodata sentinel. That last assertion is the one that catches the failure
    which looks fine until it is rendered."

    Asserted from both directions: the range is the real one, and it is *not*
    the one a naive implementation would produce.
    """
    result, _ = heightmap
    assert result["nodata_cells"] == fixture_truth.NODATA_CELL_COUNT, (
        f"the fixture must contain nodata for this test to mean anything; "
        f"found {result['nodata_cells']}"
    )
    assert result["valid_cells"] == fixture_truth.VALID_CELL_COUNT

    trap = fixture_truth.REQ11_NODATA_TRAP
    assert result["source_elevation_min_m"] != trap["naive_min"], (
        f"the normalisation minimum is the nodata sentinel "
        f"({trap['naive_min']}), so nodata was included. Real terrain would "
        f"occupy about "
        f"{trap['terrain_fraction_of_range_if_naive']:.1%} of the output range "
        f"and render as a flat plane, with nothing wrong in the file's "
        f"metadata."
    )
    assert result["source_elevation_min_m"] == trap["correct_min"]


def test_heightmap_pixels_use_the_full_output_range(heightmap, runner, fixture_truth):
    """The range is not just reported, it is actually used.

    Reporting the right numbers while writing a compressed image would pass
    every metadata assertion above. This reads the pixels.
    """
    _, destination = heightmap
    probe = runner.run_code(
        "import numpy as np\n"
        "from osgeo import gdal\n"
        f"array = gdal.Open({str(destination)!r}).ReadAsArray()\n"
        "result = {'min': int(array.min()), 'max': int(array.max())}\n"
    )
    assert_ok(probe)
    assert probe.result["min"] == 0, describe(probe.result)
    assert probe.result["max"] == 65535, (
        f"the highest terrain should map to the top of the 16-bit range; got "
        f"{probe.result['max']}. A lower maximum means the range was "
        f"compressed.\n{describe(probe.result)}"
    )


def test_heightmap_reports_the_ground_extent_and_ratio(heightmap, fixture_truth):
    """REQ-11 clause 3's other half: what makes the export reproducible.

    A heightmap image carries no absolute scale, so without these the vertical
    exaggeration is set by eye and differs between exports.
    """
    result, _ = heightmap
    assert result["extent_width_m"] == fixture_truth.EXTENT_WIDTH_M
    assert result["extent_height_m"] == fixture_truth.EXTENT_HEIGHT_M
    assert result["aspect_ratio"] == pytest.approx(1.0)
    assert result["displacement_ratio"] == pytest.approx(
        fixture_truth.DEM_DISPLACEMENT_RATIO
    ), (
        f"displacement ratio mismatch\n"
        f"    expected: {fixture_truth.DEM_DISPLACEMENT_RATIO}\n"
        f"    actual:   {result['displacement_ratio']}"
    )
    assert result["displacement_note"], describe(result)


def test_heightmap_warns_that_nodata_was_filled(heightmap):
    """Filled cells are scaled correctly and are not measured ground."""
    result, _ = heightmap
    warnings = " ".join(result["warnings"])
    assert "nodata" in warnings, warnings
    assert "not measured ground" in warnings, warnings


def test_geographic_dem_is_refused(terrain, fixture_dir, fixture_truth, tmp_path):
    """REQ-11 clause 4, on the fixture built for it.

    The geographic DEM exists in `test-data/` for exactly this case. A ratio
    computed from degrees stretches terrain east-west by roughly 62% at UK
    latitudes — plausible terrain, not the shape of that place.
    """
    source = fixture_dir / fixture_truth.GEOGRAPHIC_DEM_NAME
    envelope = terrain.export_heightmap(source, tmp_path / "bad.png")
    assert envelope.ok is False, describe(envelope.result)
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST
    assert envelope.error["is_geographic"] is True, describe(envelope.error)
    assert "62%" in envelope.error["message"], envelope.error["message"]
    assert "download_and_register_layer" in envelope.error["message"], (
        "the refusal should name the way to fix it"
    )
    assert not (tmp_path / "bad.png").exists(), "nothing should be written"


def test_geographic_dem_can_be_forced(terrain, fixture_dir, fixture_truth, tmp_path):
    """The override exists, so the refusal is a guard rather than a wall."""
    source = fixture_dir / fixture_truth.GEOGRAPHIC_DEM_NAME
    envelope = terrain.export_heightmap(
        source, tmp_path / "forced.png", allow_geographic=True
    )
    assert_ok(envelope, context_note="forcing a geographic export")
    assert (tmp_path / "forced.png").exists()


@pytest.mark.parametrize("image_format,expected", [("tif", "UInt16"), ("tif32", "Float32")])
def test_other_formats(terrain, fixture_dem, tmp_path, runner, image_format, expected):
    destination = tmp_path / f"dem.{image_format}"
    envelope = terrain.export_heightmap(
        fixture_dem, destination, image_format=image_format
    )
    assert_ok(envelope, context_note=f"format={image_format}")
    probe = runner.run_code(
        "from osgeo import gdal\n"
        # The dataset must stay referenced: GDAL invalidates a band whose
        # dataset has been garbage-collected, and the failure surfaces as an
        # opaque TypeError out of the SWIG layer.
        f"dataset = gdal.Open({str(destination)!r})\n"
        "band = dataset.GetRasterBand(1)\n"
        "result = gdal.GetDataTypeName(band.DataType)\n"
    )
    assert_ok(probe)
    assert probe.result == expected, f"expected {expected}, got {probe.result}"


def test_float32_export_keeps_metres(terrain, fixture_dem, tmp_path, runner, fixture_truth):
    """``tif32`` is the lossless path: values stay in metres, unnormalised."""
    destination = tmp_path / "dem.tif32"
    assert_ok(terrain.export_heightmap(fixture_dem, destination, image_format="tif32"))
    probe = runner.run_code(
        "import numpy as np\n"
        "from osgeo import gdal\n"
        f"array = gdal.Open({str(destination)!r}).ReadAsArray()\n"
        "result = {'min': float(array.min()), 'max': float(array.max())}\n"
    )
    assert_ok(probe)
    assert probe.result["max"] == pytest.approx(fixture_truth.DEM_VALID_MAX), (
        f"float32 export should carry metres, not a normalised value\n"
        f"{describe(probe.result)}"
    )


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"image_format": "jpeg"}, "image_format must be"),
        ({"nodata_fill": "zero"}, "nodata_fill must be"),
    ],
)
def test_bad_options_are_refused_without_a_qgis_startup(
    terrain, fixture_dem, tmp_path, kwargs, fragment
):
    envelope = terrain.export_heightmap(fixture_dem, tmp_path / "x.png", **kwargs)
    assert envelope.ok is False
    assert fragment in envelope.error["message"], envelope.error["message"]


def test_missing_source_is_refused(terrain, tmp_path):
    envelope = terrain.export_heightmap(tmp_path / "absent.tif", tmp_path / "x.png")
    assert envelope.ok is False
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST
    assert "does not exist" in envelope.error["message"]


# ---------------------------------------------------------------------------
# 5.4 model_sea_level_change — REQ-13
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bathtub(terrain, fixture_dem, tmp_path_factory):
    out = tmp_path_factory.mktemp("bathtub")
    envelope = terrain.model_sea_level_change(
        fixture_dem, sea_level_m=0.0, connectivity="bathtub",
        output_dir=out, depth_raster=True,
    )
    assert_ok(envelope, context_note="bathtub flooding at 0 m")
    return envelope.result


@pytest.fixture(scope="module")
def ocean(terrain, fixture_dem, tmp_path_factory):
    out = tmp_path_factory.mktemp("ocean")
    envelope = terrain.model_sea_level_change(
        fixture_dem, sea_level_m=0.0, connectivity="ocean",
        output_dir=out, vertical_datum="EGM2008",
    )
    assert_ok(envelope, context_note="ocean-connected flooding at 0 m")
    return envelope.result


def test_bathtub_floods_everything_below_the_level(bathtub, fixture_truth):
    """Hand-computable: the coastal inlet plus the basin."""
    assert bathtub["water_cells"] == fixture_truth.BATHTUB_FLOODED_CELLS, (
        f"flooded cell count mismatch\n"
        f"    expected: {fixture_truth.BATHTUB_FLOODED_CELLS}\n"
        f"    actual:   {bathtub['water_cells']}"
    )
    assert bathtub["water_area_m2"] == pytest.approx(
        fixture_truth.BATHTUB_FLOODED_AREA_M2
    )
    assert bathtub["land_cells"] == fixture_truth.BATHTUB_LAND_CELLS
    assert bathtub["nodata_cells"] == fixture_truth.NODATA_CELL_COUNT


def test_ocean_connected_leaves_the_landlocked_basin_dry(ocean, fixture_truth):
    """The reason Task 0 required a landlocked basin at all.

    Without `connectivity` honoured, this would equal the bathtub figure. The
    difference is exactly the basin.
    """
    assert ocean["water_cells"] == fixture_truth.OCEAN_CONNECTED_FLOODED_CELLS, (
        f"ocean-connected cell count mismatch\n"
        f"    expected: {fixture_truth.OCEAN_CONNECTED_FLOODED_CELLS}\n"
        f"    actual:   {ocean['water_cells']}"
    )
    assert ocean["water_area_m2"] == pytest.approx(
        fixture_truth.OCEAN_CONNECTED_FLOODED_AREA_M2
    )


def test_the_two_modes_differ_by_exactly_the_basin(bathtub, ocean, fixture_truth):
    """The assertion that catches an implementation ignoring the option."""
    difference = bathtub["water_cells"] - ocean["water_cells"]
    assert difference == fixture_truth.CONNECTIVITY_DIFFERENCE_CELLS, (
        f"the modes must differ by exactly the {fixture_truth.BASIN_CELL_COUNT}"
        f"-cell basin\n"
        f"    expected difference: "
        f"{fixture_truth.CONNECTIVITY_DIFFERENCE_CELLS}\n"
        f"    actual difference:   {difference}"
    )
    assert difference > 0, (
        "the two modes produced identical results, so connectivity is being "
        "ignored"
    )


def test_ocean_mode_reports_what_it_excluded(ocean, fixture_truth):
    """So the two answers are comparable without running both."""
    assert ocean["cells_below_sea_level"] == fixture_truth.BATHTUB_FLOODED_CELLS
    assert (
        ocean["landlocked_cells_excluded"]
        == fixture_truth.CONNECTIVITY_DIFFERENCE_CELLS
    ), describe(ocean)
    assert ocean["landlocked_area_m2"] == pytest.approx(
        fixture_truth.CONNECTIVITY_DIFFERENCE_CELLS * fixture_truth.CELL_AREA_M2
    )


def test_polygon_counts_distinguish_the_two_modes(bathtub, ocean, fixture_truth):
    """Structure, not only area.

    An implementation flooding the right *number* of cells in the wrong places
    could still match on area. The polygon count could not.
    """
    assert bathtub["water_polygon_count"] == (
        fixture_truth.BATHTUB_WATER_POLYGON_COUNT
    ), describe(bathtub)
    assert ocean["water_polygon_count"] == (
        fixture_truth.OCEAN_CONNECTED_WATER_POLYGON_COUNT
    ), describe(ocean)
    assert bathtub["water_polygon_area_m2"] == pytest.approx(
        fixture_truth.BATHTUB_FLOODED_AREA_M2
    )


def test_outputs_exist(bathtub):
    assert Path(bathtub["water_mask_path"]).exists()
    assert Path(bathtub["water_polygons_path"]).exists()
    assert Path(bathtub["depth_raster_path"]).exists()


def test_depth_raster_is_only_written_when_asked(ocean):
    assert ocean["depth_raster_path"] is None, describe(ocean)


def test_the_source_dem_is_not_modified(
    terrain, fixture_dem, runner, fixture_truth, tmp_path
):
    """`tasks.md` 5.4: "Assert the source DEM's statistics are unchanged".

    Thresholding, not mutation. Compared across the call rather than against
    `truth.py` alone, so a run that rewrote the DEM into a state that happened
    to match the declaration would still fail.
    """
    probe = (
        "import numpy as np\n"
        "from osgeo import gdal\n"
        f"dataset = gdal.Open({str(fixture_dem)!r})\n"
        "band = dataset.GetRasterBand(1)\n"
        "array = band.ReadAsArray()\n"
        "nodata = band.GetNoDataValue()\n"
        "valid = array[array != nodata]\n"
        "result = {\n"
        "    'min': float(valid.min()), 'max': float(valid.max()),\n"
        "    'sum': float(valid.sum()), 'count': int(valid.size),\n"
        "    'nodata': float(nodata),\n"
        "}\n"
    )
    before = runner.run_code(probe)
    assert_ok(before)

    assert_ok(
        terrain.model_sea_level_change(
            fixture_dem, sea_level_m=50.0, output_dir=tmp_path / "out"
        )
    )

    after = runner.run_code(probe)
    assert_ok(after)
    assert after.result == before.result, (
        f"the source DEM changed across the call\n"
        f"    before: {describe(before.result)}\n"
        f"    after:  {describe(after.result)}"
    )
    # And it still matches the declaration, so neither read was of a file
    # already corrupted before the test began.
    assert after.result["sum"] == pytest.approx(fixture_truth.DEM_VALID_SUM)


def test_unknown_vertical_datum_warns_and_proceeds(bathtub):
    """REQ-13: warn, do not block.

    "Implementing geoid transformation is disproportionate to v1; a visible
    caveat removes the silence, which is the actual danger."
    """
    warnings = " ".join(bathtub["warnings"])
    assert "vertical datum is unknown" in warnings, warnings
    assert "50 m" in warnings, warnings
    assert bathtub["vertical_datum"] is None
    # Proceeded anyway.
    assert bathtub["water_cells"] > 0


def test_known_datum_does_not_warn(ocean):
    """So the warning means something when it appears."""
    assert ocean["vertical_datum"] == "EGM2008"
    warnings = " ".join(ocean["warnings"])
    assert "unknown" not in warnings, warnings


def test_ellipsoidal_datum_warns_specifically(terrain, fixture_dem, tmp_path):
    """A declared datum can still be the wrong kind."""
    envelope = terrain.model_sea_level_change(
        fixture_dem,
        output_dir=tmp_path / "out",
        vertical_datum="WGS84 ellipsoid",
    )
    assert_ok(envelope)
    warnings = " ".join(envelope.result["warnings"])
    assert "ellipsoid" in warnings, warnings
    assert "not measured from mean sea level" in warnings, warnings


def test_geographic_dem_warns_that_areas_are_meaningless(
    terrain, fixture_dir, fixture_truth, tmp_path
):
    """Areas in square degrees are not comparable to anything."""
    envelope = terrain.model_sea_level_change(
        fixture_dir / fixture_truth.GEOGRAPHIC_DEM_NAME,
        sea_level_m=60.0,
        output_dir=tmp_path / "geo",
    )
    assert_ok(envelope)
    warnings = " ".join(envelope.result["warnings"])
    assert "square degrees" in warnings, warnings


def test_bad_connectivity_is_refused(terrain, fixture_dem, tmp_path):
    envelope = terrain.model_sea_level_change(
        fixture_dem, connectivity="tidal", output_dir=tmp_path
    )
    assert envelope.ok is False
    assert "connectivity must be" in envelope.error["message"]
    assert "bathtub" in envelope.error["supported_connectivity"]


def test_a_higher_sea_level_floods_more(terrain, fixture_dem, tmp_path):
    """Monotonicity. Trivial, and it would catch a sign error."""
    low = terrain.model_sea_level_change(
        fixture_dem, sea_level_m=0.0, output_dir=tmp_path / "low"
    )
    high = terrain.model_sea_level_change(
        fixture_dem, sea_level_m=150.0, output_dir=tmp_path / "high"
    )
    assert_ok(low)
    assert_ok(high)
    assert high.result["water_cells"] > low.result["water_cells"], (
        f"raising the sea level reduced the flooded area: "
        f"{low.result['water_cells']} -> {high.result['water_cells']}"
    )


# ---------------------------------------------------------------------------
# transform_bbox
# ---------------------------------------------------------------------------


def test_transform_bbox_returns_an_inner_and_an_outer_rectangle(terrain):
    """design.md §5: a reprojected lat/lon box is a curved quadrilateral.

    Both answers are useful and they are not the same: the outer box keeps
    every pixel and carries nodata corners; the inner one has a clean edge.
    """
    envelope = terrain.transform_bbox(FIXTURE_AOI, target_crs="EPSG:27700")
    assert_ok(envelope)
    result = envelope.result
    outer, inner = result["outer"], result["inner"]

    assert inner is not None, describe(result)
    assert inner[0] >= outer[0] and inner[1] >= outer[1], describe(result)
    assert inner[2] <= outer[2] and inner[3] <= outer[3], describe(result)
    assert (inner[2] - inner[0]) < (outer[2] - outer[0]), (
        "the inscribed rectangle should be narrower than the bounding one; if "
        "they match, the edges were not sampled"
    )
    # Roughly a kilometre square of Hampshire, in national grid metres.
    assert 900 < result["outer_width_m"] < 1200, describe(result)


def test_transform_bbox_refuses_a_bad_crs(terrain):
    envelope = terrain.transform_bbox(FIXTURE_AOI, target_crs="EPSG:999999")
    assert envelope.ok is False
    assert "not a usable CRS" in envelope.error["message"]


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def test_heightmap_tool_reports_the_serving_mode(tools, fixture_dem, tmp_path):
    payload = tools.tools["export_heightmap"](
        source=str(fixture_dem), destination=str(tmp_path / "hm.png")
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["mode"] == ExecutionMode.HEADLESS
    assert payload["mode_reason"]
    assert payload["displacement_ratio"] > 0


def test_sea_level_tool_reports_the_serving_mode(tools, fixture_dem, tmp_path):
    payload = tools.tools["model_sea_level_change"](
        source=str(fixture_dem), output_dir=str(tmp_path / "out")
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["mode"] == ExecutionMode.HEADLESS
    assert payload["water_cells"] > 0


def test_terrain_tools_are_mode_agnostic():
    from qgis_mcp_server import modes

    for name in (
        "export_heightmap",
        "model_sea_level_change",
        "fetch_elevation_data",
    ):
        assert name not in modes.MODE_NAMED_TOOLS, name


# ---------------------------------------------------------------------------
# 5.1 fetch_elevation_data — the wrapper must not bypass the gate
# ---------------------------------------------------------------------------


@pytest.fixture
def elevation_tools(monkeypatch, tmp_path, request):
    """A context for the wrapper: temp cache, key set, no network."""
    monkeypatch.setenv(OPENTOPOGRAPHY_CREDENTIAL.env_var, "test-key-0123456789")

    def refuse(*_args, **_kwargs):
        raise AssertionError("fetch_elevation_data must not reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    cache_path = request.config.rootpath / "test-data" / "geocoder-cache"
    if not cache_path.is_dir():
        pytest.skip("geocoder fixtures missing")

    config = Config.load()
    config.cache_dir = tmp_path / "cache"
    config.project_path = None

    with ServerContext(config) as context:
        context.places = PlaceResolver(
            geocoders=(
                NominatimGeocoder(
                    cache=ResolutionCache(cache_path, read_only=True), offline=True
                ),
            )
        )
        from qgis_mcp_server.tools import terrain as terrain_tools

        server = CollectingServer()
        terrain_tools.register(server, context)
        yield server, context


def test_wrapper_refuses_without_confirmation(elevation_tools):
    """REQ-4d names `fetch_elevation_data` explicitly.

    "The wrapper is named explicitly because convenience wrapping is exactly
    how a fetch could otherwise slip through unconfirmed."
    """
    tools, _ = elevation_tools
    payload = tools.tools["fetch_elevation_data"](bbox=list(FIXTURE_AOI))
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.CONFIRMATION_REQUIRED
    assert payload["error"]["wraps"] == "download_and_register_layer", (
        "the refusal should say what it wraps, so the two gates are visibly "
        "the same one"
    )


def test_wrapper_states_the_same_four_things(elevation_tools):
    """Source, format, licence tier, approximate size — as the wrapped tool does."""
    tools, _ = elevation_tools
    payload = tools.tools["fetch_elevation_data"](bbox=list(FIXTURE_AOI))
    plan = payload["error"]["plan"]
    assert plan["source"]["provider"] == "opentopography"
    assert plan["format"] == "GTiff"
    assert plan["licence"]["tier"]
    assert plan["estimated_size_bytes"] > 0


def test_wrapper_picks_the_best_dataset_by_default(elevation_tools):
    """No dataset given, so the ranked first is taken — which must not be an
    ellipsoidal product."""
    tools, _ = elevation_tools
    payload = tools.tools["fetch_elevation_data"](bbox=list(FIXTURE_AOI))
    chosen = payload["error"]["plan"]["source"]["dataset"]
    assert chosen == "COP30", f"expected COP30 to be chosen, got {chosen}"
    assert not chosen.endswith("_E"), chosen


def test_wrapper_downloads_nothing_unconfirmed(elevation_tools):
    tools, context = elevation_tools
    tools.tools["fetch_elevation_data"](bbox=list(FIXTURE_AOI))
    download_dir = context.acquisition.download_dir
    assert not download_dir.exists() or not list(download_dir.rglob("*.tif"))


def test_wrapper_resolves_a_place_name(elevation_tools):
    tools, _ = elevation_tools
    payload = tools.tools["fetch_elevation_data"](place=RESOLVES)
    assert payload["error"]["code"] == ErrorCode.CONFIRMATION_REQUIRED
    aoi = payload["error"]["plan"]["aoi"]
    assert -2.0 < aoi[0] < -1.0, aoi
    assert payload["error"]["plan"]["target_crs"] == "EPSG:27700"


def test_wrapper_needs_exactly_one_of_place_or_bbox(elevation_tools):
    tools, _ = elevation_tools
    for kwargs in ({}, {"place": RESOLVES, "bbox": list(FIXTURE_AOI)}):
        payload = tools.tools["fetch_elevation_data"](**kwargs)
        assert payload["ok"] is False
        assert payload["error"]["code"] == ErrorCode.BAD_REQUEST


def test_confirmed_wrapper_produces_a_clipped_heightmap_source(
    elevation_tools, request, tmp_path
):
    """The whole 5.1 chain, with the download seeded from the fixture.

    The result's ``heightmap_source`` is a clean rectangle in the target CRS:
    reprojecting a lat/lon box leaves nodata corners, and those would be filled
    and counted when ``export_heightmap`` derives its displacement ratio.
    """
    tools, context = elevation_tools
    fixture = (
        request.config.rootpath
        / "test-data"
        / "provider-fixtures"
        / "opentopography"
        / "cop30_su4080.tif"
    )
    if not fixture.exists():
        pytest.skip("provider fixtures missing")

    found, failure = context.acquisition.find_dataset(
        "opentopography", "COP30", FIXTURE_AOI
    )
    assert failure is None
    original, _ = context.acquisition.paths_for(found, FIXTURE_AOI, "EPSG:27700")
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixture, original)

    project = tmp_path / "elevation.qgz"
    payload = tools.tools["fetch_elevation_data"](
        bbox=list(FIXTURE_AOI),
        project_path=str(project),
        confirm=True,
    )
    assert payload["ok"] is True, describe(payload)

    assert payload["target_crs"] == "EPSG:27700"
    assert payload["vertical_datum"] == "EGM2008"
    assert payload["registered"] is True
    assert Path(payload["original_path"]).exists(), "REQ-12 keeps the original"
    assert "clipped_path" in payload, describe(payload)
    assert Path(payload["clipped_path"]).exists()
    assert payload["heightmap_source"] == payload["clipped_path"]


def test_the_wrapper_output_exports_as_a_heightmap(
    elevation_tools, request, tmp_path, runner
):
    """End to end: fetch -> clip -> heightmap, on real Copernicus elevation.

    The point of the chain. A clipped rectangle with no nodata corners is what
    makes the displacement ratio describe the ground rather than the gaps.
    """
    tools, context = elevation_tools
    fixture = (
        request.config.rootpath
        / "test-data"
        / "provider-fixtures"
        / "opentopography"
        / "cop30_su4080.tif"
    )
    if not fixture.exists():
        pytest.skip("provider fixtures missing")

    found, _ = context.acquisition.find_dataset(
        "opentopography", "COP30", FIXTURE_AOI
    )
    original, _ = context.acquisition.paths_for(found, FIXTURE_AOI, "EPSG:27700")
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixture, original)

    fetched = tools.tools["fetch_elevation_data"](
        bbox=list(FIXTURE_AOI), register_layer=False, confirm=True
    )
    assert fetched["ok"] is True, describe(fetched)

    destination = tmp_path / "hampshire.png"
    exported = tools.tools["export_heightmap"](
        source=fetched["heightmap_source"], destination=str(destination)
    )
    assert exported["ok"] is True, describe(exported)

    assert exported["bits_per_channel"] >= 16
    assert exported["nodata_cells"] == 0, (
        f"a clipped rectangle should have no nodata; found "
        f"{exported['nodata_cells']}, which means the clip kept the corners"
    )
    assert exported["displacement_ratio"] > 0
    # Real Hampshire downland: a modest range over about a kilometre.
    assert 0 < exported["source_elevation_min_m"] < 200, describe(exported)
    assert exported["source_elevation_max_m"] < 300, describe(exported)
    assert destination.exists()


def test_tool_descriptions_carry_the_hazards(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()
    by_name = {tool.name: tool.description or "" for tool in registered}

    for name in ("export_heightmap", "model_sea_level_change", "fetch_elevation_data"):
        assert name in by_name, sorted(by_name)

    # The three things a caller cannot infer from a signature.
    assert "REFUSED" in by_name["export_heightmap"], by_name["export_heightmap"]
    assert "displacement_ratio" in by_name["export_heightmap"]
    assert "landlocked basin" in by_name["model_sea_level_change"]
    assert "not a way" in by_name["fetch_elevation_data"]
