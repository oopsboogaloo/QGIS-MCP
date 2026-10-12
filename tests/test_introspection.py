"""Task 2: the five read-only introspection tools.

tasks.md asks for exact fixture properties — "feature count, CRS EPSG code,
field names and types" — so every assertion here compares against `truth.py`
rather than against whatever the project happens to contain.

Task 2 also "establishes the mode-reporting convention before anything depends
on it", so REQ-1b is checked on all five tools rather than on a representative
one.

Each tool call costs a QGIS startup, so the responses are gathered once at
module scope and asserted from several angles.
"""

from __future__ import annotations

import shutil

import pytest

from conftest import describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode

#: The five tools, with the introspection operation each one dispatches.
INTROSPECTION_TOOLS = [
    ("get_project_summary", "project_summary"),
    ("get_layer_summary", "layer_summary"),
    ("get_layers_tree", "layers_tree"),
    ("get_missing_layers", "missing_layers"),
    ("get_project_path_info", "project_path_info"),
]


def call(tools, name, **kwargs):
    """Invoke a registered tool and fail loudly if it reported an error."""
    payload = tools.tools[name](**kwargs)
    assert payload["ok"] is True, (
        f"{name} failed.\nerror:\n{describe(payload.get('error'))}"
    )
    return payload


# ---------------------------------------------------------------------------
# Shared responses
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def project_summary(tools, fixture_project):
    return call(tools, "get_project_summary", project_path=str(fixture_project))


@pytest.fixture(scope="module")
def layers_tree(tools, fixture_project):
    return call(tools, "get_layers_tree", project_path=str(fixture_project))


@pytest.fixture(scope="module")
def path_info(tools, fixture_project):
    return call(tools, "get_project_path_info", project_path=str(fixture_project))


@pytest.fixture(scope="module")
def layer_summaries(tools, fixture_project, fixture_truth):
    """Every fixture layer described, in one pass."""
    return {
        name: call(
            tools,
            "get_layer_summary",
            layer=name,
            project_path=str(fixture_project),
        )
        for name in fixture_truth.LAYER_SUMMARIES
    }


@pytest.fixture(scope="module")
def orphaned_project(fixture_project, tmp_path_factory):
    """The project file copied away from its data, breaking every reference.

    Possible only because the fixture stores layer paths *relative* to the
    project: copying the ``.qgz`` alone into an empty directory makes all four
    references resolve to files that are not there. That is a real
    moved-project failure rather than a hand-corrupted file, and it needs no
    extra fixture to produce.
    """
    destination = tmp_path_factory.mktemp("orphaned") / fixture_project.name
    shutil.copy2(fixture_project, destination)
    return destination


# ---------------------------------------------------------------------------
# get_project_summary
# ---------------------------------------------------------------------------


def test_project_summary_counts_layers_by_type(project_summary, fixture_truth):
    assert project_summary["layer_count"] == fixture_truth.PROJECT_LAYER_COUNT, (
        f"layer count mismatch\n"
        f"    expected: {fixture_truth.PROJECT_LAYER_COUNT}\n"
        f"    actual:   {project_summary['layer_count']}"
    )
    expected = {
        "vector": fixture_truth.PROJECT_VECTOR_LAYER_COUNT,
        "raster": fixture_truth.PROJECT_RASTER_LAYER_COUNT,
    }
    assert project_summary["layer_counts"] == expected, (
        f"layer counts by type mismatch\n"
        f"    expected: {describe(expected)}\n"
        f"    actual:   {describe(project_summary['layer_counts'])}"
    )


def test_project_summary_reports_crs_and_title(project_summary, fixture_truth):
    assert project_summary["crs"] == fixture_truth.PROJECT_CRS, describe(project_summary)
    assert project_summary["title"] == fixture_truth.PROJECT_TITLE, describe(
        project_summary
    )


def test_project_summary_extent_is_the_union_in_the_project_crs(
    project_summary, fixture_truth
):
    """The DEM spans the full 1 km square, so it bounds everything else.

    All fixture layers sit inside the DEM extent, which makes the union exactly
    the DEM extent — a hand-checkable number rather than an emergent one.
    """
    assert project_summary["extent"] == pytest.approx(list(fixture_truth.EXTENT)), (
        f"project extent mismatch\n"
        f"    expected: {list(fixture_truth.EXTENT)}\n"
        f"    actual:   {project_summary['extent']}"
    )
    assert project_summary["extent_crs"] == fixture_truth.PRIMARY_CRS, describe(
        project_summary
    )


def test_project_summary_reports_no_invalid_layers(project_summary):
    assert project_summary["invalid_layer_count"] == 0, (
        f"the fixture project should resolve every layer.\n"
        f"{describe(project_summary)}"
    )
    assert project_summary["all_layers_loaded"] is True, describe(project_summary)


def test_project_summary_layout_count(project_summary, fixture_truth):
    assert project_summary["layout_count"] == fixture_truth.PROJECT_LAYOUT_COUNT
    assert project_summary["layouts"] == []


def test_active_layer_is_reported_unavailable_not_null(project_summary):
    """A GUI concept, absent headless — and the distinction is reported.

    Returning a bare null would read as "no layer is active", which is a claim
    about the project. The truth is that the question has no answer on this
    path, and REQ-1b's reported mode is what tells the caller why.
    """
    assert project_summary["active_layer"] is None, describe(project_summary)
    assert project_summary["active_layer_available"] is False, describe(project_summary)
    assert "live mode" in project_summary["active_layer_note"], describe(project_summary)


# ---------------------------------------------------------------------------
# get_layer_summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layer_name", ["dem_su4080", "survey_points", "aoi_boundary", "tracks"]
)
def test_layer_summary_matches_declared_properties(
    layer_summaries, fixture_truth, layer_name
):
    """Exact match against known fixture properties, per §9.3's test table."""
    expected = fixture_truth.LAYER_SUMMARIES[layer_name]
    actual = layer_summaries[layer_name]
    for key, want in expected.items():
        got = actual.get(key)
        if isinstance(want, list):
            assert got == pytest.approx(want), (
                f"{layer_name}.{key} mismatch\n"
                f"    expected: {want}\n"
                f"    actual:   {got}"
            )
        else:
            assert got == want, (
                f"{layer_name}.{key} mismatch\n"
                f"    expected: {want!r}\n"
                f"    actual:   {got!r}\n"
                f"full summary:\n{describe(actual)}"
            )


def test_layer_summary_reports_field_types_not_just_names(
    layer_summaries, fixture_truth
):
    """Field *types* are what §9.3 asks for, and they carry the real risk.

    A name tells a caller a column exists; the type tells it whether an
    expression will work. ``elevation_m`` being Real rather than String is the
    difference between arithmetic and a silent string comparison.
    """
    fields = layer_summaries[fixture_truth.SURVEY_POINTS_LAYER]["fields"]
    assert fields["elevation_m"] == "Real", describe(fields)
    assert fields["id"] == "Integer", describe(fields)
    assert fields["name"] == "String", describe(fields)


def test_layer_summary_includes_the_geopackage_fid(layer_summaries, fixture_truth):
    """PyQGIS surfaces the GeoPackage primary key; OGR does not.

    Asserted rather than tolerated, because the two readers disagree and a
    caller writing an expression needs to know which view it is getting. The
    field count follows from it.
    """
    summary = layer_summaries[fixture_truth.SURVEY_POINTS_LAYER]
    assert fixture_truth.GPKG_FID_FIELD in summary["fields"], describe(summary["fields"])
    assert summary["fields"][fixture_truth.GPKG_FID_FIELD] == fixture_truth.GPKG_FID_TYPE
    assert summary["field_count"] == len(fixture_truth.SURVEY_POINTS_FIELDS) + 1


def test_layer_summary_reports_source_path_without_provider_options(
    layer_summaries, fixture_truth, fixture_gpkg
):
    """``source`` keeps the layer URI; ``source_path`` is just the file.

    A caller checking whether a dataset exists on disk needs the path; one
    re-opening the same layer needs the whole URI including ``layername=``.
    """
    summary = layer_summaries[fixture_truth.TRACKS_LAYER]
    assert "layername=" in summary["source"], describe(summary)
    assert summary["source_path"].endswith(fixture_gpkg.name), describe(summary)
    assert "|" not in summary["source_path"], describe(summary)


def test_raster_layer_summary_reports_the_nodata_sentinel(
    layer_summaries, fixture_truth
):
    """The value REQ-11 turns on, read back through the layer API."""
    summary = layer_summaries["dem_su4080"]
    assert summary["nodata_value"] == fixture_truth.DEM_NODATA, describe(summary)
    assert summary["data_type"] == "Float32", describe(summary)
    assert summary["band_count"] == fixture_truth.DEM_BAND_COUNT, describe(summary)


def test_vector_summary_has_no_raster_fields_and_vice_versa(
    layer_summaries, fixture_truth
):
    """Keys follow the layer type, so absence is informative.

    A vector carrying ``band_count: null`` would invite a caller to treat it as
    a raster with no bands.
    """
    vector = layer_summaries[fixture_truth.AOI_BOUNDARY_LAYER]
    raster = layer_summaries["dem_su4080"]
    for absent in ("band_count", "data_type", "width", "height"):
        assert absent not in vector, f"vector summary should not carry {absent!r}"
    for absent in ("feature_count", "fields", "geometry_type"):
        assert absent not in raster, f"raster summary should not carry {absent!r}"


def test_unknown_layer_name_returns_the_available_names(tools, fixture_project):
    """The requirements' recurring contract (§0) applied to an unknown name.

    "Return the candidates rather than guessing, and never fail bare" is stated
    as the default shape for any underdetermined input, not only for the four
    requirements that named it.
    """
    payload = tools.tools["get_layer_summary"](
        layer="no_such_layer", project_path=str(fixture_project)
    )
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST, describe(payload)
    available = payload["error"]["available_layers"]
    assert "survey_points" in available, (
        f"the error must list the layers that do exist.\n{describe(payload['error'])}"
    )
    assert len(available) == 4, describe(available)


# ---------------------------------------------------------------------------
# get_layers_tree
# ---------------------------------------------------------------------------


def test_layers_tree_preserves_groups_and_order(layers_tree, fixture_truth):
    """The declared structure, compared on the declared keys only.

    The tool returns richer nodes than `truth.py` declares — layer ids, types,
    validity — so the response is projected down to the declared keys. The
    alternative, declaring every returned key, would make `truth.py` a copy of
    the implementation rather than a statement about the fixture.
    """

    def project(nodes):
        reduced = []
        for node in nodes:
            entry = {
                "kind": node["kind"],
                "name": node["name"],
                "visible": node["visible"],
            }
            if node["kind"] == "group":
                entry["children"] = project(node["children"])
            reduced.append(entry)
        return reduced

    actual = project(layers_tree["tree"])
    assert actual == fixture_truth.PROJECT_TREE, (
        f"layer tree mismatch\n"
        f"    expected: {describe(fixture_truth.PROJECT_TREE)}\n"
        f"    actual:   {describe(actual)}"
    )


def test_layers_tree_reports_the_hidden_layer(layers_tree, fixture_truth):
    """Visibility is per-node, and one fixture layer is unchecked.

    Without this the tree could be a flat list of names and every other
    assertion would still pass.
    """
    group = next(node for node in layers_tree["tree"] if node["kind"] == "group")
    hidden = [
        node["name"] for node in group["children"] if not node["visible"]
    ]
    assert hidden == [fixture_truth.PROJECT_HIDDEN_LAYER], (
        f"expected exactly {fixture_truth.PROJECT_HIDDEN_LAYER!r} to be hidden\n"
        f"    actual hidden: {hidden}"
    )


def test_layers_tree_nests_rather_than_flattens(layers_tree, fixture_truth):
    """Group membership must be structural, not a flat list with a label."""
    top_level = {node["name"] for node in layers_tree["tree"]}
    assert fixture_truth.PROJECT_GROUP_NAME in top_level, describe(top_level)
    assert fixture_truth.SURVEY_POINTS_LAYER not in top_level, (
        f"{fixture_truth.SURVEY_POINTS_LAYER!r} is inside a group and must not "
        f"appear at the top level.\ntop level: {top_level}"
    )
    assert layers_tree["layer_count"] == fixture_truth.PROJECT_LAYER_COUNT


def test_layers_tree_carries_layer_identity(layers_tree):
    """Each layer node names the layer it stands for, so it can be followed up."""
    group = next(node for node in layers_tree["tree"] if node["kind"] == "group")
    for node in group["children"]:
        assert node["layer_id"], describe(node)
        assert node["is_valid"] is True, describe(node)
        assert node["layer_type"] == "Vector", describe(node)


# ---------------------------------------------------------------------------
# get_missing_layers
# ---------------------------------------------------------------------------


def test_intact_project_has_no_missing_layers(tools, fixture_project):
    payload = call(tools, "get_missing_layers", project_path=str(fixture_project))
    assert payload["missing_count"] == 0, describe(payload)
    assert payload["missing_layers"] == [], describe(payload)


def test_orphaned_project_reports_every_broken_reference(
    tools, orphaned_project, fixture_truth
):
    """A moved project, as it actually fails."""
    payload = call(tools, "get_missing_layers", project_path=str(orphaned_project))
    assert payload["missing_count"] == fixture_truth.PROJECT_LAYER_COUNT, (
        f"every layer should be missing in the orphaned copy\n"
        f"    expected: {fixture_truth.PROJECT_LAYER_COUNT}\n"
        f"    actual:   {payload['missing_count']}\n"
        f"{describe(payload['missing_layers'])}"
    )
    names = sorted(entry["name"] for entry in payload["missing_layers"])
    assert names == sorted(fixture_truth.PROJECT_LAYER_NAMES), (
        f"missing layer names mismatch\n"
        f"    expected: {sorted(fixture_truth.PROJECT_LAYER_NAMES)}\n"
        f"    actual:   {names}"
    )


def test_missing_layer_entries_name_the_path_and_the_reason(
    tools, orphaned_project
):
    """"File absent" and "file present but unreadable" point at different fixes.

    Reporting both as "invalid" would send the reader hunting for which one
    they have.
    """
    payload = call(tools, "get_missing_layers", project_path=str(orphaned_project))
    entry = payload["missing_layers"][0]
    assert entry["is_valid"] is False, describe(entry)
    assert entry["source_path"], describe(entry)
    assert "does not exist" in entry["invalid_reason"], (
        f"a moved project's layers are absent, not unreadable.\n"
        f"reason: {entry['invalid_reason']}"
    )


def test_orphaned_project_still_reports_its_layers(tools, orphaned_project, fixture_truth):
    """A project with broken layers must still be introspectable.

    ``QgsProject.read`` returns False when any layer fails to load, which is
    the normal condition for this very tool. Treating that as a fatal read
    error would make broken projects — the only ones worth asking about —
    exactly the ones that cannot be asked about.
    """
    payload = call(tools, "get_project_summary", project_path=str(orphaned_project))
    assert payload["layer_count"] == fixture_truth.PROJECT_LAYER_COUNT, describe(payload)
    assert payload["invalid_layer_count"] == fixture_truth.PROJECT_LAYER_COUNT
    assert payload["all_layers_loaded"] is False, describe(payload)


# ---------------------------------------------------------------------------
# get_project_path_info
# ---------------------------------------------------------------------------


def test_path_info_reports_the_file(path_info, fixture_project, fixture_truth):
    assert path_info["exists"] is True, describe(path_info)
    assert path_info["file_path"].endswith(fixture_project.name), describe(path_info)
    assert path_info["file_size_bytes"] > 0, describe(path_info)
    assert path_info["crs"] == fixture_truth.PROJECT_CRS, describe(path_info)
    assert path_info["title"] == fixture_truth.PROJECT_TITLE, describe(path_info)


def test_path_info_reports_relative_path_storage(path_info):
    """Relative storage is what makes the project portable.

    The generator sets it deliberately, and it is the property the orphaned-copy
    test depends on — so it is asserted rather than assumed.
    """
    assert path_info["file_path_storage"] == "Relative", (
        f"the fixture project must store layer paths relatively.\n"
        f"{describe(path_info)}"
    )


def test_path_info_reports_the_saving_qgis_version(path_info):
    assert path_info["last_saved_by_qgis_version"], describe(path_info)
    assert path_info["last_saved_by_qgis_version"].startswith("4."), (
        f"the fixture should have been written by QGIS 4.x\n{describe(path_info)}"
    )
    assert path_info["last_save_date_time"], describe(path_info)


def test_path_info_is_never_dirty_headless(path_info):
    """Honest rather than useful: the project was read moments ago."""
    assert path_info["is_dirty"] is False, describe(path_info)


# ---------------------------------------------------------------------------
# REQ-1b: the mode-reporting convention, on all five
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name,operation", INTROSPECTION_TOOLS)
def test_introspection_tools_are_mode_agnostic(tool_name, operation):
    """design.md §3.3: REQ-1a's scope is a list, and §4.2 is not on it."""
    from qgis_mcp_server import introspection, modes

    assert tool_name not in modes.MODE_NAMED_TOOLS, (
        f"{tool_name} is mode-agnostic under REQ-1b and must not appear in the "
        f"mode-named partition"
    )
    assert operation in introspection.OPERATIONS, (
        f"{operation!r} must be a supported operation; got "
        f"{introspection.OPERATIONS}"
    )
    decision = modes.resolve(tool_name)
    assert decision.mode == ExecutionMode.HEADLESS, decision
    assert decision.reason, decision


@pytest.mark.parametrize("tool_name,_operation", INTROSPECTION_TOOLS)
def test_every_introspection_tool_reports_its_mode(
    tools, fixture_project, tool_name, _operation
):
    """Task 2 establishes the convention, so all five are checked."""
    kwargs = {"project_path": str(fixture_project)}
    if tool_name == "get_layer_summary":
        kwargs["layer"] = "survey_points"
    payload = tools.tools[tool_name](**kwargs)
    assert payload["mode"] == ExecutionMode.HEADLESS, describe(payload)
    assert payload["mode_reason"], (
        f"{tool_name} must report why a mode served the call, not just which."
        f"\n{describe(payload)}"
    )


@pytest.mark.parametrize("tool_name,_operation", INTROSPECTION_TOOLS)
def test_missing_project_is_refused_without_a_qgis_startup(
    context, tool_name, _operation
):
    """With no project there is nothing to report, and saying so is free.

    Checked server-side: the child enforces the same rule, but reaching it
    costs a two-second QGIS startup to learn something already knowable.
    """
    from qgis_mcp_server import introspection

    operation = dict(INTROSPECTION_TOOLS)[tool_name]
    envelope = introspection.query(context.headless, operation, project_path=None)
    assert envelope.ok is False, describe(envelope.result)
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST, describe(envelope.error)
    assert "project" in envelope.error["message"].lower(), describe(envelope.error)


def test_nonexistent_project_names_the_path(context, tmp_path):
    from qgis_mcp_server import introspection

    missing = tmp_path / "absent.qgz"
    envelope = introspection.query(
        context.headless, "project_summary", project_path=missing
    )
    assert envelope.ok is False
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST, describe(envelope.error)
    assert envelope.error["project_path"] == str(missing), describe(envelope.error)


def test_unknown_operation_is_refused(context):
    from qgis_mcp_server import introspection

    envelope = introspection.query(context.headless, "not_an_operation")
    assert envelope.ok is False
    assert envelope.error["code"] == ErrorCode.BAD_REQUEST, describe(envelope.error)
    assert "project_summary" in envelope.error["supported_operations"]


# ---------------------------------------------------------------------------
# Relative project paths across the process boundary
# ---------------------------------------------------------------------------


def test_relative_project_path_resolves(context, fixture_project):
    """A relative path must mean the same thing on both sides of the boundary.

    The server validates the path and the child opens it. When the two
    processes had different working directories, a relative path the server
    confirmed would fail to open in the child — reported as an unreadable
    project rather than as the path confusion it was.
    """
    import os

    relative = os.path.relpath(fixture_project, os.getcwd())
    assert not os.path.isabs(relative)

    from qgis_mcp_server import introspection

    envelope = introspection.query(
        context.headless, "project_summary", project_path=relative
    )
    assert envelope.ok is True, (
        f"a relative project path should resolve.\n"
        f"relative: {relative}\nerror: {describe(envelope.error)}"
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_all_five_tools_register_with_descriptions(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()

    by_name = {tool.name: tool for tool in registered}
    for tool_name, _operation in INTROSPECTION_TOOLS:
        assert tool_name in by_name, (
            f"expected {tool_name} to be registered; found {sorted(by_name)}"
        )
        description = by_name[tool_name].description or ""
        assert len(description) > 80, (
            f"{tool_name} needs a description an agent can act on.\n"
            f"description: {description!r}"
        )

    # The two facts a caller cannot infer from the signatures.
    assert "live mode" in (by_name["get_project_summary"].description or ""), (
        "get_project_summary must say the active layer is live-only"
    )
    assert "fid" in (by_name["get_layer_summary"].description or ""), (
        "get_layer_summary must warn that GeoPackage fid appears in the schema"
    )
