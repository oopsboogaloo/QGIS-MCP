"""Task 4.7: `download_and_register_layer`.

`tasks.md` asks for one thing specifically — "assert an unconfirmed call is
refused" — and leaves live fetching to a single opt-in integration test. Both
are here, but the gate is only part of what this tool carries: REQ-4d's
disclosure, REQ-12's reprojection with the original retained, REQ-13's vertical
datum, and REQ-1b's serving mode all meet in one call.

**Every test here runs with zero API calls.** `urlopen` is patched to fail, and
the downloaded original is seeded from the committed fixture — which is exactly
the path `execute` takes when a previous download is still cached, so seeding
exercises real behaviour rather than bypassing it. That keeps reproject-and-
register under test without spending any of an allowance of 50 per 24 hours.
"""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

import pytest

from conftest import CollectingServer, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode
from qgis_mcp_server.acquisition import RESAMPLING_BILINEAR, RESAMPLING_NEAREST
from qgis_mcp_server.config import Config
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.geocoding import (
    NominatimGeocoder,
    PlaceResolver,
    ResolutionCache,
)
from qgis_mcp_server.providers.catalogue import OPENTOPOGRAPHY_CREDENTIAL

#: The AOI the committed COP30 fixture was recorded for.
FIXTURE_AOI = (-1.270, 51.170, -1.255, 51.180)
#: Spans several UTM zones, so REQ-12 forbids choosing a CRS automatically.
CONTINENTAL_AOI = (-10.0, 36.0, 30.0, 60.0)

RESOLVES = "Andover, Hampshire, England"
AMBIGUOUS = "Newport"


@pytest.fixture
def no_network(monkeypatch):
    """Any network call fails the test rather than silently working."""

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "this test must not reach the network; seed the original from the "
            "committed fixture instead"
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def acquisition_context(monkeypatch, tmp_path, request, no_network):
    """A context with a temp cache, a key configured and an offline geocoder."""
    monkeypatch.setenv(OPENTOPOGRAPHY_CREDENTIAL.env_var, "test-key-0123456789")

    cache_path = request.config.rootpath / "test-data" / "geocoder-cache"
    if not cache_path.is_dir():
        pytest.skip("geocoder fixtures missing")

    config = Config.load()
    # A temp cache, so a test neither reads nor pollutes the real download
    # directory -- and so "was this reused" is decided by the test, not by
    # whatever happens to be on this machine.
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
        yield context


@pytest.fixture
def tools(acquisition_context):
    from qgis_mcp_server.tools import acquisition

    server = CollectingServer()
    acquisition.register(server, acquisition_context)
    return server


@pytest.fixture
def recorded_original(request):
    path = (
        request.config.rootpath
        / "test-data"
        / "provider-fixtures"
        / "opentopography"
        / "cop30_su4080.tif"
    )
    if not path.exists():
        pytest.skip(
            "provider fixtures missing. Run: "
            "python test-data/record_provider_fixtures.py"
        )
    return path


def seed(context, dataset_id: str, aoi, target_crs: str, source: Path) -> Path:
    """Place a recorded response where a previous download would have left it.

    Not a shortcut past the fetch: ``execute`` already skips the request when a
    non-empty original is present, because REQ-12 requires it retained and
    re-fetching a file we were told to keep would waste the allowance twice.
    So this drives the same branch a second download takes.
    """
    found, failure = context.acquisition.find_dataset(
        "opentopography", dataset_id, aoi
    )
    assert failure is None, describe(failure.error if failure else None)
    original, _ = context.acquisition.paths_for(found, aoi, target_crs)
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, original)
    return original


def download(tools, **kwargs):
    return tools.tools["download_and_register_layer"](**kwargs)


# ---------------------------------------------------------------------------
# REQ-4d: the confirmation gate
# ---------------------------------------------------------------------------


def test_unconfirmed_call_is_refused(tools):
    """The assertion tasks.md names for this task."""
    payload = download(
        tools, provider="opentopography", dataset="COP30", bbox=list(FIXTURE_AOI)
    )
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.CONFIRMATION_REQUIRED
    assert payload["error"]["confirmation_required"] is True


def test_unconfirmed_call_downloads_nothing(tools, acquisition_context):
    """Proven two ways: no files, and the network would have failed the test.

    The ``no_network`` fixture makes any request an assertion error, so this
    passing is itself evidence the gate sits before the fetch rather than after.
    """
    download(
        tools, provider="opentopography", dataset="COP30", bbox=list(FIXTURE_AOI)
    )
    download_dir = acquisition_context.acquisition.download_dir
    assert not download_dir.exists() or not list(download_dir.rglob("*.tif")), (
        "an unconfirmed call wrote files"
    )


def test_refusal_states_everything_req4d_requires(tools):
    """Source, format, licence tier and approximate size — all four.

    REQ-4d requires the confirmation to state them *before* proceeding. Making
    the caller assemble that itself would mean guessing the size and the
    licence, so the refusal carries them instead.
    """
    payload = download(
        tools, provider="opentopography", dataset="COP30", bbox=list(FIXTURE_AOI)
    )
    plan = payload["error"]["plan"]

    assert plan["source"]["provider"] == "opentopography", describe(plan)
    assert plan["source"]["dataset"] == "COP30", describe(plan)
    assert plan["format"] == "GTiff", describe(plan)
    assert plan["licence"]["tier"], describe(plan)
    assert plan["estimated_size_bytes"] > 0, describe(plan)
    assert plan["estimated_size_human"], describe(plan)


def test_refusal_names_what_would_change_on_disk(tools, tmp_path):
    """REQ-4 territory, stated in the same breath as REQ-4d's fetch.

    Confirming a download also consents to the project being modified, so the
    project path has to be in front of the caller when they confirm.
    """
    project = tmp_path / "target.qgz"
    payload = download(
        tools,
        provider="opentopography",
        dataset="COP30",
        bbox=list(FIXTURE_AOI),
        project_path=str(project),
    )
    plan = payload["error"]["plan"]
    assert plan["will_modify_project"] is True, describe(plan)
    assert plan["project_path"] == str(project), describe(plan)
    assert str(project) in payload["error"]["message"], payload["error"]["message"]
    assert plan["original_path"].endswith(".tif"), describe(plan)


def test_refusal_message_is_readable_to_a_person(tools):
    """It is what an agent shows a user before asking them to confirm."""
    payload = download(
        tools, provider="opentopography", dataset="COP30", bbox=list(FIXTURE_AOI)
    )
    message = payload["error"]["message"]
    assert "Copernicus GLO-30" in message, message
    assert "opentopography" in message, message
    assert "confirm=true" in message, message


def test_confirmed_call_proceeds(tools, acquisition_context, recorded_original):
    seed(acquisition_context, "COP30", FIXTURE_AOI, "EPSG:27700", recorded_original)
    payload = download(
        tools,
        provider="opentopography",
        dataset="COP30",
        bbox=list(FIXTURE_AOI),
        confirm=True,
        register_layer=False,
    )
    assert payload["ok"] is True, describe(payload)
    assert Path(payload["layer_path"]).exists()


# ---------------------------------------------------------------------------
# REQ-12: reprojection, original retained, CRS and tier reported
# ---------------------------------------------------------------------------


@pytest.fixture
def acquired(tools, acquisition_context, recorded_original, tmp_path):
    """One completed acquisition, registered into a fresh project."""
    seed(acquisition_context, "COP30", FIXTURE_AOI, "EPSG:27700", recorded_original)
    project = tmp_path / "acquired.qgz"
    payload = download(
        tools,
        provider="opentopography",
        dataset="COP30",
        bbox=list(FIXTURE_AOI),
        project_path=str(project),
        confirm=True,
    )
    assert payload["ok"] is True, describe(payload)
    return payload


def test_layer_is_reprojected_to_the_tier_1_crs(acquired):
    """A UK AOI lands in the national grid, and the response says which tier."""
    assert acquired["reprojected"] is True, describe(acquired)
    assert acquired["target_crs"] == "EPSG:27700", describe(acquired)
    assert acquired["crs_tier"] == 1, describe(acquired)
    assert acquired["crs_reason"], describe(acquired)
    assert acquired["registration"]["crs"] == "EPSG:27700", describe(
        acquired["registration"]
    )


def test_the_unmodified_original_is_retained(acquired, recorded_original):
    """REQ-12 requires it kept, so the resampling stays re-derivable.

    Byte-identical: not merely present, but untouched. A reprojection that
    wrote over its own input would still leave a file at this path.
    """
    original = Path(acquired["original_path"])
    assert original.exists(), describe(acquired)
    assert original.read_bytes() == recorded_original.read_bytes(), (
        "the retained original is not byte-identical to what was downloaded"
    )
    assert original != Path(acquired["layer_path"]), (
        "the reprojected layer must be a separate file from the original"
    )


def test_reprojection_uses_bilinear_not_the_algorithms_default(acquired):
    """`gdal:warpreproject` defaults to Nearest Neighbour, which is wrong here.

    Nearest preserves exact pixel values and is right for categorical rasters.
    On a continuous elevation surface it produces blocky terraces that look
    like terrain and are not — the same silent-plausible-output failure REQ-11
    guards against for bit depth. So it is set explicitly.
    """
    assert RESAMPLING_BILINEAR != RESAMPLING_NEAREST
    assert RESAMPLING_NEAREST == 0, (
        "if the algorithm's default stops being nearest neighbour, this "
        "reasoning needs revisiting"
    )
    step = next(s for s in acquired["steps"] if s["step"] == "reproject")
    assert step["resampling"] == "bilinear", describe(step)
    assert step["original_retained"] == acquired["original_path"], describe(step)


def test_multi_zone_aoi_is_refused_without_a_target_crs(tools):
    """REQ-12: the server SHALL NOT choose, and that bites hardest here.

    Discovering the question after three layers have arrived in a projection
    nobody chose costs re-downloads; asking first costs one round-trip.
    """
    payload = download(
        tools,
        provider="opentopography",
        dataset="SRTM15Plus",
        bbox=list(CONTINENTAL_AOI),
        confirm=True,
    )
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST
    assert payload["error"]["requires_choice"] is True, describe(payload["error"])
    assert len(payload["error"]["candidates"]) >= 2, describe(payload["error"])
    assert "cartographic" in payload["error"]["message"], payload["error"]["message"]


def test_multi_zone_aoi_proceeds_with_an_explicit_target(tools):
    """Named a target, the refusal lifts — so it is a question, not a wall."""
    payload = download(
        tools,
        provider="opentopography",
        dataset="SRTM15Plus",
        bbox=list(CONTINENTAL_AOI),
        target_crs="EPSG:3035",
    )
    assert payload["ok"] is False, describe(payload)
    # Refused for confirmation now, not for the CRS.
    assert payload["error"]["code"] == ErrorCode.CONFIRMATION_REQUIRED
    assert payload["error"]["plan"]["target_crs"] == "EPSG:3035", describe(
        payload["error"]["plan"]
    )


# ---------------------------------------------------------------------------
# REQ-13: the vertical datum
# ---------------------------------------------------------------------------


def test_vertical_datum_is_recorded_on_the_layer(acquired):
    """A GeoTIFF has nowhere to put it, so the project is the only place it
    survives — and REQ-13 needs it there for a later mismatch warning."""
    assert acquired["vertical_datum"] == "EGM2008", describe(acquired)
    recorded = acquired["registration"]["provenance_recorded"]
    assert recorded["vertical_datum"] == "EGM2008", describe(recorded)


def test_provenance_survives_in_the_project(acquired, runner):
    """Read back through PyQGIS, not from the response that wrote it.

    Asserting the return value would only prove the server said it recorded
    something. This opens the written project and reads the custom properties,
    which is what a later tool or a person would actually find.
    """
    project_path = acquired["registration"]["project_path"]
    prefix = acquired["registration"]["provenance_property_prefix"]
    envelope = runner.run_code(
        "from qgis.core import QgsProject\n"
        "project = QgsProject.instance()\n"
        "layer = list(project.mapLayers().values())[0]\n"
        f"keys = [k for k in layer.customPropertyKeys() if k.startswith({prefix!r})]\n"
        "result = {\n"
        "    'name': layer.name(),\n"
        "    'crs': layer.crs().authid(),\n"
        f"    'properties': {{k.replace({prefix!r}, ''): layer.customProperty(k) for k in keys}},\n"
        "    'abstract': layer.metadata().abstract(),\n"
        "    'rights': list(layer.metadata().rights()),\n"
        "}\n",
        project_path=project_path,
    )
    assert envelope.ok, describe(envelope.error)
    result = envelope.result

    assert result["crs"] == "EPSG:27700", describe(result)
    properties = result["properties"]
    assert properties["vertical_datum"] == "EGM2008", describe(properties)
    assert properties["provider"] == "opentopography", describe(properties)
    assert properties["dataset_identifier"] == "COP30", describe(properties)
    assert properties["target_crs"] == "EPSG:27700", describe(properties)
    assert properties["original_path"].endswith("_original.tif"), describe(properties)

    # Also in the layer's own metadata, which is where a person looking at the
    # project in QGIS six months from now would find it.
    assert "EGM2008" in result["abstract"], result["abstract"]
    assert result["rights"], result


def test_the_licence_travels_onto_the_layer(acquired, runner):
    """"Where did this come from" has to be answerable from the project alone.

    Attribution and a datum were already recorded. What a person actually needs
    before putting a layer into paid work is the licence itself, somewhere to
    read its terms, and a warning when the answer is "check first" — all three
    were known at acquisition and none of them reached the layer.

    Checked in the two places QGIS looks: the metadata licence field, and the
    layer attribution property, which is what a print layout draws. A credit
    that only exists in a dialogue box does not end up on the exported map.
    """
    project_path = acquired["registration"]["project_path"]
    envelope = runner.run_code(
        "from qgis.core import QgsProject\n"
        "project = QgsProject.instance()\n"
        "layer = list(project.mapLayers().values())[0]\n"
        "result = {\n"
        "    'licences': list(layer.metadata().licenses()),\n"
        "    'abstract': layer.metadata().abstract(),\n"
        "    'attribution': layer.attribution(),\n"
        "    'attribution_url': layer.attributionUrl(),\n"
        "}\n",
        project_path=project_path,
    )
    assert envelope.ok, describe(envelope.error)
    result = envelope.result

    assert result["licences"], "no licence recorded on the layer"
    assert "mixed" in " ".join(result["licences"]).lower(), result["licences"]

    # OpenTopography's free key does not cover commercial integration, so the
    # layer must say so where it will be read rather than only in the response
    # that created it.
    assert "COMMERCIAL USE NEEDS CHECKING" in result["abstract"], result["abstract"]
    assert "Enterprise key" in result["abstract"], result["abstract"]
    assert "opentopography.org" in result["attribution_url"], result

    # The layout credit.
    assert result["attribution"], "nothing for a print layout to draw"


def test_ellipsoidal_dataset_warns_before_download(tools):
    """The 50 m trap, surfaced at the point of choice.

    SRTMGL1_E differs from SRTMGL1 by one suffix and by roughly 50 m of height
    over the UK. The warning has to arrive before the download, not after.
    """
    payload = download(
        tools,
        provider="opentopography",
        dataset="SRTMGL1_E",
        bbox=list(FIXTURE_AOI),
    )
    warnings = " ".join(payload["error"]["plan"]["warnings"]).lower()
    assert "ellipsoid" in warnings, warnings
    assert "50 m" in warnings, warnings
    assert payload["error"]["plan"]["vertical_datum"] == "WGS84 ellipsoid"


def test_orthometric_dataset_does_not_warn_about_datums(tools):
    """So the warning means something when it appears."""
    payload = download(
        tools, provider="opentopography", dataset="COP30", bbox=list(FIXTURE_AOI)
    )
    warnings = " ".join(payload["error"]["plan"]["warnings"]).lower()
    assert "ellipsoid" not in warnings, warnings


def test_licence_obligations_are_surfaced_before_download(tools):
    """What a published output must carry, and what the tier does not cover."""
    payload = download(
        tools, provider="opentopography", dataset="COP30", bbox=list(FIXTURE_AOI)
    )
    warnings = " ".join(payload["error"]["plan"]["warnings"])
    assert "must carry" in warnings, warnings
    assert "OpenTopography" in warnings, warnings
    assert "commercial" in warnings.lower(), warnings


# ---------------------------------------------------------------------------
# Registration outcomes
# ---------------------------------------------------------------------------


def test_registering_creates_a_project_when_none_exists(acquired, tmp_path):
    assert acquired["registered"] is True, describe(acquired)
    assert acquired["registration"]["project_created"] is True
    assert Path(acquired["registration"]["project_path"]).exists()
    assert acquired["registration"]["project_layer_count"] == 1


def test_register_layer_false_touches_no_project(
    tools, acquisition_context, recorded_original, tmp_path
):
    seed(acquisition_context, "COP30", FIXTURE_AOI, "EPSG:27700", recorded_original)
    project = tmp_path / "untouched.qgz"
    payload = download(
        tools,
        provider="opentopography",
        dataset="COP30",
        bbox=list(FIXTURE_AOI),
        project_path=str(project),
        register_layer=False,
        confirm=True,
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["registered"] is False
    assert not project.exists(), "no project should have been written"
    # The data is still there and usable.
    assert Path(payload["layer_path"]).exists()


def test_no_project_configured_still_produces_the_files(
    tools, acquisition_context, recorded_original
):
    """A completed download must not be discarded for want of a project.

    Throwing away a fetch that already spent one of 50 daily calls, because
    there was nowhere to register it, would be the wrong trade — so this is a
    partial success that says what is missing.
    """
    seed(acquisition_context, "COP30", FIXTURE_AOI, "EPSG:27700", recorded_original)
    payload = download(
        tools,
        provider="opentopography",
        dataset="COP30",
        bbox=list(FIXTURE_AOI),
        confirm=True,
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["registered"] is False
    assert "no project is configured" in payload["registration_note"]
    assert Path(payload["layer_path"]).exists()


def test_a_cached_original_is_reused_rather_than_refetched(
    tools, acquisition_context, recorded_original, tmp_path
):
    """REQ-12 keeps the original, so a second call must not spend a call.

    Reported rather than silent: it explains both a suspiciously fast response
    and a daily allowance that did not move.
    """
    seed(acquisition_context, "COP30", FIXTURE_AOI, "EPSG:27700", recorded_original)
    payload = download(
        tools,
        provider="opentopography",
        dataset="COP30",
        bbox=list(FIXTURE_AOI),
        confirm=True,
        register_layer=False,
    )
    step = next(s for s in payload["steps"] if s["step"] == "download")
    assert step["reused_existing"] is True, describe(step)


# ---------------------------------------------------------------------------
# Dataset and AOI resolution
# ---------------------------------------------------------------------------


def test_unknown_provider_lists_the_implemented_ones(tools):
    payload = download(
        tools, provider="nosuchprovider", dataset="X", bbox=list(FIXTURE_AOI)
    )
    assert payload["ok"] is False, describe(payload)
    assert "opentopography" in payload["error"]["implemented_providers"]


def test_unknown_dataset_lists_what_is_available(tools):
    payload = download(
        tools, provider="opentopography", dataset="NOTADEM", bbox=list(FIXTURE_AOI)
    )
    assert payload["ok"] is False, describe(payload)
    assert "COP30" in payload["error"]["available"], describe(payload["error"])


def test_dataset_excluded_for_this_aoi_is_distinguished_from_missing(tools):
    """Different mistakes need different answers.

    COP30 exists but cannot serve a continental AOI. Telling the caller it does
    not exist would send them looking for another identifier, when what they
    need is a smaller AOI.
    """
    payload = download(
        tools,
        provider="opentopography",
        dataset="COP30",
        bbox=list(CONTINENTAL_AOI),
    )
    assert payload["ok"] is False, describe(payload)
    assert "exists but cannot serve this AOI" in payload["error"]["message"]
    assert "450,000 km2" in payload["error"]["reason"], describe(payload["error"])
    assert payload["error"]["remedy"], describe(payload["error"])


def test_place_name_resolves_into_the_aoi(tools):
    payload = download(
        tools, provider="opentopography", dataset="COP30", place=RESOLVES
    )
    assert payload["error"]["code"] == ErrorCode.CONFIRMATION_REQUIRED, describe(
        payload
    )
    aoi = payload["error"]["plan"]["aoi"]
    assert -2.0 < aoi[0] < -1.0 and 51.0 < aoi[1] < 51.5, aoi
    assert payload["error"]["plan"]["target_crs"] == "EPSG:27700"


def test_ambiguous_place_stops_before_the_gate(tools):
    """Nothing is planned, let alone fetched, until the place is settled."""
    payload = download(
        tools, provider="opentopography", dataset="COP30", place=AMBIGUOUS
    )
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.AMBIGUOUS_PLACE
    assert "plan" not in payload["error"], (
        "no plan should be produced for an unresolved place"
    )


def test_needs_exactly_one_of_place_or_bbox(tools):
    for kwargs in ({}, {"place": RESOLVES, "bbox": list(FIXTURE_AOI)}):
        payload = download(
            tools, provider="opentopography", dataset="COP30", **kwargs
        )
        assert payload["ok"] is False, describe(payload)
        assert payload["error"]["code"] == ErrorCode.BAD_REQUEST


# ---------------------------------------------------------------------------
# REQ-1b
# ---------------------------------------------------------------------------


def test_mode_is_reported_on_success_and_on_refusal(tools, acquired):
    """REQ-1b calls this out by name: it is the only way to know whether the
    layer landed in an open canvas or in a project file on disk."""
    assert acquired["mode"] == ExecutionMode.HEADLESS, describe(acquired)
    assert acquired["mode_reason"], describe(acquired)

    refusal = download(
        tools, provider="opentopography", dataset="COP30", bbox=list(FIXTURE_AOI)
    )
    assert refusal["mode"] == ExecutionMode.HEADLESS, describe(refusal)


def test_download_tool_is_mode_agnostic():
    from qgis_mcp_server import modes

    assert "download_and_register_layer" not in modes.MODE_NAMED_TOOLS


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_tool_description_tells_the_agent_to_call_twice(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()

    by_name = {tool.name: tool for tool in registered}
    assert "download_and_register_layer" in by_name, sorted(by_name)
    description = by_name["download_and_register_layer"].description or ""
    assert "CALL IT TWICE" in description, description
    assert "confirm=true" in description, description
    assert "KEPT" in description, (
        "the description should say the original download is retained"
    )

    schema = by_name["download_and_register_layer"].input_schema
    assert set(schema.get("required", [])) == {"provider", "dataset"}, schema.get(
        "required"
    )
    assert "confirm" in schema["properties"], sorted(schema["properties"])


# ---------------------------------------------------------------------------
# The live call, excluded from the default pass
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_download_and_register(tmp_path, monkeypatch):
    """Opt-in. Spends one OpenTopography call.

    Run with: pytest -m integration

    `tasks.md` 4.7: "Live fetching covered by a single opt-in integration test
    against a known small dataset, run occasionally rather than in the default
    pass." The AOI is the same 1 km2 the fixtures use.
    """
    config = Config.load()
    if not config.credential_store.get(OPENTOPOGRAPHY_CREDENTIAL):
        pytest.skip("no OpenTopography API key configured")
    config.cache_dir = tmp_path / "cache"
    config.project_path = None

    with ServerContext(config) as context:
        found, failure = context.acquisition.find_dataset(
            "opentopography", "COP30", FIXTURE_AOI
        )
        assert failure is None
        project = tmp_path / "live.qgz"
        plan, failure = context.acquisition.plan(
            found, FIXTURE_AOI, project_path=project
        )
        assert failure is None
        envelope = context.acquisition.execute(plan)

    assert envelope.ok, describe(envelope.error)
    result = envelope.result
    assert Path(result["original_path"]).exists()
    assert Path(result["layer_path"]).exists()
    assert result["registered"] is True
    assert result["registration"]["crs"] == "EPSG:27700"
    assert result["registration"]["provenance_recorded"]["vertical_datum"] == "EGM2008"
