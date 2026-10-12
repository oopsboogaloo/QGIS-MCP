"""Tasks 5.3, 5.5 and 5.6: texture export, mesh export, and map rendering.

`tasks.md` asks 5.6 for "headless layout render to PNG and to PDF — file
exists, correct dimensions and DPI, non-blank", and 5.3 for "file exists,
correct dimensions and format, non-blank, georeferencing bounds match the input
extent". Both are here.

**REQ-T4 applies to 5.3 and is honoured by saying so.** The requirement is that
where correctness "genuinely cannot be reduced to a structured assertion", the
test asserts the mechanical basics and the remaining visual judgement is
"explicitly flagged in the task as needing a one-off human check rather than
silently assumed covered". Nothing here claims a rendered map *looks* right —
see `test_texture_visual_judgement_is_not_covered_here`.

The sharpest test is the aspect-ratio one. QGIS expands an extent to match the
output image's aspect, so a texture UV-mapped to the requested box rather than
the rendered one is misaligned with nothing in the image to show it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import CollectingServer, assert_ok, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode


@pytest.fixture(scope="module")
def terrain(context):
    return context.terrain


@pytest.fixture(scope="module")
def tools(context):
    from qgis_mcp_server.tools import rendering

    server = CollectingServer()
    rendering.register(server, context)
    return server


# ---------------------------------------------------------------------------
# 5.6 render_map_to_path — layouts
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def layout_png(terrain, fixture_project, fixture_truth, tmp_path_factory):
    destination = tmp_path_factory.mktemp("layout") / "layout.png"
    envelope = terrain.render_layout(
        destination,
        layout=fixture_truth.PROJECT_LAYOUT_NAME,
        image_format="png",
        dpi=150,
        project_path=fixture_project,
    )
    assert_ok(envelope, context_note="exporting the fixture layout to PNG")
    return envelope.result, destination


def test_layout_renders_headless_to_png(layout_png):
    """REQ-1b's half of this tool: a Print Layout needs no GUI.

    A layout is a project object, which is why REQ-1a pointedly excludes
    `render_map_to_path` from its scope.
    """
    result, destination = layout_png
    assert destination.exists()
    assert result["file_bytes"] > 0
    assert result["is_blank"] is False, (
        f"the layout rendered a single flat colour, so nothing drew.\n"
        f"{describe(result)}"
    )


def test_layout_dimensions_follow_the_page_and_dpi(layout_png, fixture_truth):
    """297x210 mm at 150 dpi, within a pixel of rounding.

    The tolerance is deliberate: QGIS rounds the page to whole pixels and
    which way it rounds is not something this project should pin down.
    """
    result, _ = layout_png
    expected_width, expected_height = fixture_truth.layout_pixels(150)
    assert abs(result["width"] - expected_width) <= 2, (
        f"width mismatch\n"
        f"    expected: {expected_width} (+/-2)\n"
        f"    actual:   {result['width']}"
    )
    assert abs(result["height"] - expected_height) <= 2, (
        f"height mismatch\n"
        f"    expected: {expected_height} (+/-2)\n"
        f"    actual:   {result['height']}"
    )
    assert result["page_width_mm"] == fixture_truth.LAYOUT_PAGE_WIDTH_MM
    assert result["page_height_mm"] == fixture_truth.LAYOUT_PAGE_HEIGHT_MM


def test_dpi_changes_the_output_size(terrain, fixture_project, fixture_truth, tmp_path):
    """So the dpi argument is doing something rather than being accepted."""
    sizes = {}
    for dpi in (96, 200):
        destination = tmp_path / f"layout_{dpi}.png"
        envelope = terrain.render_layout(
            destination,
            layout=fixture_truth.PROJECT_LAYOUT_NAME,
            dpi=dpi,
            project_path=fixture_project,
        )
        assert_ok(envelope, context_note=f"dpi={dpi}")
        sizes[dpi] = envelope.result["width"]
    assert sizes[200] > sizes[96], sizes
    # Twice the dots, near enough twice the pixels.
    assert sizes[200] / sizes[96] == pytest.approx(200 / 96, rel=0.01), sizes


def test_layout_renders_to_pdf(terrain, fixture_project, fixture_truth, tmp_path):
    destination = tmp_path / "layout.pdf"
    envelope = terrain.render_layout(
        destination,
        layout=fixture_truth.PROJECT_LAYOUT_NAME,
        image_format="pdf",
        dpi=200,
        project_path=fixture_project,
    )
    assert_ok(envelope, context_note="exporting the layout to PDF")
    assert destination.exists()
    assert destination.read_bytes()[:5] == b"%PDF-", (
        "the output should be a real PDF, not an image with a .pdf name"
    )
    assert envelope.result["file_bytes"] > 1000


def test_unknown_layout_lists_the_available_ones(
    terrain, fixture_project, fixture_truth, tmp_path
):
    envelope = terrain.render_layout(
        tmp_path / "x.png", layout="no_such_layout", project_path=fixture_project
    )
    assert envelope.ok is False, describe(envelope.result)
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST
    assert fixture_truth.PROJECT_LAYOUT_NAME in envelope.error["available_layouts"]


def test_a_single_layout_needs_no_name(terrain, fixture_project, fixture_truth, tmp_path):
    """With one layout there is nothing to disambiguate."""
    envelope = terrain.render_layout(
        tmp_path / "only.png", project_path=fixture_project
    )
    assert_ok(envelope)
    assert envelope.result["layout"] == fixture_truth.PROJECT_LAYOUT_NAME


@pytest.mark.parametrize("dpi", [0, -10, 5000])
def test_absurd_dpi_is_refused(terrain, fixture_project, tmp_path, dpi):
    envelope = terrain.render_layout(
        tmp_path / "x.png", dpi=dpi, project_path=fixture_project
    )
    assert envelope.ok is False
    assert "dpi must be" in envelope.error["message"]


def test_rendering_without_a_project_is_refused(terrain, tmp_path):
    """Checked server-side, so the common misconfiguration costs no startup."""
    envelope = terrain.render_layout(tmp_path / "x.png", project_path=None)
    assert envelope.ok is False
    assert "needs a project" in envelope.error["message"]


# ---------------------------------------------------------------------------
# 5.6 render_map_to_path — the canvas carve-out
# ---------------------------------------------------------------------------


def test_canvas_request_names_the_missing_live_mode(tools, fixture_project, tmp_path):
    """The one request this tool cannot serve headless.

    REQ-1a excludes `render_map_to_path` from its scope *because* only the
    canvas variant is live-only. So asking for the canvas must name the missing
    mode rather than substituting a layout or a direct render -- either of
    which would return a successful-looking image of something else.
    """
    payload = tools.tools["render_map_to_path"](
        destination=str(tmp_path / "canvas.png"),
        source="canvas",
        project_path=str(fixture_project),
    )
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.MODE_UNAVAILABLE
    assert payload["error"]["missing_mode"] == ExecutionMode.LIVE
    assert "Print Layout" in payload["error"]["message"], (
        "the refusal should point at the headless alternatives"
    )
    assert not (tmp_path / "canvas.png").exists()


def test_render_tool_is_mode_agnostic():
    """design.md §3.3: the partition is declared, and this tool is not on it."""
    from qgis_mcp_server import modes

    assert "render_map_to_path" not in modes.MODE_NAMED_TOOLS
    assert "export_georeferenced_texture" not in modes.MODE_NAMED_TOOLS
    assert "export_vector_layer_for_mesh" not in modes.MODE_NAMED_TOOLS
    assert modes.resolve("render_map_to_path").mode == ExecutionMode.HEADLESS


def test_layout_format_in_a_direct_render_is_refused(tools, fixture_project, tmp_path):
    """PDF is a layout format; a direct render cannot produce one.

    Accepting it and writing a PNG named .pdf would be worse than refusing.
    """
    payload = tools.tools["render_map_to_path"](
        destination=str(tmp_path / "x.pdf"),
        image_format="pdf",
        project_path=str(fixture_project),
    )
    assert payload["ok"] is False, describe(payload)
    assert "layout format" in payload["error"]["message"]
    assert "pdf" in payload["error"]["layout_formats"]


def test_direct_render_without_an_extent_uses_the_layers(
    tools, fixture_project, fixture_truth, tmp_path
):
    """"Render this project" should frame itself on the data."""
    payload = tools.tools["render_map_to_path"](
        destination=str(tmp_path / "all.png"),
        pixel_size=5.0,
        project_path=str(fixture_project),
    )
    assert payload["ok"] is True, describe(payload)
    extent = payload["extent"]
    # The DEM spans the full square and bounds every other layer.
    assert extent == pytest.approx(list(fixture_truth.EXTENT)), (
        f"expected the layers' union\n"
        f"    expected: {list(fixture_truth.EXTENT)}\n"
        f"    actual:   {extent}"
    )


# ---------------------------------------------------------------------------
# 5.3 export_georeferenced_texture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def texture(tools, fixture_project, fixture_truth, tmp_path_factory):
    destination = tmp_path_factory.mktemp("texture") / "texture.png"
    payload = tools.tools["export_georeferenced_texture"](
        destination=str(destination),
        extent=list(fixture_truth.EXTENT),
        pixel_size=2.0,
        project_path=str(fixture_project),
    )
    assert payload["ok"] is True, describe(payload)
    return payload, destination


def test_texture_exists_with_the_expected_dimensions(texture, fixture_truth):
    """1000 m at 2 m per pixel is 500 px, in both directions."""
    payload, destination = texture
    assert destination.exists()
    expected = int(fixture_truth.EXTENT_WIDTH_M / 2.0)
    assert payload["width"] == expected, describe(payload)
    assert payload["height"] == expected, describe(payload)
    assert payload["file_bytes"] > 0


def test_texture_is_not_blank(texture):
    """REQ-T4's mechanical floor: something actually drew."""
    payload, _ = texture
    assert payload["is_blank"] is False, describe(payload)
    assert payload["distinct_colours_sampled"] > 5, (
        f"only {payload['distinct_colours_sampled']} distinct colours sampled, "
        f"which suggests little or nothing rendered"
    )


def test_pixel_size_prevents_the_extent_being_adjusted(texture, fixture_truth):
    """The whole point of preferring pixel_size.

    Deriving the output size from the extent means the aspect already matches,
    so QGIS has nothing to expand and the returned extent is the requested one.
    """
    payload, _ = texture
    assert payload["extent_was_adjusted"] is False, describe(payload)
    assert payload["extent"] == pytest.approx(list(fixture_truth.EXTENT))
    assert payload["requested_extent"] == pytest.approx(list(fixture_truth.EXTENT))
    assert payload["metres_per_pixel_x"] == pytest.approx(2.0)
    assert payload["metres_per_pixel_y"] == pytest.approx(2.0)
    assert payload["size_derived_from"] == "pixel_size"


def test_mismatched_width_and_height_expands_the_extent_and_says_so(
    tools, fixture_project, fixture_truth, tmp_path
):
    """The silent failure this tool exists to prevent, demonstrated.

    A 1000 m square asked for at 256x200 px really renders 1280 m by 1000 m.
    A texture UV-mapped to the requested box would be out by 140 m on each
    side, with nothing in the image to show it -- so the response reports both
    boxes and warns.
    """
    payload = tools.tools["render_map_to_path"](
        destination=str(tmp_path / "squashed.png"),
        extent=list(fixture_truth.EXTENT),
        width=256,
        height=200,
        project_path=str(fixture_project),
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["extent_was_adjusted"] is True, describe(payload)

    requested_width = (
        payload["requested_extent"][2] - payload["requested_extent"][0]
    )
    actual_width = payload["extent"][2] - payload["extent"][0]
    assert actual_width > requested_width, (
        f"the extent should have been expanded; requested {requested_width} m, "
        f"rendered {actual_width} m"
    )
    assert actual_width == pytest.approx(1280.0), (
        f"1000 m at a 256x200 aspect should render 1280 m; got {actual_width}"
    )
    warnings = " ".join(payload["warnings"])
    assert "expanded" in warnings, warnings
    assert "requested_extent" in warnings, (
        "the warning should say which field to UV-align against"
    )


def test_texture_tool_offers_no_height_parameter(tools):
    """Removing the footgun rather than warning about it.

    `export_georeferenced_texture` takes `pixel_size` or `width`, never both
    width and height -- offering both is how an aspect mismatch gets
    introduced, and this tool's entire value is an extent that can be trusted
    against a mesh.
    """
    import inspect

    signature = inspect.signature(tools.tools["export_georeferenced_texture"])
    assert "height" not in signature.parameters, sorted(signature.parameters)
    assert "pixel_size" in signature.parameters
    assert "width" in signature.parameters


def test_world_file_is_written_with_pixel_centre_coordinates(
    texture, fixture_truth
):
    """A world file's last two values are the upper-left pixel's *centre*.

    Half a pixel, and getting it wrong shifts every later alignment by half a
    cell -- which at 30 m is 15 m on the ground and invisible in the image.
    """
    payload, _ = texture
    world_file = Path(payload["world_file"])
    assert world_file.exists(), describe(payload)
    assert world_file.suffix == ".pgw", world_file.name

    values = [float(line) for line in world_file.read_text().split()]
    assert len(values) == 6, values
    pixel = 2.0
    assert values[0] == pytest.approx(pixel)
    assert values[1] == pytest.approx(0.0)
    assert values[2] == pytest.approx(0.0)
    assert values[3] == pytest.approx(-pixel)
    # Centre of the upper-left pixel: half a pixel in from the extent corner.
    assert values[4] == pytest.approx(fixture_truth.EXTENT[0] + pixel / 2), (
        f"the X origin should be the pixel centre "
        f"({fixture_truth.EXTENT[0] + pixel / 2}), not the extent corner "
        f"({fixture_truth.EXTENT[0]}); got {values[4]}"
    )
    assert values[5] == pytest.approx(fixture_truth.EXTENT[3] - pixel / 2), (
        f"the Y origin should be the pixel centre "
        f"({fixture_truth.EXTENT[3] - pixel / 2}); got {values[5]}"
    )


def test_world_file_makes_the_texture_loadable_as_a_raster(texture, runner):
    """The georeferencing works, read back through GDAL.

    Asserting the sidecar's contents proves the arithmetic; this proves GDAL
    agrees, which is what matters when the texture goes back into QGIS.
    """
    payload, destination = texture
    probe = runner.run_code(
        "from osgeo import gdal\n"
        f"dataset = gdal.Open({str(destination)!r})\n"
        "gt = dataset.GetGeoTransform()\n"
        "result = {\n"
        "    'origin': [gt[0], gt[3]],\n"
        "    'pixel': [gt[1], gt[5]],\n"
        "    'width': dataset.RasterXSize,\n"
        "}\n"
    )
    assert_ok(probe, context_note="reading the texture's georeferencing")
    # GDAL reports the pixel *corner*, having converted from the world file's
    # centre convention -- so this also confirms the half-pixel was right.
    assert probe.result["origin"][0] == pytest.approx(payload["extent"][0])
    assert probe.result["origin"][1] == pytest.approx(payload["extent"][3])
    assert probe.result["pixel"][0] == pytest.approx(2.0)


def test_texture_extent_matches_a_heightmap_from_the_same_extent(
    tools, terrain, fixture_dem, fixture_project, fixture_truth, tmp_path
):
    """The point of 5.3: a texture and a heightmap that line up.

    Both are rendered or exported from the same extent, so UV coordinates map
    one onto the other without adjustment. If either silently covered different
    ground, this is where it would show.
    """
    texture_payload = tools.tools["export_georeferenced_texture"](
        destination=str(tmp_path / "pair_texture.png"),
        extent=list(fixture_truth.EXTENT),
        pixel_size=4.0,
        project_path=str(fixture_project),
    )
    assert texture_payload["ok"] is True, describe(texture_payload)

    heightmap = terrain.export_heightmap(fixture_dem, tmp_path / "pair_height.png")
    assert_ok(heightmap)

    assert texture_payload["extent"] == pytest.approx(list(fixture_truth.EXTENT))
    assert heightmap.result["extent_width_m"] == pytest.approx(
        texture_payload["ground_width_m"]
    ), (
        f"the heightmap covers {heightmap.result['extent_width_m']} m and the "
        f"texture {texture_payload['ground_width_m']} m -- they would not UV-align"
    )
    assert texture_payload["uv_alignment_note"], describe(texture_payload)


def test_texture_warns_in_a_geographic_crs(
    tools, fixture_project, fixture_truth, tmp_path
):
    """Metres per pixel in degrees is not a measurement."""
    payload = tools.tools["export_georeferenced_texture"](
        destination=str(tmp_path / "geo.png"),
        extent=list(fixture_truth.GEOGRAPHIC_EXTENT),
        extent_crs="EPSG:4326",
        crs="EPSG:4326",
        width=128,
        project_path=str(fixture_project),
    )
    assert payload["ok"] is True, describe(payload)
    warnings = " ".join(payload["warnings"])
    assert "geographic" in warnings, warnings
    assert "stretched" in warnings, warnings


def test_unknown_layer_is_refused_with_the_available_names(
    tools, fixture_project, tmp_path
):
    """A texture quietly missing its overlay looks like a styling problem."""
    payload = tools.tools["export_georeferenced_texture"](
        destination=str(tmp_path / "x.png"),
        layers=["geology_that_is_not_here"],
        project_path=str(fixture_project),
    )
    assert payload["ok"] is False, describe(payload)
    assert "survey_points" in payload["error"]["available_layers"]


def test_layer_selection_is_honoured(tools, fixture_project, fixture_truth, tmp_path):
    payload = tools.tools["export_georeferenced_texture"](
        destination=str(tmp_path / "one.png"),
        layers=["dem_su4080"],
        extent=list(fixture_truth.EXTENT),
        pixel_size=10.0,
        project_path=str(fixture_project),
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["layers"] == ["dem_su4080"], describe(payload)


def test_texture_visual_judgement_is_not_covered_here(texture):
    """REQ-T4, stated rather than assumed.

    Every assertion in this file is mechanical: the file exists, its dimensions
    follow from the extent and pixel size, its georeferencing round-trips
    through GDAL, and it is not a flat colour. None of that says the map
    *reads* well -- whether the hillshade has the right exaggeration, whether
    the geology colours separate, whether labels collide.

    REQ-T4 requires that gap be flagged rather than silently assumed covered,
    so it is flagged here: **the cartographic quality of a rendered texture
    needs a one-off human look.** `terrain-demo/` holds output from a real run
    for exactly that purpose.
    """
    payload, destination = texture
    assert destination.exists()
    assert payload["is_blank"] is False
    assert payload["distinct_colours_sampled"] > 5


# ---------------------------------------------------------------------------
# 5.5 export_vector_layer_for_mesh
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def draped_geojson(tools, fixture_gpkg, fixture_dem, fixture_truth, tmp_path_factory):
    destination = tmp_path_factory.mktemp("mesh") / "tracks.geojson"
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(destination),
        layer=fixture_truth.TRACKS_LAYER,
        mesh_format="geojson",
        dem=str(fixture_dem),
    )
    assert payload["ok"] is True, describe(payload)
    return payload, json.loads(destination.read_text(encoding="utf-8"))


def test_mesh_export_samples_exact_elevations(draped_geojson, fixture_truth):
    """Hand-computable, because the vertices land on pixel centres.

    track_a's endpoints sit exactly on DEM pixel centres, and the base ramp is
    linear, so bilinear sampling returns the formula's value with nothing to
    round. 196.0 and 221.0 are derived in `truth.py`, not measured here.
    """
    payload, collection = draped_geojson
    assert payload["vertices_sampled"] == payload["vertex_count"], describe(payload)
    assert payload["vertices_outside_dem"] == 0, describe(payload)

    track_a = next(
        feature
        for feature in collection["features"]
        if feature["properties"]["name"] == "track_a"
    )
    coordinates = track_a["geometry"]["coordinates"]
    assert coordinates[0][2] == pytest.approx(fixture_truth.TRACK_A_START_Z), (
        f"start elevation mismatch\n"
        f"    expected: {fixture_truth.TRACK_A_START_Z}\n"
        f"    actual:   {coordinates[0][2]}"
    )
    assert coordinates[-1][2] == pytest.approx(fixture_truth.TRACK_A_END_Z), (
        f"end elevation mismatch\n"
        f"    expected: {fixture_truth.TRACK_A_END_Z}\n"
        f"    actual:   {coordinates[-1][2]}"
    )


def test_every_vertex_carries_three_coordinates(draped_geojson):
    """Z is the point of the exercise; a 2D export would be useless here."""
    _, collection = draped_geojson
    for feature in collection["features"]:
        geometry = feature["geometry"]
        parts = (
            [geometry["coordinates"]]
            if geometry["type"] == "LineString"
            else geometry["coordinates"]
        )
        for part in parts:
            for vertex in part:
                assert len(vertex) == 3, f"{feature['properties']} has a 2D vertex"


def test_mesh_export_counts_match_the_fixture(draped_geojson, fixture_truth):
    payload, collection = draped_geojson
    assert payload["feature_count"] == fixture_truth.TRACKS_COUNT
    assert len(collection["features"]) == fixture_truth.TRACKS_COUNT
    # track_a has 2 vertices, track_b has 3.
    assert payload["vertex_count"] == 5, describe(payload)


def test_geojson_records_the_crs_outside_the_crs_member(draped_geojson, fixture_truth):
    """RFC 7946 dropped GeoJSON's `crs` member and assumes WGS84.

    These coordinates are EPSG:27700. Putting them in a `crs` member would be
    ignored by a conforming reader or, worse, read as a claim that they are
    lat/lon -- so the CRS goes in a namespaced member instead.
    """
    _, collection = draped_geojson
    assert "crs" not in collection, (
        "a `crs` member would be ignored or misread; use the namespaced one"
    )
    metadata = collection["qgis_mcp"]
    assert metadata["crs"] == fixture_truth.PRIMARY_CRS
    assert "NOT WGS84" in metadata["note"], metadata["note"]


def test_obj_export_uses_local_metres(
    tools, fixture_gpkg, fixture_dem, fixture_truth, tmp_path
):
    """Blender loses centimetre accuracy at a 440,000 m easting.

    Single-precision floats, and the symptom is jitter along a road rather than
    an error anywhere -- so OBJ is local by default and reports its origin.
    """
    destination = tmp_path / "tracks.obj"
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(destination),
        layer=fixture_truth.TRACKS_LAYER,
        mesh_format="obj",
        dem=str(fixture_dem),
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["local_coordinates"] is True
    assert payload["local_origin"] == pytest.approx([440050.0, 180050.0])
    assert payload["obj_polyline_count"] == fixture_truth.TRACKS_COUNT

    text = destination.read_text(encoding="utf-8")
    vertices = [line for line in text.splitlines() if line.startswith("v ")]
    polylines = [line for line in text.splitlines() if line.startswith("l ")]
    assert len(vertices) == 5, vertices
    assert len(polylines) == fixture_truth.TRACKS_COUNT

    # track_a starts at the origin and runs 500 m east at its sampled height.
    assert vertices[0] == f"v 0.0000 0.0000 {fixture_truth.TRACK_A_START_Z:.4f}"
    assert vertices[1] == f"v 500.0000 0.0000 {fixture_truth.TRACK_A_END_Z:.4f}"
    assert "# crs: EPSG:27700" in text
    assert "Z up" in text


def test_obj_indices_are_one_based(tools, fixture_gpkg, fixture_truth, tmp_path):
    """OBJ is 1-indexed; a 0 index makes the file unreadable to Blender."""
    destination = tmp_path / "idx.obj"
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(destination),
        layer=fixture_truth.TRACKS_LAYER,
        mesh_format="obj",
    )
    assert payload["ok"] is True, describe(payload)
    for line in destination.read_text().splitlines():
        if line.startswith("l "):
            indices = [int(value) for value in line.split()[1:]]
            assert min(indices) >= 1, line
            assert max(indices) <= payload["vertex_count"], line


def test_without_a_dem_the_result_is_flat_and_says_so(
    tools, fixture_gpkg, fixture_truth, tmp_path
):
    destination = tmp_path / "flat.geojson"
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(destination),
        layer=fixture_truth.TRACKS_LAYER,
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["vertices_sampled"] == 0
    warnings = " ".join(payload["warnings"])
    assert "Z = 0" in warnings, warnings
    assert "flat" in warnings, warnings

    collection = json.loads(destination.read_text())
    track = collection["features"][0]["geometry"]["coordinates"]
    assert all(vertex[2] == 0.0 for vertex in track), track


def test_z_scale_exaggerates(tools, fixture_gpkg, fixture_dem, fixture_truth, tmp_path):
    destination = tmp_path / "exaggerated.geojson"
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(destination),
        layer=fixture_truth.TRACKS_LAYER,
        dem=str(fixture_dem),
        z_scale=3.0,
    )
    assert payload["ok"] is True, describe(payload)
    collection = json.loads(destination.read_text())
    track_a = next(
        feature
        for feature in collection["features"]
        if feature["properties"]["name"] == "track_a"
    )
    assert track_a["geometry"]["coordinates"][0][2] == pytest.approx(
        fixture_truth.TRACK_A_START_Z * 3.0
    )


def test_crs_mismatch_between_layer_and_dem_is_refused(
    tools, fixture_gpkg, fixture_dir, fixture_truth, tmp_path
):
    """Sampling across CRSs returns plausible numbers from the wrong places.

    The layer is EPSG:27700 and the geographic DEM is EPSG:4326. Comparing
    coordinates across them would read elevations from somewhere off the coast
    of Africa and report them without complaint, so it is refused.
    """
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(tmp_path / "x.geojson"),
        layer=fixture_truth.TRACKS_LAYER,
        dem=str(fixture_dir / fixture_truth.GEOGRAPHIC_DEM_NAME),
    )
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST
    assert "geographic" in payload["error"]["message"], payload["error"]["message"]


def test_unknown_layer_lists_the_available_ones(tools, fixture_gpkg, tmp_path):
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(tmp_path / "x.geojson"),
        layer="no_such_layer",
    )
    assert payload["ok"] is False, describe(payload)
    assert "tracks" in payload["error"]["available_layers"]


def test_polygon_layer_exports_as_a_boundary(
    tools, fixture_gpkg, fixture_dem, fixture_truth, tmp_path
):
    """A polygon's ring becomes a polyline, which Blender can fill."""
    destination = tmp_path / "boundary.obj"
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(destination),
        layer=fixture_truth.AOI_BOUNDARY_LAYER,
        mesh_format="obj",
        dem=str(fixture_dem),
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["feature_count"] == fixture_truth.AOI_BOUNDARY_COUNT
    # A closed 4-corner ring: 5 vertices, first repeated last.
    assert payload["vertex_count"] == 5, describe(payload)
    assert payload["obj_polyline_count"] == 1


def test_bad_mesh_format_is_refused(tools, fixture_gpkg, tmp_path):
    payload = tools.tools["export_vector_layer_for_mesh"](
        source=str(fixture_gpkg),
        destination=str(tmp_path / "x.svg"),
        mesh_format="svg",
    )
    assert payload["ok"] is False, describe(payload)
    assert "geojson" in payload["error"]["supported_formats"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_all_three_tools_register_with_their_hazards(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()
    descriptions = {tool.name: tool.description or "" for tool in registered}

    for name in (
        "render_map_to_path",
        "export_georeferenced_texture",
        "export_vector_layer_for_mesh",
    ):
        assert name in descriptions, sorted(descriptions)

    # The three things a caller cannot infer and cannot detect getting wrong.
    assert "ALWAYS use the returned" in descriptions["export_georeferenced_texture"]
    assert "140 m" in descriptions["export_georeferenced_texture"]
    assert "live QGIS session" in descriptions["render_map_to_path"]
    assert "same projected CRS" in descriptions["export_vector_layer_for_mesh"]
