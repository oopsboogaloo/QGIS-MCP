"""Tasks 4.5 and 4.6: `search_data_sources` and `get_dataset_licence_info`.

This is the first tool that puts the whole acquisition front end together —
REQ-9 place resolution, REQ-10 per-provider status, REQ-8 grouping, and the
REQ-12 CRS preview — so the tests here are as much about the seams between
those as about any one of them.

Offline throughout: the geocoder replays committed fixtures and the
OpenTopography adapter's `search` makes no network call by design.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from conftest import CollectingServer, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode
from qgis_mcp_server.geocoding import (
    NominatimGeocoder,
    PlaceResolver,
    ResolutionCache,
)
from qgis_mcp_server.providers.base import DataType, LicenceTier

RESOLVES = "Andover, Hampshire, England"
AMBIGUOUS = "Newport"
NOT_FOUND = "Qqzzxx Nonexistent Placename"

UK_BBOX = [-1.27, 51.17, -1.255, 51.18]
CONTINENTAL_BBOX = [-10.0, 36.0, 30.0, 60.0]
ARCTIC_BBOX = [10.0, 70.0, 11.0, 71.0]
ONTARIO_BBOX = [-80.0, 43.0, -79.0, 44.0]


@pytest.fixture(scope="module")
def tools(context, request, monkeymodule):
    """Acquisition tools with an offline geocoder and a key configured.

    The key is a placeholder: nothing here fetches, and `search` is local by
    design — but without one the dispatcher would correctly skip the adapter
    for missing credentials and every menu would come back empty.
    """
    from qgis_mcp_server.providers.catalogue import OPENTOPOGRAPHY_CREDENTIAL
    from qgis_mcp_server.tools import acquisition

    cache_path = request.config.rootpath / "test-data" / "geocoder-cache"
    if not cache_path.is_dir():
        pytest.skip("geocoder fixtures missing")

    monkeymodule.setenv(OPENTOPOGRAPHY_CREDENTIAL.env_var, "test-key-0123456789")

    def refuse(*_args, **_kwargs):
        raise AssertionError("search_data_sources must not reach the network")

    monkeymodule.setattr(urllib.request, "urlopen", refuse)

    context.places = PlaceResolver(
        geocoders=(
            NominatimGeocoder(
                cache=ResolutionCache(cache_path, read_only=True), offline=True
            ),
        )
    )
    # Rebuilt so the adapter picks up the key set above.
    from qgis_mcp_server.providers.dispatcher import ProviderDispatcher
    from qgis_mcp_server.providers.opentopography import OpenTopographyAdapter

    context.providers = ProviderDispatcher(
        adapters=(OpenTopographyAdapter(context.credentials),)
    )

    server = CollectingServer()
    acquisition.register(server, context)
    return server


@pytest.fixture(scope="module")
def monkeymodule():
    """A module-scoped monkeypatch, since the tools fixture is module-scoped."""
    from _pytest.monkeypatch import MonkeyPatch

    patch = MonkeyPatch()
    yield patch
    patch.undo()


def search(tools, **kwargs):
    return tools.tools["search_data_sources"](**kwargs)


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def test_needs_exactly_one_of_place_or_bbox(tools):
    for kwargs in ({}, {"place": RESOLVES, "bbox": UK_BBOX}):
        payload = search(tools, **kwargs)
        assert payload["ok"] is False, describe(payload)
        assert payload["error"]["code"] == ErrorCode.BAD_REQUEST


@pytest.mark.parametrize(
    "bad_bbox",
    [
        [1.0, 2.0, 3.0],
        [10.0, 50.0, 5.0, 51.0],      # reversed longitude
        [-1.0, 52.0, 1.0, 51.0],      # reversed latitude
        [-200.0, 50.0, 1.0, 51.0],    # out of range
    ],
)
def test_malformed_bbox_is_refused(tools, bad_bbox):
    payload = search(tools, bbox=bad_bbox)
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST


def test_unknown_data_type_returns_the_supported_list(tools):
    """Candidates rather than a bare refusal, per requirements.md §0."""
    payload = search(tools, bbox=UK_BBOX, data_types=["elevatoin"])
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST
    assert DataType.ELEVATION in payload["error"]["supported_data_types"]


# ---------------------------------------------------------------------------
# REQ-9: the place path
# ---------------------------------------------------------------------------


def test_place_name_is_resolved_into_an_aoi(tools):
    payload = search(tools, place=RESOLVES)
    assert payload["ok"] is True, describe(payload)
    assert payload["aoi_source"] == "place"
    assert payload["resolved_place"]["name"] == "Andover"
    min_lon, min_lat, max_lon, max_lat = payload["aoi"]
    assert min_lon < max_lon and min_lat < max_lat


def test_ambiguous_place_stops_the_search(tools):
    """Nothing is searched until the place is settled.

    Searching the highest-scoring candidate would produce a complete, correct
    menu for the wrong Newport — the most expensive kind of wrong, because it
    looks like an answer.
    """
    payload = search(tools, place=AMBIGUOUS)
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.AMBIGUOUS_PLACE
    assert len(payload["error"]["candidates"]) > 1
    assert "by_data_type" not in payload, (
        "no search should have run against an unresolved place"
    )


def test_unmatched_place_is_distinguished_from_ambiguous(tools):
    payload = search(tools, place=NOT_FOUND)
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.PLACE_NOT_FOUND


def test_resolved_place_and_bbox_paths_agree(tools):
    """The two entry points must produce the same shape of answer."""
    from_place = search(tools, place=RESOLVES)
    from_bbox = search(tools, bbox=from_place["aoi"])
    assert from_bbox["ok"] is True, describe(from_bbox)
    assert set(from_place["by_data_type"]) == set(from_bbox["by_data_type"])
    assert from_bbox["aoi_source"] == "bbox"
    assert "resolved_place" not in from_bbox


# ---------------------------------------------------------------------------
# REQ-8: one grouped menu
# ---------------------------------------------------------------------------


def test_results_are_grouped_by_data_type(tools):
    payload = search(tools, bbox=UK_BBOX)
    assert payload["ok"] is True, describe(payload)
    assert DataType.ELEVATION in payload["by_data_type"], sorted(
        payload["by_data_type"]
    )
    datasets = payload["by_data_type"][DataType.ELEVATION]
    assert datasets, describe(payload)
    assert {d["provider"] for d in datasets} == {"opentopography"}


def test_requesting_several_types_returns_one_response(tools):
    """REQ-8: grouped across providers in a single response.

    Only elevation has an implemented adapter, so the useful assertion is that
    asking for two types yields one call and one grouped body — not that both
    groups are populated.
    """
    payload = search(
        tools, bbox=UK_BBOX, data_types=[DataType.ELEVATION, DataType.GEOLOGY]
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["requested_data_types"] == [
        DataType.ELEVATION,
        DataType.GEOLOGY,
    ]
    assert isinstance(payload["by_data_type"], dict)


def test_datasets_carry_what_req4d_needs_to_confirm(tools):
    """REQ-4d requires source, format, licence tier and approximate size.

    All four have to be present on the dataset itself, because the confirmation
    happens at download time and must not need a second lookup.
    """
    payload = search(tools, bbox=UK_BBOX)
    dataset = payload["by_data_type"][DataType.ELEVATION][0]
    assert dataset["provider"], describe(dataset)
    assert dataset["format"], describe(dataset)
    assert dataset["licence"]["tier"], describe(dataset)
    assert dataset["estimated_size_bytes"] > 0, describe(dataset)
    # REQ-13's datum, recorded at the point of acquisition.
    assert dataset["extra"]["vertical_datum"], describe(dataset)


# ---------------------------------------------------------------------------
# REQ-10: per-provider status
# ---------------------------------------------------------------------------


def test_provider_status_is_always_reported(tools):
    payload = search(tools, bbox=UK_BBOX)
    statuses = {entry["provider"]: entry for entry in payload["provider_status"]}
    assert "opentopography" in statuses, statuses
    assert statuses["opentopography"]["state"] == "ok"
    assert statuses["opentopography"]["dataset_count"] > 0


def test_a_provider_out_of_coverage_explains_itself(tools):
    """Ontario is outside every UK provider's declared extent.

    With only the global adapter implemented, what this checks is that the
    search still succeeds and reports a real menu rather than treating the
    absence of UK providers as a failure.
    """
    payload = search(tools, bbox=ONTARIO_BBOX)
    assert payload["ok"] is True, describe(payload)
    assert payload["degraded"] is False
    assert payload["by_data_type"][DataType.ELEVATION], describe(payload)


def test_the_declared_versus_implemented_gap_is_stated(tools, context):
    """A declared provider with no adapter is named, not silently omitted.

    Otherwise its absence is indistinguishable from a provider that had
    nothing for this area, and the two call for completely different responses.

    This module's fixture deliberately runs one adapter, so the gap it reports
    is the other four. That is a property of the fixture, not of the product —
    see the test below, which asserts against the real adapter set.
    """
    from qgis_mcp_server.providers.catalogue import ALL_PROVIDERS

    payload = search(tools, bbox=UK_BBOX)
    pending = {entry["name"] for entry in payload["providers_not_yet_implemented"]}

    # The fixture wires exactly one adapter, so the gap is everything else —
    # derived from the catalogue rather than listed, so adding a provider does
    # not fail this test for a reason that has nothing to do with it.
    wired = {adapter.name for adapter in context.providers.adapters}
    assert pending == {p.name for p in ALL_PROVIDERS} - wired, sorted(pending)

    for entry in payload["providers_not_yet_implemented"]:
        assert "not yet implemented" in entry["status"], entry


def test_the_shipped_server_has_no_unimplemented_providers(config):
    """Task 4.8 closed the gap, and this is what keeps it closed.

    The test above runs against a cut-down dispatcher, so it would keep
    passing however many adapters were added or removed. This one builds the
    real context: a provider declared in the catalogue without an adapter
    wired into it fails here.
    """
    from qgis_mcp_server.context import ServerContext
    from qgis_mcp_server.providers.catalogue import ALL_PROVIDERS

    with ServerContext(config) as context:
        wired = {adapter.name for adapter in context.providers.adapters}

    declared = {provider.name for provider in ALL_PROVIDERS}
    assert declared - wired == set(), (
        f"declared but not wired into the dispatcher: {sorted(declared - wired)}"
    )
    assert wired - declared == set(), (
        f"wired but not declared in the catalogue: {sorted(wired - declared)}"
    )


# ---------------------------------------------------------------------------
# Dataset-level exclusions
# ---------------------------------------------------------------------------


def test_area_limited_datasets_are_reported_as_excluded(tools):
    """A continental AOI leaves only coarse products, and says why.

    Without this the menu would show one 450 m dataset and look like that is
    all that exists for Europe — the remedy, splitting the AOI, is only
    obvious once the area limit is stated.
    """
    payload = search(tools, bbox=CONTINENTAL_BBOX)
    assert payload["ok"] is True, describe(payload)
    excluded = {entry["identifier"]: entry for entry in payload["dataset_exclusions"]}
    assert "COP30" in excluded, sorted(excluded)
    assert "450,000 km2" in excluded["COP30"]["reason"], excluded["COP30"]
    assert excluded["COP30"]["remedy"], excluded["COP30"]
    assert excluded["COP30"]["provider"] == "opentopography"


def test_latitude_limited_datasets_are_reported_as_excluded(tools):
    """Above 60°N the SRTM family returns nothing, which is easy to not notice."""
    payload = search(tools, bbox=ARCTIC_BBOX)
    excluded = {entry["identifier"] for entry in payload["dataset_exclusions"]}
    assert {"SRTMGL1", "SRTMGL3", "NASADEM"} <= excluded, sorted(excluded)
    offered = {
        d["identifier"] for d in payload["by_data_type"][DataType.ELEVATION]
    }
    assert "COP30" in offered, offered


def test_no_exclusions_for_a_small_well_covered_aoi(tools):
    """So the field means something when it does appear."""
    payload = search(tools, bbox=UK_BBOX)
    assert "dataset_exclusions" not in payload, describe(
        payload.get("dataset_exclusions")
    )


# ---------------------------------------------------------------------------
# REQ-12 preview
# ---------------------------------------------------------------------------


def test_uk_search_previews_the_national_grid(tools):
    payload = search(tools, bbox=UK_BBOX)
    target = payload["target_crs"]
    assert target["tier"] == 1, describe(target)
    assert target["crs"] == "EPSG:27700", describe(target)
    assert target["requires_choice"] is False


def test_continental_search_previews_a_required_choice(tools):
    """Better to learn a decision is needed before downloading than after.

    REQ-12's tier 3 needs a cartographic judgement, and discovering that after
    three layers have arrived in the wrong CRS is the expensive order.
    """
    payload = search(tools, bbox=CONTINENTAL_BBOX)
    target = payload["target_crs"]
    assert target["tier"] == 3, describe(target)
    assert target["requires_choice"] is True
    assert "crs" not in target, describe(target)
    assert len(target["candidates"]) >= 2


def test_place_search_previews_the_crs_for_the_resolved_aoi(tools):
    payload = search(tools, place=RESOLVES)
    assert payload["target_crs"]["crs"] == "EPSG:27700", describe(
        payload["target_crs"]
    )


# ---------------------------------------------------------------------------
# Mode reporting
# ---------------------------------------------------------------------------


def test_search_reports_the_serving_mode(tools):
    payload = search(tools, bbox=UK_BBOX)
    assert payload["mode"] == ExecutionMode.HEADLESS
    assert payload["mode_reason"]


# ---------------------------------------------------------------------------
# get_dataset_licence_info (task 4.6)
# ---------------------------------------------------------------------------


def test_licence_info_reports_every_provider(tools):
    """Every declared provider, derived rather than listed.

    A hardcoded set here would be a second copy of the catalogue, and the two
    drift: adding a provider fails this test for no reason, which trains people
    to update the list without reading it. Comparing against ALL_PROVIDERS
    keeps the assertion meaning what its name says — a provider that gains no
    licence entry still fails.
    """
    from qgis_mcp_server.providers.catalogue import ALL_PROVIDERS

    payload = tools.tools["get_dataset_licence_info"]()
    assert payload["ok"] is True, describe(payload)
    reported = {entry["provider"] for entry in payload["licences"]}
    declared = {provider.name for provider in ALL_PROVIDERS}
    assert reported == declared, (
        f"missing a licence entry: {sorted(declared - reported)}; "
        f"unexpected: {sorted(reported - declared)}"
    )


def test_licence_info_states_the_osm_share_alike_obligation(tools):
    """The obligation most likely to catch out commission work."""
    payload = tools.tools["get_dataset_licence_info"](provider="openstreetmap")
    licence = payload["licences"][0]["licence"]
    assert licence["tier"] == LicenceTier.OPEN_SHARE_ALIKE
    assert "OpenStreetMap contributors" in licence["attribution"]
    assert "share-alike" in licence["commercial_note"].lower(), licence
    # And the distinction that actually decides the question for a map render.
    assert "Produced Work" in licence["commercial_note"], licence


def test_licence_info_flags_opentopographys_commercial_restriction(tools):
    payload = tools.tools["get_dataset_licence_info"](provider="opentopography")
    licence = payload["licences"][0]["licence"]
    assert licence["needs_commercial_check"] is True
    assert "Enterprise" in licence["commercial_note"], licence
    assert payload["needs_commercial_check"] == ["opentopography"]


def test_public_domain_provider_is_not_flagged(tools):
    """So the flag means something when it appears."""
    payload = tools.tools["get_dataset_licence_info"](provider="natural_earth")
    assert payload["licences"][0]["licence"]["tier"] == LicenceTier.PUBLIC_DOMAIN
    assert payload["needs_commercial_check"] == []


def test_mixed_tier_provider_caveats_a_per_dataset_question(tools):
    """A provider-level answer is not sufficient where terms vary per product.

    Saying so beats implying the answer covers the dataset asked about.
    """
    payload = tools.tools["get_dataset_licence_info"](
        provider="os_data_hub", dataset="OS Terrain 5"
    )
    entry = payload["licences"][0]
    assert entry["dataset"] == "OS Terrain 5"
    assert "does not settle" in entry["caveat"], entry
    assert "OS Terrain 5" in entry["caveat"], entry


def test_unknown_provider_returns_the_known_list(tools):
    payload = tools.tools["get_dataset_licence_info"](provider="nosuchprovider")
    assert payload["ok"] is False, describe(payload)
    assert "opentopography" in payload["error"]["known_providers"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_tools_register_with_their_warnings(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()

    by_name = {tool.name: tool for tool in registered}
    for expected in ("search_data_sources", "get_dataset_licence_info"):
        assert expected in by_name, sorted(by_name)

    search_description = by_name["search_data_sources"].description or ""
    # The two things a caller would otherwise get wrong: treating a missing key
    # as a blocker, and reading a coarse-only menu as all that exists.
    assert "NOT a blocker" in search_description, search_description
    assert "dataset_exclusions" in search_description, search_description
    assert "Nothing is downloaded" in search_description, search_description
