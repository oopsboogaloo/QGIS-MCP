"""Task 3: ``run_processing_algorithm`` and ``search_processing_algorithms``.

Covers REQ-7 (an unrecognised id returns fuzzy matches, never a bare failure),
REQ-1b (the serving mode is reported), REQ-4 (destructive destinations are
gated), and `design.md` §6.4 (providers are reported as actually registered).

The hand-calculable outputs tasks.md asks for are declared in `truth.py` rather
than inline, so the arithmetic is reviewable in one place. The raster statistics
case is the strongest of them: it checks the DEM's full `SUM`, derived in closed
form from the declared pattern, which validates every cell rather than only the
extremes — and confirms Processing honours the nodata sentinel, since the
sentinel is excluded from both `COUNT` and `SUM`.
"""

from __future__ import annotations

import pytest

from conftest import assert_failed, assert_ok, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode


@pytest.fixture(scope="module")
def catalogue(processing):
    """The registered providers and algorithms, fetched once."""
    return processing.catalogue()


# ---------------------------------------------------------------------------
# design.md 6.4: report what is actually registered
# ---------------------------------------------------------------------------


def test_catalogue_reports_registered_providers(catalogue):
    """The providers come from the running QGIS, not from a hardcoded list."""
    assert catalogue.providers, "no Processing providers were reported at all"
    for required in ("native", "gdal"):
        assert required in catalogue.providers, (
            f"provider {required!r} must be registered in a working QGIS 4.2 "
            f"install; got {sorted(catalogue.providers)}"
        )
        assert catalogue.providers[required].algorithm_count > 0, (
            f"provider {required!r} is registered but reported no algorithms"
        )
    assert "native:buffer" in catalogue, (
        f"native:buffer must be registered; catalogue holds "
        f"{len(catalogue.algorithms)} algorithms"
    )


def test_saga_absence_is_reported_with_its_reason(catalogue):
    """The task-3 note: absence must be plain, not implied.

    SAGA has not been a core provider since QGIS 3.30 and needs a third-party
    plugin this server cannot install, so it should be absent here — and the
    report has to say why rather than leaving a caller to infer it from a
    missing entry.
    """
    assert "saga" not in catalogue.providers, (
        f"saga unexpectedly registered — the fixture expectation that SAGA "
        f"needs a plugin no longer holds: {sorted(catalogue.providers)}"
    )
    report = catalogue.absence_report("saga")
    unavailable = report.get("unavailable_providers") or {}
    assert "saga" in unavailable, describe(report)
    reason = unavailable["saga"].lower()
    assert "plugin" in reason, (
        f"the SAGA explanation must say a plugin is needed.\nreason: {reason}"
    )
    assert "saga" not in report["registered_providers"], describe(report)


def test_absent_provider_explanation_names_grass_requirements(catalogue):
    """GRASS is built in but needs GRASS itself present (design.md §6.4).

    Written to hold whichever way this install is configured: if GRASS is
    registered there is nothing to explain, and if it is not, the explanation
    must name the real prerequisite.
    """
    report = catalogue.absence_report("grass")
    if "grass" in catalogue.providers:
        assert "grass" not in (report.get("unavailable_providers") or {})
        return
    reason = (report.get("unavailable_providers") or {})["grass"].lower()
    assert "grass" in reason, describe(report)


def test_registered_provider_is_not_reported_as_absent(catalogue):
    """The absence report must not cry wolf over a provider that is present."""
    report = catalogue.absence_report("native")
    assert "native" not in (report.get("unavailable_providers") or {}), describe(report)


# ---------------------------------------------------------------------------
# REQ-7: fuzzy matching on an unrecognised id
# ---------------------------------------------------------------------------


def test_wrong_provider_prefix_suggests_the_registered_one(processing):
    """REQ-7's own worked example: ``native:buffer`` versus ``qgis:buffer``.

    The requirement exists to "reduce failed round-trips from slightly-wrong
    IDs", citing exactly this pair as differing across QGIS versions. The right
    algorithm under a different provider must rank first — it is nearly always
    what was meant.
    """
    envelope = processing.run("qgis:buffer", {})
    assert_failed(envelope, ErrorCode.ALGORITHM_NOT_FOUND)
    suggestions = [entry["id"] for entry in envelope.error["suggestions"]]
    assert suggestions, describe(envelope.error)
    assert suggestions[0] == "native:buffer", (
        f"the same algorithm under a registered provider must rank first\n"
        f"    expected first: 'native:buffer'\n"
        f"    actual order:   {suggestions[:5]}"
    )


def test_misspelled_id_returns_close_matches(processing):
    envelope = processing.run("native:bufer", {})
    assert_failed(envelope, ErrorCode.ALGORITHM_NOT_FOUND)
    suggestions = [entry["id"] for entry in envelope.error["suggestions"]]
    assert "native:buffer" in suggestions, (
        f"expected native:buffer among the suggestions for 'native:bufer'\n"
        f"    actual: {suggestions}"
    )


def test_partial_name_returns_close_matches(processing):
    """A half-remembered id should still land somewhere useful."""
    envelope = processing.run("native:zonalstats", {})
    assert_failed(envelope, ErrorCode.ALGORITHM_NOT_FOUND)
    suggestions = [entry["id"] for entry in envelope.error["suggestions"]]
    assert any("zonalstatistics" in candidate for candidate in suggestions), (
        f"expected a zonal statistics algorithm among the suggestions\n"
        f"    actual: {suggestions}"
    )


def test_unknown_id_never_fails_bare(processing):
    """Even with nothing similar, the error must point somewhere.

    The requirements' recurring contract (§0) is to return candidates and never
    fail bare. With no candidates, naming the tool that lists them is the
    minimum that still leaves the caller a next move.
    """
    envelope = processing.run("native:zzzznotathingatall", {})
    assert_failed(envelope, ErrorCode.ALGORITHM_NOT_FOUND)
    assert "search_processing_algorithms" in envelope.error["message"], (
        f"a suggestion-less failure must name the discovery tool.\n"
        f"message: {envelope.error['message']}"
    )
    assert "suggestions" in envelope.error, describe(envelope.error)


def test_unregistered_provider_is_explained_not_merely_unmatched(processing):
    """An absent *provider* is a different answer from a near-miss id.

    Offering spelling suggestions for ``saga:slopeaspectcurvature`` would imply
    the algorithm exists somewhere reachable. It does not, and cannot be made
    to, so the error says that instead.
    """
    envelope = processing.run("saga:slopeaspectcurvature", {})
    assert_failed(envelope, ErrorCode.ALGORITHM_NOT_FOUND)
    detail = envelope.error.get("provider_unavailable")
    assert detail is not None, (
        f"expected a provider_unavailable explanation.\n{describe(envelope.error)}"
    )
    assert detail["provider"] == "saga", describe(detail)
    assert "out of scope" in detail["reason"].lower(), (
        f"the reason must say plugin installation is out of scope.\n"
        f"reason: {detail['reason']}"
    )
    assert "native" in detail["registered_providers"], describe(detail)


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "buffer", "native:", ":buffer", "native buffer", "native:buf fer"],
)
def test_malformed_id_is_rejected_before_launching_qgis(processing, bad_id):
    """A malformed id cannot match, so it is refused without a QGIS startup."""
    envelope = processing.run(bad_id, {})
    assert_failed(
        envelope, ErrorCode.BAD_REQUEST, context_note=f"algorithm_id={bad_id!r}"
    )


# ---------------------------------------------------------------------------
# Hand-calculable runs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def raster_statistics(processing, fixture_dem):
    envelope = processing.run(
        "native:rasterlayerstatistics", {"INPUT": str(fixture_dem), "BAND": 1}
    )
    assert_ok(envelope, context_note="native:rasterlayerstatistics on the fixture DEM")
    return envelope.result["results"]


def test_raster_statistics_match_the_declared_dem(raster_statistics, fixture_truth):
    """Known algorithm + fixture input -> hand-calculable expected output.

    Every key is compared, not just the extremes. ``SUM`` is the one that
    matters most: it is derived in closed form from the declared pattern, so it
    checks all 2491 valid cells, where ``MIN`` and ``MAX`` check two.
    """
    expected = fixture_truth.RASTER_STATISTICS
    actual = {key: raster_statistics.get(key) for key in expected}
    for key, want in expected.items():
        got = actual[key]
        assert got == pytest.approx(want), (
            f"raster statistic {key} mismatch\n"
            f"    expected: {want}\n"
            f"    actual:   {got}\n"
            f"full results: {describe(raster_statistics)}"
        )


def test_raster_statistics_exclude_the_nodata_sentinel(
    raster_statistics, fixture_truth
):
    """Independent confirmation that Processing honours nodata.

    Reached by a different code path from the fixture suite's own check: if the
    sentinel were counted, ``COUNT`` would be 2500 rather than 2491 and ``MIN``
    would be -9999.0. This is the REQ-11 trap seen from the Processing side.
    """
    assert raster_statistics["COUNT"] == fixture_truth.VALID_CELL_COUNT, (
        f"COUNT must exclude nodata cells\n"
        f"    expected: {fixture_truth.VALID_CELL_COUNT}\n"
        f"    actual:   {raster_statistics['COUNT']}\n"
        f"    (the full grid is {fixture_truth.DEM_TOTAL_CELLS} cells)"
    )
    assert raster_statistics["MIN"] != fixture_truth.DEM_NODATA, (
        f"MIN is {raster_statistics['MIN']}, the nodata sentinel, so nodata "
        f"was counted as data"
    )


@pytest.fixture(scope="module")
def buffered_boundary(processing, runner, fixture_gpkg, fixture_truth, tmp_path_factory):
    """Buffer the AOI rectangle, and measure what came back."""
    destination = tmp_path_factory.mktemp("processing") / "buffered.gpkg"
    envelope = processing.run(
        "native:buffer",
        {
            "INPUT": f"{fixture_gpkg}|layername={fixture_truth.AOI_BOUNDARY_LAYER}",
            "DISTANCE": fixture_truth.BUFFER_DISTANCE_M,
            "JOIN_STYLE": fixture_truth.BUFFER_JOIN_STYLE_MITER,
            "END_CAP_STYLE": fixture_truth.BUFFER_END_CAP_FLAT,
            "OUTPUT": str(destination),
        },
    )
    assert_ok(envelope, context_note="native:buffer on the AOI boundary")

    measured = runner.run_code(
        "from osgeo import ogr\n"
        f"datasource = ogr.Open({str(destination)!r})\n"
        "layer = datasource.GetLayer(0)\n"
        "feature = layer.GetNextFeature()\n"
        "geometry = feature.GetGeometryRef()\n"
        "xmin, xmax, ymin, ymax = geometry.GetEnvelope()\n"
        "result = {\n"
        "    'count': layer.GetFeatureCount(),\n"
        "    'area': geometry.GetArea(),\n"
        "    'extent': [xmin, ymin, xmax, ymax],\n"
        "    'crs': layer.GetSpatialRef().GetAuthorityCode(None),\n"
        "}\n"
    )
    assert_ok(measured, context_note="measuring the buffer output")
    return envelope, measured.result


def test_buffer_area_is_exact(buffered_boundary, fixture_truth):
    """Miter joins on a rectangle give a rectangle, so the area is exact.

    420 x 320 = 134 400 m², with no arcs to approximate — which is the reason
    `truth.py` specifies miter rather than the default round join.
    """
    _, measured = buffered_boundary
    assert measured["count"] == 1, describe(measured)
    assert measured["area"] == pytest.approx(fixture_truth.AOI_BUFFERED_AREA_M2), (
        f"buffered area mismatch\n"
        f"    expected: {fixture_truth.AOI_BUFFERED_AREA_M2}\n"
        f"    actual:   {measured['area']}"
    )


def test_buffer_extent_is_exact(buffered_boundary, fixture_truth):
    _, measured = buffered_boundary
    assert measured["extent"] == pytest.approx(
        list(fixture_truth.AOI_BUFFERED_EXTENT)
    ), (
        f"buffered extent mismatch\n"
        f"    expected: {list(fixture_truth.AOI_BUFFERED_EXTENT)}\n"
        f"    actual:   {measured['extent']}"
    )


def test_buffer_preserves_the_crs(buffered_boundary, fixture_truth):
    """Nothing in this call should reproject; REQ-12 reserves that for
    acquisition, and a silent reprojection here would corrupt later measures."""
    _, measured = buffered_boundary
    expected_code = fixture_truth.PRIMARY_CRS.split(":")[1]
    assert measured["crs"] == expected_code, (
        f"output CRS mismatch\n"
        f"    expected EPSG:{expected_code}\n"
        f"    actual   EPSG:{measured['crs']}"
    )


def test_run_reports_outputs_that_reached_disk(buffered_boundary):
    """The ``outputs`` summary says whether each destination was written.

    An algorithm can report success and write nothing — with an empty input,
    for instance — and that stays invisible until something downstream fails to
    open the file.
    """
    envelope, _ = buffered_boundary
    outputs = envelope.result["outputs"]
    by_parameter = {entry["parameter"]: entry for entry in outputs}
    assert "OUTPUT" in by_parameter, describe(outputs)
    entry = by_parameter["OUTPUT"]
    assert entry.get("is_path") is True, describe(entry)
    assert entry.get("exists") is True, describe(entry)
    assert entry.get("size_bytes", 0) > 0, describe(entry)


def test_clip_matches_the_documented_expectation(
    processing, runner, fixture_gpkg, fixture_truth, tmp_path
):
    """2 of 5 survey points fall inside the AOI boundary.

    The same number `test_fixtures.py` derives from the geometry directly, now
    reached through a Processing algorithm — so the two agree or one of them is
    wrong.
    """
    destination = tmp_path / "clipped.gpkg"
    envelope = processing.run(
        "native:clip",
        {
            "INPUT": f"{fixture_gpkg}|layername={fixture_truth.SURVEY_POINTS_LAYER}",
            "OVERLAY": f"{fixture_gpkg}|layername={fixture_truth.AOI_BOUNDARY_LAYER}",
            "OUTPUT": str(destination),
        },
    )
    assert_ok(envelope, context_note="native:clip of survey points by the AOI")

    measured = runner.run_code(
        "from osgeo import ogr\n"
        f"datasource = ogr.Open({str(destination)!r})\n"
        "layer = datasource.GetLayer(0)\n"
        "result = {\n"
        "    'count': layer.GetFeatureCount(),\n"
        "    'names': sorted(f.GetField('name') for f in layer),\n"
        "}\n"
    )
    assert_ok(measured, context_note="measuring the clip output")
    assert measured.result["names"] == sorted(fixture_truth.SURVEY_POINTS_INSIDE_AOI), (
        f"clipped points mismatch\n"
        f"    expected: {sorted(fixture_truth.SURVEY_POINTS_INSIDE_AOI)}\n"
        f"    actual:   {measured.result['names']}"
    )
    assert measured.result["count"] == fixture_truth.SURVEY_POINTS_INSIDE_AOI_COUNT


# ---------------------------------------------------------------------------
# Failure reporting
# ---------------------------------------------------------------------------


def test_missing_mandatory_parameters_reports_the_algorithms_own_error(processing):
    """When Processing refuses, pass its reasoning through rather than ours.

    ``qgis_process`` names each missing mandatory parameter. Replacing that
    with a generic failure would discard the only part a caller can act on.
    """
    envelope = processing.run("native:buffer", {})
    assert_failed(envelope, ErrorCode.ALGORITHM_FAILED)
    stderr = envelope.error.get("stderr") or ""
    assert "INPUT" in stderr, (
        f"the algorithm's own parameter diagnostic must survive.\n"
        f"stderr: {stderr}"
    )
    assert envelope.error["algorithm_id"] == "native:buffer", describe(envelope.error)


# ---------------------------------------------------------------------------
# REQ-4: destructive destinations
# ---------------------------------------------------------------------------


def test_overwriting_an_existing_output_requires_confirmation(
    processing, fixture_gpkg, fixture_truth, tmp_path
):
    """REQ-4 applied to Processing destinations.

    There is no undo across a scripted session, so a destination that already
    holds data is gated. Creating a *new* file is not gated — that is the
    ordinary case, and gating it would make the tool unusable.
    """
    destination = tmp_path / "twice.gpkg"
    parameters = {
        "INPUT": f"{fixture_gpkg}|layername={fixture_truth.AOI_BOUNDARY_LAYER}",
        "OUTPUT": str(destination),
    }

    first = processing.run("native:centroids", dict(parameters))
    assert_ok(first, context_note="first write to a fresh path must not need confirm")
    assert destination.exists()

    second = processing.run("native:centroids", dict(parameters))
    assert_failed(second, ErrorCode.CONFIRMATION_REQUIRED)
    overwrites = second.error["would_overwrite"]
    assert overwrites[0]["parameter"] == "OUTPUT", describe(second.error)
    assert overwrites[0]["path"] == str(destination), describe(second.error)

    confirmed = processing.run("native:centroids", dict(parameters), confirm=True)
    assert_ok(confirmed, context_note="confirm=true must permit the overwrite")


def test_project_file_destination_requires_confirmation(
    processing, fixture_gpkg, fixture_truth, tmp_path
):
    """REQ-4 names overwriting a project file explicitly.

    Gated even when the file does not exist yet: the requirement is about
    writing a project, not about what is already there.
    """
    destination = tmp_path / "not-yet-written.qgz"
    assert not destination.exists()
    envelope = processing.run(
        "native:centroids",
        {
            "INPUT": f"{fixture_gpkg}|layername={fixture_truth.AOI_BOUNDARY_LAYER}",
            "OUTPUT": str(destination),
        },
    )
    assert_failed(envelope, ErrorCode.CONFIRMATION_REQUIRED)
    assert "project" in describe(envelope.error).lower(), describe(envelope.error)


def test_layer_uri_inputs_are_not_mistaken_for_destinations(
    processing, fixture_gpkg, fixture_truth, tmp_path
):
    """The input GeoPackage exists, and must not trip the overwrite guard.

    Every call names an existing input file. A guard that looked at paths
    without regard to which parameter they belong to would demand confirmation
    for every read.
    """
    envelope = processing.run(
        "native:centroids",
        {
            "INPUT": f"{fixture_gpkg}|layername={fixture_truth.AOI_BOUNDARY_LAYER}",
            "OUTPUT": str(tmp_path / "fresh.gpkg"),
        },
    )
    assert_ok(
        envelope,
        context_note="an existing INPUT must not require overwrite confirmation",
    )


# ---------------------------------------------------------------------------
# REQ-1b: mode reporting
# ---------------------------------------------------------------------------


def test_processing_tools_are_mode_agnostic():
    """design.md §3.3: the partition is declared, not inferred.

    REQ-1a's scope is a list, and these two tools are not on it — so they fall
    under REQ-1b and report which path served them instead of refusing.
    """
    from qgis_mcp_server import modes

    for tool_name in ("run_processing_algorithm", "search_processing_algorithms"):
        assert tool_name not in modes.MODE_NAMED_TOOLS, (
            f"{tool_name} is mode-agnostic under REQ-1b and must not appear in "
            f"the mode-named partition"
        )
    assert modes.MODE_NAMED_TOOLS["execute_pyqgis_code_for_cli"] == (
        ExecutionMode.HEADLESS
    )


def test_run_tool_reports_the_serving_mode(tools, fixture_dem):
    """REQ-1b: the response says which mode served the call, and why."""
    payload = tools.tools["run_processing_algorithm"](
        algorithm_id="native:rasterlayerstatistics",
        parameters={"INPUT": str(fixture_dem), "BAND": 1},
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["mode"] == ExecutionMode.HEADLESS, describe(payload)
    assert payload["mode_reason"], (
        f"the serving mode must come with a reason — 'headless' alone does not "
        f"answer why a layer is not on screen.\n{describe(payload)}"
    )
    assert payload["results"]["MIN"] == -5.0, describe(payload)


def test_failed_run_still_reports_the_mode(tools):
    """A failure must not drop the mode: it is how a caller reads the error."""
    payload = tools.tools["run_processing_algorithm"](algorithm_id="qgis:buffer")
    assert payload["ok"] is False, describe(payload)
    assert payload["mode"] == ExecutionMode.HEADLESS, describe(payload)
    assert payload["error"]["code"] == ErrorCode.ALGORITHM_NOT_FOUND, describe(payload)


# ---------------------------------------------------------------------------
# search_processing_algorithms
# ---------------------------------------------------------------------------


def test_search_finds_an_algorithm_by_free_text(tools):
    payload = tools.tools["search_processing_algorithms"](query="buffer")
    assert payload["ok"] is True, describe(payload)
    ids = [entry["id"] for entry in payload["algorithms"]]
    assert "native:buffer" in ids, f"expected native:buffer among {ids}"
    assert payload["mode"] == ExecutionMode.HEADLESS, describe(payload)


def test_search_reports_registered_providers(tools):
    """§6.4 again, at the tool boundary rather than the catalogue's."""
    payload = tools.tools["search_processing_algorithms"](query="buffer")
    providers = payload["providers"]
    assert "native" in providers["registered_providers"], describe(providers)
    assert "saga" in (providers.get("unavailable_providers") or {}), describe(providers)


def test_search_truncation_is_reported(tools):
    """A window must not read as the whole answer."""
    payload = tools.tools["search_processing_algorithms"](query="layer", limit=2)
    assert payload["returned"] == 2, describe(payload)
    assert payload["matched"] > 2, describe(payload)
    assert payload["truncated"] is True, describe(payload)
    assert str(payload["matched"]) in payload["note"], describe(payload)


def test_search_by_id_returns_the_full_parameter_schema(tools):
    """The narrative spec's "and their parameter schemas".

    Enum option *numbers* are the reason this matters: nothing about
    ``JOIN_STYLE`` tells a caller that miter is 1, and a guess produces
    geometry that is wrong rather than an error.
    """
    payload = tools.tools["search_processing_algorithms"](algorithm_id="native:buffer")
    assert payload["ok"] is True, describe(payload)
    parameters = payload["parameters"]
    for required in ("INPUT", "DISTANCE", "JOIN_STYLE", "END_CAP_STYLE", "OUTPUT"):
        assert required in parameters, (
            f"expected parameter {required!r} in the schema; got "
            f"{sorted(parameters)}"
        )
    options = parameters["JOIN_STYLE"]["available_options"]
    assert options["1"] == "Miter", (
        f"JOIN_STYLE option 1 must be Miter, which is what truth.py's exact "
        f"buffer geometry depends on.\noptions: {describe(options)}"
    )
    assert "OUTPUT" in payload["outputs"], describe(payload["outputs"])


def test_search_by_unknown_id_returns_suggestions(tools):
    """REQ-7's contract applies to discovery as well as to running."""
    payload = tools.tools["search_processing_algorithms"](algorithm_id="qgis:buffer")
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.ALGORITHM_NOT_FOUND, describe(payload)
    ids = [entry["id"] for entry in payload["error"]["suggestions"]]
    assert "native:buffer" in ids, f"expected native:buffer among {ids}"


def test_search_restricted_to_an_unregistered_provider_explains_itself(tools):
    """Zero results would imply the algorithms exist but did not match."""
    payload = tools.tools["search_processing_algorithms"](query="slope", provider="saga")
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST, describe(payload)
    assert "saga" in (payload["error"].get("unavailable_providers") or {}), describe(
        payload["error"]
    )


def test_search_restricted_to_a_registered_provider_filters(tools):
    payload = tools.tools["search_processing_algorithms"](
        query="buffer", provider="gdal", limit=25
    )
    assert payload["ok"] is True, describe(payload)
    providers = {entry["provider"] for entry in payload["algorithms"]}
    assert providers == {"gdal"}, (
        f"expected only gdal algorithms; got providers {providers}"
    )


def test_single_hit_search_includes_its_schema(tools):
    """One hit identifies an algorithm, so return the schema now.

    Saves a round-trip the caller would certainly make next.
    """
    payload = tools.tools["search_processing_algorithms"](
        query="rasterlayerstatistics"
    )
    assert payload["returned"] == 1, describe(payload)
    assert "parameters" in payload, (
        f"a single-hit search should carry the parameter schema.\n"
        f"{describe({k: v for k, v in payload.items() if k != 'providers'})}"
    )
    assert "BAND" in payload["parameters"], describe(payload["parameters"])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_tools_register_on_a_real_server_with_their_descriptions(config):
    """Checked against a real ``MCPServer``, not the collecting shim.

    The shim is fine for behaviour, but registration — names, descriptions,
    schemas derived from type hints — is exactly the part a shim could let
    drift, so it is verified against the SDK.
    """
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()

    by_name = {tool.name: tool for tool in registered}
    for expected in ("run_processing_algorithm", "search_processing_algorithms"):
        assert expected in by_name, (
            f"expected {expected} to be registered; found {sorted(by_name)}"
        )

    run_description = by_name["run_processing_algorithm"].description or ""
    # REQ-1b's mode consequence and REQ-4's gate are both things a caller
    # cannot infer from the signature, so both must be stated.
    for required in ("confirm=true", "live session", "Headless"):
        assert required in run_description, (
            f"run_processing_algorithm's description must mention {required!r}.\n"
            f"description:\n{run_description}"
        )

    search_description = by_name["search_processing_algorithms"].description or ""
    for required in ("saga", "grass", "registered"):
        assert required.lower() in search_description.lower(), (
            f"search_processing_algorithms' description must mention "
            f"{required!r} (the task-3 provider-absence note).\n"
            f"description:\n{search_description}"
        )


def test_run_schema_exposes_its_parameters(config):
    """§2.4: schemas are derived from type hints, not hand-authored."""
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()

    # SDK 2.x renamed this from the wire format's `inputSchema`.
    schema = next(
        tool.input_schema
        for tool in registered
        if tool.name == "run_processing_algorithm"
    )
    properties = schema.get("properties", {})
    for required in ("algorithm_id", "parameters", "confirm", "timeout_seconds"):
        assert required in properties, (
            f"expected {required!r} in the derived schema; got {sorted(properties)}"
        )
    assert schema.get("required") == ["algorithm_id"], (
        f"algorithm_id should be the only required parameter; got "
        f"{schema.get('required')}"
    )
