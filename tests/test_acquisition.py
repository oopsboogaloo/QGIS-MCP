"""Tasks 4.0 and 4.2: provider declarations, credentials, and clip_to_aoi.

`tasks.md` asks task 4.2's test to cover "both input forms — a bounding box for
the DEM, a boundary polygon for the vector layers", and both are here with
hand-computable answers from `truth.py`.

The sharpest test in this file is the extent-order one. QGIS orders extent
strings `xmin,xmax,ymin,ymax`; every other spatial interface a caller is likely
to have used orders them `xmin,ymin,xmax,ymax`. The wrong order does not raise
— it returns a correctly-formatted raster of the wrong place. That is the whole
justification for `clip_to_aoi` existing rather than callers building extent
strings themselves, so the wrong answer is asserted against explicitly.
"""

from __future__ import annotations

import pytest

from conftest import assert_failed, assert_ok, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode
from qgis_mcp_server.clipping import (
    ClipRunner,
    detect_data_kind,
    format_qgis_extent,
    validate_bbox,
)
from qgis_mcp_server.credentials import CredentialSpec, CredentialStore
from qgis_mcp_server.providers import ALL_PROVIDERS, PROVIDERS_BY_NAME, providers_for
from qgis_mcp_server.providers.base import DataType, LicenceTier

UK_AOI = (-1.27, 51.17, -1.255, 51.18)
ONTARIO_AOI = (-80.0, 43.0, -79.0, 44.0)


@pytest.fixture(scope="module")
def clipper(context):
    return ClipRunner(context.processing)


# ---------------------------------------------------------------------------
# The extent-order conversion
# ---------------------------------------------------------------------------


def test_extent_string_inverts_the_order_qgis_wants(fixture_truth):
    """The conversion, asserted against the string recorded in truth.py."""
    rendered = format_qgis_extent(fixture_truth.AOI_BBOX, fixture_truth.PRIMARY_CRS)
    assert rendered == fixture_truth.AOI_QGIS_EXTENT_STRING, (
        f"extent string mismatch\n"
        f"    expected: {fixture_truth.AOI_QGIS_EXTENT_STRING}\n"
        f"    actual:   {rendered}"
    )


def test_extent_string_is_not_the_input_order(fixture_truth):
    """Guards the conversion against being quietly removed.

    If someone 'simplified' format_qgis_extent to pass the tuple through, every
    other test here would still pass on square-ish AOIs. This one would not.
    """
    xmin, ymin, xmax, ymax = fixture_truth.AOI_BBOX
    passthrough = f"{xmin},{ymin},{xmax},{ymax} [{fixture_truth.PRIMARY_CRS}]"
    rendered = format_qgis_extent(fixture_truth.AOI_BBOX, fixture_truth.PRIMARY_CRS)
    assert rendered != passthrough, (
        "format_qgis_extent returned the input order unchanged; QGIS wants "
        "xmin,xmax,ymin,ymax"
    )


def test_extent_string_includes_the_crs():
    """Without it QGIS reads the numbers in the layer's CRS."""
    assert format_qgis_extent((1.0, 2.0, 3.0, 4.0), "EPSG:27700").endswith(
        "[EPSG:27700]"
    )
    assert "[" not in format_qgis_extent((1.0, 2.0, 3.0, 4.0))


@pytest.mark.parametrize(
    "bad_bbox",
    [
        (440600.0, 180300.0, 440200.0, 180600.0),   # reversed x
        (440200.0, 180600.0, 440600.0, 180300.0),   # reversed y
        (440200.0, 180300.0, 440200.0, 180600.0),   # zero width
        (1.0, 2.0, 3.0),                            # too short
    ],
)
def test_malformed_bbox_is_refused(bad_bbox):
    with pytest.raises(ValueError):
        validate_bbox(bad_bbox)


def test_reversed_bbox_names_the_order_confusion(fixture_truth):
    """The likeliest cause of a reversed box is QGIS's own order, so say so."""
    xmin, ymin, xmax, ymax = fixture_truth.AOI_BBOX
    with pytest.raises(ValueError) as caught:
        validate_bbox((xmin, xmax, ymin, ymax))
    assert "xmin,xmax,ymin,ymax" in str(caught.value), str(caught.value)


# ---------------------------------------------------------------------------
# Data-kind detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        ("dem.tif", "raster"),
        ("dem.TIFF", "raster"),
        ("mosaic.vrt", "raster"),
        ("roads.shp", "vector"),
        ("places.geojson", "vector"),
        ("data.gpkg", None),
        ("data.gpkg|layername=roads", None),
        ("noextension", None),
    ],
)
def test_data_kind_detection(source, expected):
    assert detect_data_kind(source) == expected, (
        f"detect_data_kind({source!r}) should be {expected!r}, got "
        f"{detect_data_kind(source)!r}"
    )


def test_geopackage_requires_an_explicit_data_kind(clipper, fixture_gpkg, tmp_path):
    """A GeoPackage holds either, so guessing would be a coin flip.

    Refusing with a clear reason beats a 50% chance of a confusing
    algorithm-level error.
    """
    envelope = clipper.clip(
        f"{fixture_gpkg}|layername=tracks",
        tmp_path / "out.gpkg",
        bbox=(440100.0, 180000.0, 440300.0, 180100.0),
        bbox_crs="EPSG:27700",
    )
    assert_failed(envelope, ErrorCode.BAD_REQUEST)
    assert "data_kind" in envelope.error["message"], describe(envelope.error)
    assert "GeoPackage" in envelope.error["message"], describe(envelope.error)


# ---------------------------------------------------------------------------
# clip_to_aoi: raster by bounding box
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def clipped_dem(clipper, runner, fixture_dem, fixture_truth, tmp_path_factory):
    """The DEM clipped to the AOI bounding box, and measured."""
    destination = tmp_path_factory.mktemp("clip") / "dem_clipped.tif"
    envelope = clipper.clip(
        fixture_dem,
        destination,
        bbox=fixture_truth.AOI_BBOX,
        bbox_crs=fixture_truth.PRIMARY_CRS,
    )
    assert_ok(envelope, context_note="clipping the DEM to the AOI bbox")

    measured = runner.run_code(
        "import numpy as np\n"
        "from osgeo import gdal\n"
        f"dataset = gdal.Open({str(destination)!r})\n"
        "band = dataset.GetRasterBand(1)\n"
        "array = band.ReadAsArray()\n"
        "nodata = band.GetNoDataValue()\n"
        "mask = array == nodata if nodata is not None else np.zeros_like(array, bool)\n"
        "valid = array[~mask]\n"
        "gt = dataset.GetGeoTransform()\n"
        "result = {\n"
        "    'width': dataset.RasterXSize,\n"
        "    'height': dataset.RasterYSize,\n"
        "    'origin': [gt[0], gt[3]],\n"
        "    'pixel': [gt[1], -gt[5]],\n"
        "    'nodata_cells': int(mask.sum()),\n"
        "    'min': float(valid.min()),\n"
        "    'max': float(valid.max()),\n"
        "}\n"
    )
    assert_ok(measured, context_note="measuring the clipped DEM")
    return envelope, measured.result


def test_clipped_dem_has_the_expected_grid(clipped_dem, fixture_truth):
    """The AOI's edges fall on cell boundaries, so there is nothing to round."""
    _, measured = clipped_dem
    assert measured["width"] == fixture_truth.CLIPPED_DEM_WIDTH, (
        f"clipped width mismatch\n"
        f"    expected: {fixture_truth.CLIPPED_DEM_WIDTH}\n"
        f"    actual:   {measured['width']}"
    )
    assert measured["height"] == fixture_truth.CLIPPED_DEM_HEIGHT, (
        f"clipped height mismatch\n"
        f"    expected: {fixture_truth.CLIPPED_DEM_HEIGHT}\n"
        f"    actual:   {measured['height']}"
    )
    assert measured["pixel"] == pytest.approx(
        [fixture_truth.PIXEL_SIZE_M, fixture_truth.PIXEL_SIZE_M]
    ), f"clipping must not resample\n{describe(measured)}"


def test_clipped_dem_is_in_the_right_place(clipped_dem, fixture_truth):
    """The assertion that catches a swapped-axis clip.

    A clip that read the bbox in QGIS's order would still produce a valid
    raster — just an enormous one, origin at easting 180300. Checking the
    origin is how a wrong-place result stops being invisible.
    """
    _, measured = clipped_dem
    assert measured["origin"] == pytest.approx(
        list(fixture_truth.CLIPPED_DEM_ORIGIN)
    ), (
        f"clipped origin mismatch -- the clip took the wrong window\n"
        f"    expected: {list(fixture_truth.CLIPPED_DEM_ORIGIN)}\n"
        f"    actual:   {measured['origin']}"
    )


def test_clipped_dem_values_follow_the_base_ramp(clipped_dem, fixture_truth):
    """The clipped window misses every special region, so its range is exact.

    Rows 20-34 and columns 10-29 contain no hill, no coast, no basin and no
    nodata — so min and max follow from the ramp formula alone, and matching
    them is strong evidence the clip took precisely the intended window rather
    than one shifted by a cell or two.
    """
    _, measured = clipped_dem
    assert measured["min"] == pytest.approx(fixture_truth.CLIPPED_DEM_MIN), (
        f"clipped minimum mismatch\n"
        f"    expected: {fixture_truth.CLIPPED_DEM_MIN}\n"
        f"    actual:   {measured['min']}"
    )
    assert measured["max"] == pytest.approx(fixture_truth.CLIPPED_DEM_MAX), (
        f"clipped maximum mismatch\n"
        f"    expected: {fixture_truth.CLIPPED_DEM_MAX}\n"
        f"    actual:   {measured['max']}"
    )
    assert measured["nodata_cells"] == fixture_truth.CLIPPED_DEM_NODATA_CELLS, (
        f"no nodata should survive this clip; {measured['nodata_cells']} did"
    )


def test_clip_reports_which_algorithm_ran(clipped_dem, fixture_truth):
    """The four cases differ at the edges, so which one ran is part of the answer."""
    envelope, _ = clipped_dem
    clip = envelope.result["clip"]
    assert clip["algorithm_id"] == "gdal:cliprasterbyextent", describe(clip)
    assert clip["data_kind"] == "raster", describe(clip)
    assert clip["aoi_form"] == "bbox", describe(clip)
    assert clip["data_kind_was_detected"] is True, describe(clip)
    assert clip["qgis_extent_string"] == fixture_truth.AOI_QGIS_EXTENT_STRING


def test_wrong_extent_order_would_have_produced_a_different_raster(
    context, fixture_dem, fixture_truth, tmp_path
):
    """The failure clip_to_aoi exists to prevent, demonstrated.

    Passing the conventional (xmin, ymin, xmax, ymax) order straight through to
    the algorithm succeeds and returns a raster of nowhere. Asserting that it
    differs is what keeps this a known hazard rather than a rediscovered one.
    """
    xmin, ymin, xmax, ymax = fixture_truth.AOI_BBOX
    destination = tmp_path / "wrong_order.tif"
    envelope = context.processing.run(
        "gdal:cliprasterbyextent",
        {
            "INPUT": str(fixture_dem),
            # Deliberately unconverted.
            "PROJWIN": f"{xmin},{ymin},{xmax},{ymax} [{fixture_truth.PRIMARY_CRS}]",
            "OUTPUT": str(destination),
        },
    )
    assert_ok(
        envelope,
        context_note="the wrong order must still succeed -- that is the hazard",
    )

    measured = context.headless.run_code(
        "from osgeo import gdal\n"
        f"dataset = gdal.Open({str(destination)!r})\n"
        "result = {'width': dataset.RasterXSize, 'height': dataset.RasterYSize}\n"
    )
    assert_ok(measured)
    assert measured.result["width"] != fixture_truth.CLIPPED_DEM_WIDTH, (
        f"the wrong extent order unexpectedly produced the right grid; the "
        f"hazard this tool guards may no longer exist.\n"
        f"{describe(measured.result)}"
    )


# ---------------------------------------------------------------------------
# clip_to_aoi: vector by mask polygon and by bounding box
# ---------------------------------------------------------------------------


def test_vector_clipped_by_mask_polygon(
    clipper, runner, fixture_gpkg, fixture_truth, tmp_path
):
    """The documented 2-of-5 expectation, reached through clip_to_aoi."""
    destination = tmp_path / "points_by_mask.gpkg"
    envelope = clipper.clip(
        f"{fixture_gpkg}|layername={fixture_truth.SURVEY_POINTS_LAYER}",
        destination,
        mask=f"{fixture_gpkg}|layername={fixture_truth.AOI_BOUNDARY_LAYER}",
        data_kind="vector",
    )
    assert_ok(envelope, context_note="clipping points by the AOI polygon")
    assert envelope.result["clip"]["algorithm_id"] == "native:clip"

    measured = runner.run_code(
        "from osgeo import ogr\n"
        f"datasource = ogr.Open({str(destination)!r})\n"
        "layer = datasource.GetLayer(0)\n"
        "result = {'names': sorted(f.GetField('name') for f in layer)}\n"
    )
    assert_ok(measured)
    assert measured.result["names"] == sorted(
        fixture_truth.SURVEY_POINTS_INSIDE_AOI
    ), (
        f"clipped points mismatch\n"
        f"    expected: {sorted(fixture_truth.SURVEY_POINTS_INSIDE_AOI)}\n"
        f"    actual:   {measured.result['names']}"
    )


def test_vector_clipped_by_bbox_truncates_geometry(
    clipper, runner, fixture_gpkg, fixture_truth, tmp_path
):
    """Clipping cuts geometry, it does not merely select intersecting features.

    track_a is 500 m long and the bbox keeps 200 m of it. A tool that selected
    whole intersecting features would return 500 m and look fine — "a road
    running 20 km out of the AOI" is exactly what that failure looks like at
    acquisition scale.
    """
    destination = tmp_path / "tracks_by_bbox.gpkg"
    envelope = clipper.clip(
        f"{fixture_gpkg}|layername={fixture_truth.TRACKS_LAYER}",
        destination,
        bbox=fixture_truth.TRACK_CLIP_BBOX,
        bbox_crs=fixture_truth.PRIMARY_CRS,
        data_kind="vector",
    )
    assert_ok(envelope, context_note="clipping tracks by bbox")
    assert envelope.result["clip"]["algorithm_id"] == "native:extractbyextent"

    measured = runner.run_code(
        "from osgeo import ogr\n"
        f"datasource = ogr.Open({str(destination)!r})\n"
        "layer = datasource.GetLayer(0)\n"
        "lengths = [f.GetGeometryRef().Length() for f in layer]\n"
        "result = {'count': len(lengths), 'total_length': sum(lengths)}\n"
    )
    assert_ok(measured)
    assert measured.result["count"] == fixture_truth.TRACK_CLIP_FEATURE_COUNT, (
        f"feature count mismatch\n"
        f"    expected: {fixture_truth.TRACK_CLIP_FEATURE_COUNT}\n"
        f"    actual:   {measured.result['count']}"
    )
    assert measured.result["total_length"] == pytest.approx(
        fixture_truth.TRACK_CLIP_LENGTH_M
    ), (
        f"clipped length mismatch -- geometry was not truncated\n"
        f"    expected: {fixture_truth.TRACK_CLIP_LENGTH_M} m\n"
        f"    actual:   {measured.result['total_length']} m\n"
        f"    (the unclipped track is 500 m)"
    )


def test_clip_needs_exactly_one_aoi_form(clipper, fixture_dem, tmp_path):
    for kwargs in (
        {},
        {
            "bbox": (0.0, 0.0, 1.0, 1.0),
            "mask": "mask.gpkg",
        },
    ):
        envelope = clipper.clip(fixture_dem, tmp_path / "out.tif", **kwargs)
        assert_failed(
            envelope, ErrorCode.BAD_REQUEST, context_note=f"kwargs={sorted(kwargs)}"
        )


def test_clip_inherits_the_overwrite_guard(
    clipper, fixture_dem, fixture_truth, tmp_path
):
    """REQ-4 is enforced once, in ProcessingRunner, and applies here for free."""
    destination = tmp_path / "twice.tif"
    kwargs = {
        "bbox": fixture_truth.AOI_BBOX,
        "bbox_crs": fixture_truth.PRIMARY_CRS,
    }
    assert_ok(clipper.clip(fixture_dem, destination, **kwargs))
    assert_failed(
        clipper.clip(fixture_dem, destination, **kwargs),
        ErrorCode.CONFIRMATION_REQUIRED,
    )
    assert_ok(clipper.clip(fixture_dem, destination, confirm=True, **kwargs))


# ---------------------------------------------------------------------------
# Task 4.0: provider declarations and coverage pre-filtering
# ---------------------------------------------------------------------------


def test_every_provider_declares_what_it_needs():
    for provider in ALL_PROVIDERS:
        assert provider.data_types, f"{provider.name} declares no data types"
        assert provider.licence.summary, f"{provider.name} has no licence summary"
        assert provider.protocol_family, f"{provider.name} has no protocol family"
        if provider.requires_credentials:
            assert provider.credential is not None, (
                f"{provider.name} requires credentials but declares no "
                f"CredentialSpec, so check_api_credentials cannot report it"
            )
            assert provider.credential.registration_url, (
                f"{provider.name} has no registration URL -- a missing "
                f"credential is only actionable with somewhere to go"
            )


def test_the_four_protocol_families_are_all_represented():
    """design.md §4: the five providers span four protocol families.

    If they collapsed to one, the family bases would be unnecessary — and the
    claim the spec corrects, that every provider is config-only, would be true
    after all.
    """
    families = {provider.protocol_family for provider in ALL_PROVIDERS}
    assert families == {"rest", "static_catalogue", "overpass", "ogc"}, families


def test_coverage_prefiltering_skips_uk_providers_outside_the_uk():
    """"No querying OS Data Hub for a bounding box in Ontario" (design.md §4)."""
    uk_names = {provider.name for provider in providers_for(UK_AOI)}
    assert "os_data_hub" in uk_names, uk_names
    assert "bgs" in uk_names, uk_names

    ontario_names = {provider.name for provider in providers_for(ONTARIO_AOI)}
    assert "os_data_hub" not in ontario_names, ontario_names
    assert "bgs" not in ontario_names, ontario_names
    # Global providers still apply.
    assert "opentopography" in ontario_names, ontario_names
    assert "openstreetmap" in ontario_names, ontario_names


def test_coverage_prefiltering_answers_who_serves_a_data_type():
    """Answerable with no network traffic and no credentials at all."""
    geology = providers_for(UK_AOI, [DataType.GEOLOGY])
    assert [provider.name for provider in geology] == ["bgs"]

    geology_abroad = providers_for(ONTARIO_AOI, [DataType.GEOLOGY])
    assert geology_abroad == (), (
        f"no declared provider serves geology outside the UK; got "
        f"{[p.name for p in geology_abroad]}"
    )


def test_partial_coverage_counts_as_coverage():
    """Intersection, not containment.

    A provider covering half the requested area still has something to offer,
    and ruling it out would hide real results from the menu.
    """
    straddling = (-12.0, 50.0, 2.0, 52.0)   # part Atlantic, part UK
    assert PROVIDERS_BY_NAME["os_data_hub"].covers(straddling) is True


def test_elevation_providers_are_both_offered_for_a_uk_aoi():
    """The REQ-12 refinement needs a real choice to be worth making.

    OS Terrain is already EPSG:27700 and OpenTopography serves 4326, so a UK
    elevation request genuinely has a 5 m national option and a 30 m global
    one — which is the case design.md §5 describes.
    """
    names = {p.name for p in providers_for(UK_AOI, [DataType.ELEVATION])}
    assert names == {"os_data_hub", "opentopography"}, names


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


SPEC = CredentialSpec(
    provider="test_provider",
    config_key="test_provider",
    env_var="QGIS_MCP_TEST_PROVIDER_KEY",
    registration_url="https://example.invalid/register",
    notes="test only",
)


def test_environment_beats_the_config_file(monkeypatch):
    store = CredentialStore({"test_provider": "from-file"})
    monkeypatch.setenv(SPEC.env_var, "from-env")
    assert store.get(SPEC) == "from-env"
    assert "environment" in store.status(SPEC).source


def test_config_file_is_used_when_the_environment_is_unset(monkeypatch):
    monkeypatch.delenv(SPEC.env_var, raising=False)
    store = CredentialStore({"test_provider": "from-file"})
    assert store.get(SPEC) == "from-file"
    assert "qgis-mcp.toml" in store.status(SPEC).source


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_values_count_as_absent(monkeypatch, blank):
    """A key left unfilled in a config file is not a key.

    Treating `os_data_hub = ""` as present would turn a clear "no credential"
    into an authentication failure from the provider, several steps further
    from the cause.
    """
    monkeypatch.delenv(SPEC.env_var, raising=False)
    store = CredentialStore({"test_provider": blank})
    assert store.get(SPEC) is None
    assert store.status(SPEC).present is False


def test_status_never_discloses_the_key(monkeypatch):
    """Not even partially.

    The status is returned to an agent and printed by --check, which may be
    pasted into an issue. A prefix is still a leaked key.
    """
    secret = "super-secret-key-value-12345"
    monkeypatch.setenv(SPEC.env_var, secret)
    payload = CredentialStore().status(SPEC).to_dict()
    rendered = describe(payload)
    assert secret not in rendered, "the key value leaked into the status payload"
    for length in (6, 8, 12):
        assert secret[:length] not in rendered, (
            f"a {length}-character prefix of the key leaked into the status"
        )
        assert secret[-length:] not in rendered, (
            f"a {length}-character suffix of the key leaked into the status"
        )
    # Length is reported, because it distinguishes a real key from an empty
    # string left in a config file.
    assert payload["key_length"] == len(secret)


def test_absent_credential_reports_how_to_get_one(monkeypatch):
    monkeypatch.delenv(SPEC.env_var, raising=False)
    payload = CredentialStore().status(SPEC).to_dict()
    assert payload["present"] is False
    assert payload["registration_url"] == SPEC.registration_url
    assert SPEC.env_var in payload["action"], describe(payload)


def test_check_api_credentials_reports_every_provider(tools):
    payload = tools.tools["check_api_credentials"]()
    assert payload["ok"] is True, describe(payload)
    assert payload["mode"] == ExecutionMode.HEADLESS, describe(payload)

    reported = {entry["name"] for entry in payload["providers"]}
    assert reported == {provider.name for provider in ALL_PROVIDERS}, reported

    credentialled = {entry["provider"] for entry in payload["credentials"]}
    assert credentialled == {"os_data_hub", "opentopography"}, credentialled


def test_check_api_credentials_says_it_is_not_a_gate(tools):
    """REQ-10 made this a diagnostic rather than a required pre-flight.

    An agent that read a missing key as a blocker would refuse work the server
    can still do — a partial menu annotated "BGS: skipped, no API key" is far
    more useful than an error.
    """
    payload = tools.tools["check_api_credentials"]()
    assert "REQ-10" in payload["note"], payload["note"]
    assert "do not block" in payload["note"], payload["note"]


def test_check_api_credentials_flags_commercial_restrictions(tools):
    """The constraint that matters for paid commission work.

    OpenTopography's free key does not cover commercial integration. Surfacing
    that before a deliverable depends on it is the whole reason licence tier
    sits in the capability declaration.
    """
    payload = tools.tools["check_api_credentials"]()
    flagged = {entry["provider"] for entry in payload["commercial_attention"]}
    assert "opentopography" in flagged, describe(payload["commercial_attention"])
    note = next(
        entry["note"]
        for entry in payload["commercial_attention"]
        if entry["provider"] == "opentopography"
    )
    assert "Enterprise" in note, note


def test_licence_tiers_needing_a_commercial_check_are_marked():
    """A public-domain provider must not be flagged, or the flag means nothing."""
    assert PROVIDERS_BY_NAME["natural_earth"].licence.tier == (
        LicenceTier.PUBLIC_DOMAIN
    )
    assert PROVIDERS_BY_NAME["natural_earth"].licence.needs_commercial_check is False
    assert PROVIDERS_BY_NAME["openstreetmap"].licence.needs_commercial_check is True
    assert PROVIDERS_BY_NAME["opentopography"].licence.needs_commercial_check is True


def test_odbl_attribution_is_declared():
    """REQ-4d has to state licence tier before a download, which needs this."""
    licence = PROVIDERS_BY_NAME["openstreetmap"].licence
    assert "OpenStreetMap contributors" in licence.attribution
    assert licence.tier == LicenceTier.OPEN_SHARE_ALIKE


# ---------------------------------------------------------------------------
# select_target_crs at the tool boundary
# ---------------------------------------------------------------------------


def test_select_target_crs_tool_returns_tier_1_for_the_uk(tools):
    payload = tools.tools["select_target_crs"](aoi=list(UK_AOI))
    assert payload["ok"] is True, describe(payload)
    assert payload["tier"] == 1, describe(payload)
    assert payload["crs"] == "EPSG:27700", describe(payload)
    assert payload["requires_choice"] is False


def test_select_target_crs_tool_asks_for_a_decision_at_continental_scale(tools):
    payload = tools.tools["select_target_crs"](aoi=[-10.0, 36.0, 30.0, 60.0])
    assert payload["ok"] is True, describe(payload)
    assert payload["tier"] == 3, describe(payload)
    assert payload["requires_choice"] is True, describe(payload)
    assert "crs" not in payload, (
        f"tier 3 must not name a CRS.\n{describe(payload)}"
    )
    assert len(payload["candidates"]) >= 2, describe(payload)


def test_select_target_crs_tool_refuses_a_malformed_aoi(tools):
    payload = tools.tools["select_target_crs"](aoi=[10.0, 50.0, 5.0, 51.0])
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST, describe(payload)


def test_select_target_crs_tool_validates_raster_inputs(tools):
    payload = tools.tools["select_target_crs"](
        aoi=list(UK_AOI), raster_inputs=[{"name": "x", "crs": "EPSG:27700"}]
    )
    assert payload["ok"] is False, describe(payload)
    assert "resolution_m" in payload["error"]["message"], describe(payload)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_acquisition_tools_register_with_their_hazard_warnings(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()

    by_name = {tool.name: tool for tool in registered}
    for expected in ("clip_to_aoi", "check_api_credentials", "select_target_crs"):
        assert expected in by_name, (
            f"expected {expected} registered; found {sorted(by_name)}"
        )

    clip_description = by_name["clip_to_aoi"].description or ""
    # The bbox order is the one thing a caller cannot infer and cannot detect
    # getting wrong, so the description has to carry it.
    assert "[xmin, ymin, xmax, ymax]" in clip_description, clip_description
    assert "does not raise an error" in clip_description, clip_description

    crs_description = by_name["select_target_crs"].description or ""
    assert "requires_choice=true" in crs_description, crs_description
