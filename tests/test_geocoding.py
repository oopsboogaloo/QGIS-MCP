"""Task 4.1: place resolution and the REQ-9 ambiguity contract.

`tasks.md` 4.1: "cached real responses committed as fixtures. An ambiguous name
returns the candidate list; an unambiguous one proceeds without asking. No
geocoder mocking needed — the cache makes it deterministic."

So every geocoder here is the **real** `NominatimGeocoder` reading **real**
recorded responses, with `offline=True` and a read-only cache. That combination
is doing two jobs: it makes the suite deterministic and network-free, and it
means an accidental network call fails loudly as a missing fixture rather than
passing quietly against live data that may have changed.

The fixtures are regenerated with `test-data/record_geocoder_fixtures.py`.
"""

from __future__ import annotations

import pytest

from conftest import CollectingServer, describe
from qgis_mcp_protocol import ErrorCode
from qgis_mcp_server.geocoding import (
    NominatimGeocoder,
    PlaceResolver,
    Resolution,
    ResolutionCache,
    cache_key,
)
from qgis_mcp_server.geocoding.base import SMALL_AOI_DEGREES, GeocoderError
from qgis_mcp_server.geocoding.nominatim import (
    admin_context_from_address,
    bbox_from_nominatim,
)

#: Queries recorded in the fixture cache, with what each one is here to prove.
RESOLVES = "Andover, Hampshire, England"
AMBIGUOUS_UK_AND_US = "Newport"
AMBIGUOUS_MANY = "Springfield"
AMBIGUOUS_ADMIN = "Hampshire, England"
AMBIGUOUS_CONTINENTS = "Stonehenge"
NOT_FOUND = "Qqzzxx Nonexistent Placename"


@pytest.fixture(scope="module")
def fixture_cache(request):
    """The committed cache, read-only so a miss cannot be papered over."""
    path = request.config.rootpath / "test-data" / "geocoder-cache"
    if not path.is_dir():
        pytest.skip(
            "geocoder fixtures missing. Run: "
            "python test-data/record_geocoder_fixtures.py"
        )
    return ResolutionCache(path, read_only=True)


@pytest.fixture(scope="module")
def geocoder(fixture_cache):
    """The real Nominatim adapter, forbidden from reaching the network."""
    return NominatimGeocoder(cache=fixture_cache, offline=True)


@pytest.fixture(scope="module")
def resolver(geocoder):
    return PlaceResolver(geocoders=(geocoder,))


@pytest.fixture(scope="module")
def place_tools(context, resolver):
    """Tools registered against a context whose resolver is offline."""
    from qgis_mcp_server.tools import acquisition

    context.places = resolver
    server = CollectingServer()
    acquisition.register(server, context)
    return server


# ---------------------------------------------------------------------------
# The coordinate-order conversion
# ---------------------------------------------------------------------------


def test_nominatim_bbox_order_is_converted():
    """Nominatim orders its box ``[south, north, west, east]``.

    The third ordering convention in this codebase, and like the others it
    converts silently when mishandled: a latitude read as a longitude is a
    valid number in a plausible range, so the result is a box somewhere off
    west Africa rather than an error.
    """
    # Andover: south 51.19, north 51.24, west -1.53, east -1.42.
    assert bbox_from_nominatim(["51.1945", "51.2371", "-1.5344", "-1.4211"]) == (
        pytest.approx(-1.5344),
        pytest.approx(51.1945),
        pytest.approx(-1.4211),
        pytest.approx(51.2371),
    )


def test_converted_bbox_is_not_the_input_order():
    """Guards the conversion against being quietly removed."""
    raw = ["51.1945", "51.2371", "-1.5344", "-1.4211"]
    converted = bbox_from_nominatim(raw)
    assert converted != tuple(float(value) for value in raw), (
        "bbox_from_nominatim returned Nominatim's own order unchanged"
    )


@pytest.mark.parametrize(
    "raw", [[], ["1", "2", "3"], ["a", "b", "c", "d"], None]
)
def test_malformed_bbox_is_refused(raw):
    with pytest.raises(GeocoderError):
        bbox_from_nominatim(raw or [])


def test_admin_context_walks_whatever_fields_exist():
    """Nominatim's address fields vary by place type.

    A village has a county; a US city has a state instead. Assuming a fixed
    shape would produce empty context for half of all places, and context is
    precisely what REQ-9 needs to tell two Newports apart.
    """
    village = admin_context_from_address(
        {"county": "Hampshire", "state": "England", "country": "United Kingdom"}
    )
    assert village == "Hampshire, England, United Kingdom"

    city = admin_context_from_address(
        {"city": "Springfield", "state": "Illinois", "country": "United States"}
    )
    assert city == "Springfield, Illinois, United States"

    assert admin_context_from_address({}) == ""
    assert admin_context_from_address(None) == ""


# ---------------------------------------------------------------------------
# REQ-9: an unambiguous name proceeds
# ---------------------------------------------------------------------------


def test_fully_qualified_name_resolves_without_asking(resolver):
    """The REQ-9 happy path, on a real recorded response.

    "Andover, Hampshire, England" returns two candidates — the town at 0.473
    and a railway station at 0.319. Only one clears the threshold, so it is
    exactly one and resolution proceeds.
    """
    resolution = resolver.resolve(RESOLVES)
    assert resolution.state == Resolution.RESOLVED, describe(resolution.to_dict())

    place = resolution.resolved
    assert place is not None
    assert place.name == "Andover", describe(place.to_dict())
    assert place.country_code == "gb", describe(place.to_dict())
    assert place.place_type == "administrative", describe(place.to_dict())


def test_resolved_place_carries_an_aoi_in_the_expected_order(resolver):
    """The bbox is the payload: it becomes the AOI for everything downstream."""
    payload = resolver.resolve(RESOLVES).to_dict()
    assert payload["aoi_crs"] == "EPSG:4326"
    min_lon, min_lat, max_lon, max_lat = payload["aoi"]
    assert min_lon < max_lon and min_lat < max_lat, payload["aoi"]
    # Andover is in Hampshire: just west of Greenwich, just north of 51.
    assert -2.0 < min_lon < -1.0, payload["aoi"]
    assert 51.0 < min_lat < 51.5, payload["aoi"]


def test_resolved_aoi_feeds_the_crs_policy(resolver):
    """The two halves of the acquisition front end have to agree.

    A UK place's AOI must land in REQ-12's tier 1. If the bbox order were
    wrong, this is where it would show: the coordinates would fall outside the
    UK and the policy would answer tier 2 or 3.
    """
    from qgis_mcp_server.crs_policy import select_target_crs

    aoi = resolver.resolve(RESOLVES).to_dict()["aoi"]
    selection = select_target_crs(tuple(aoi))
    assert selection.tier == 1, (
        f"a UK place's AOI should resolve to tier 1; got tier "
        f"{selection.tier}\naoi: {aoi}"
    )
    assert selection.crs == "EPSG:27700"


def test_resolution_keeps_the_candidates_it_set_aside(resolver):
    """So a caller can see what was rejected rather than trusting nothing was."""
    resolution = resolver.resolve(RESOLVES)
    assert len(resolution.candidates) > 1, describe(resolution.to_dict())
    assert resolution.candidates[0] is resolution.resolved
    assert len(resolution.above_threshold) == 1, describe(resolution.to_dict())


def test_boundary_polygon_is_reported_when_present(resolver):
    """Real boundaries, not rectangles — the reason Nominatim is the default."""
    place = resolver.resolve(RESOLVES).resolved
    assert place.geometry is not None, "expected a boundary polygon"
    payload = place.to_dict()
    assert payload["has_boundary_polygon"] is True
    assert payload["geometry_type"] in ("Polygon", "MultiPolygon"), payload


# ---------------------------------------------------------------------------
# REQ-9: an ambiguous name returns candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query", [AMBIGUOUS_UK_AND_US, AMBIGUOUS_MANY, AMBIGUOUS_ADMIN, AMBIGUOUS_CONTINENTS]
)
def test_ambiguous_names_do_not_resolve(resolver, query):
    resolution = resolver.resolve(query)
    assert resolution.state == Resolution.AMBIGUOUS, (
        f"{query!r} should be ambiguous\n{describe(resolution.to_dict())}"
    )
    assert resolution.resolved is None
    assert len(resolution.candidates) > 1


def test_ambiguous_candidates_carry_the_context_that_distinguishes_them(resolver):
    """REQ-9 requires "types and administrative context".

    Without it the caller is handed several identically-named places and no
    basis for choosing — which is the same dead end as being handed one.
    """
    resolution = resolver.resolve(AMBIGUOUS_UK_AND_US)
    for candidate in resolution.candidates:
        assert candidate.admin_context, (
            f"candidate {candidate.display_name!r} has no administrative "
            f"context, so it cannot be told apart from the others"
        )
        assert candidate.place_type, f"{candidate.display_name!r} has no type"

    contexts = {candidate.admin_context for candidate in resolution.candidates}
    assert len(contexts) > 1, (
        f"ambiguous candidates must differ in context: {contexts}"
    )


def test_newport_spans_countries(resolver):
    """The concrete case REQ-9's rationale describes.

    "Newport" resolves to places in both the UK and the US. Silently taking
    the highest-scoring one would acquire data for Rhode Island when Wales was
    meant — a successful download of the wrong place, which nothing downstream
    can detect.
    """
    resolution = resolver.resolve(AMBIGUOUS_UK_AND_US)
    countries = {c.country_code for c in resolution.candidates if c.country_code}
    assert {"gb", "us"} <= countries, (
        f"expected UK and US candidates; got {sorted(countries)}"
    )


def test_stonehenge_spans_three_continents(resolver):
    """"A similarly-named place on another continent", literally.

    Stonehenge matches Wiltshire, Saskatchewan and Queensland. Recorded from
    the live service — not a constructed example.
    """
    resolution = resolver.resolve(AMBIGUOUS_CONTINENTS)
    countries = {c.country_code for c in resolution.candidates if c.country_code}
    assert {"gb", "ca", "au"} <= countries, sorted(countries)


def test_ambiguity_message_tells_the_caller_what_to_do(resolver):
    resolution = resolver.resolve(AMBIGUOUS_MANY)
    assert "ambiguous" in resolution.message.lower(), resolution.message
    assert "pick one" in resolution.message.lower(), resolution.message


def test_weak_matches_are_ambiguous_rather_than_resolved(resolver):
    """When nothing clears the threshold, ask — do not take the best of a bad lot.

    Raising the threshold above every candidate's confidence must not turn into
    "not found": the candidates exist and are returned. Asking costs a
    round-trip; guessing costs the wrong data.
    """
    resolution = resolver.resolve(RESOLVES, threshold=0.99)
    assert resolution.state == Resolution.AMBIGUOUS, describe(resolution.to_dict())
    assert resolution.candidates, "candidates must still be returned"
    assert resolution.above_threshold == []
    assert "below the" in resolution.message, resolution.message


def test_lowering_the_threshold_can_resolve_a_weak_match(resolver):
    """The threshold is a real control, not decoration."""
    strict = resolver.resolve(AMBIGUOUS_CONTINENTS, threshold=0.99)
    assert strict.state == Resolution.AMBIGUOUS
    permissive = resolver.resolve(AMBIGUOUS_CONTINENTS, threshold=0.58)
    assert permissive.state == Resolution.RESOLVED, describe(permissive.to_dict())
    assert permissive.resolved.country_code == "gb"


# ---------------------------------------------------------------------------
# Not found, and too small
# ---------------------------------------------------------------------------


def test_unmatched_name_is_not_found_not_an_error(resolver):
    resolution = resolver.resolve(NOT_FOUND)
    assert resolution.state == Resolution.NOT_FOUND, describe(resolution.to_dict())
    assert resolution.candidates == []
    assert "no place matching" in resolution.message


def test_not_found_message_suggests_adding_context(resolver):
    resolution = resolver.resolve(NOT_FOUND)
    assert "administrative context" in resolution.message, resolution.message


def test_empty_query_is_refused_without_a_lookup(resolver):
    for query in ("", "   "):
        resolution = resolver.resolve(query)
        assert resolution.state == Resolution.NOT_FOUND
        assert resolution.message == "place name is empty"


def test_monument_sized_place_is_flagged_as_too_small(resolver):
    """Stonehenge is 140 m by 120 m — correct, and useless as an AOI.

    At 30 m elevation resolution that is some five pixels. A download would
    succeed and return nothing worth having, which reads as a broken pipeline
    rather than as an AOI that was never big enough.
    """
    resolution = resolver.resolve(AMBIGUOUS_CONTINENTS, threshold=0.58)
    place = resolution.resolved
    assert place.country_code == "gb"
    assert place.is_small_for_an_aoi is True, (
        f"bbox span {place.bbox} should be under {SMALL_AOI_DEGREES} degrees"
    )
    assert "warning" in place.to_dict(), describe(place.to_dict())


def test_a_town_is_not_flagged_as_too_small(resolver):
    """So the warning means something when it does appear."""
    place = resolver.resolve(RESOLVES).resolved
    assert place.is_small_for_an_aoi is False
    assert "warning" not in place.to_dict()


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def test_offline_geocoder_refuses_an_unrecorded_query(geocoder):
    """A fixture gap must fail loudly, not reach the network.

    This is what makes the whole suite trustworthy: without it, a test could
    pass against live data that has since changed, and nobody would know the
    fixture was never used.
    """
    with pytest.raises(GeocoderError) as caught:
        geocoder.resolve("Some Place Nobody Recorded")
    assert "no cached" in str(caught.value), str(caught.value)
    assert "network access is disabled" in str(caught.value), str(caught.value)


def test_cache_keys_normalise_the_query():
    """"Newport", "newport" and " newport " are one question, not three."""
    base = cache_key("nominatim", "Newport")
    assert cache_key("nominatim", "newport") == base
    assert cache_key("nominatim", "  NEWPORT  ") == base
    assert cache_key("nominatim", "New  port") != base


def test_cache_keys_separate_different_questions():
    """A different limit or country filter is a different question."""
    assert cache_key("nominatim", "Newport", {"limit": "10"}) != cache_key(
        "nominatim", "Newport", {"limit": "5"}
    )
    assert cache_key("nominatim", "Newport") != cache_key("other", "Newport")


def test_cache_round_trip(tmp_path):
    cache = ResolutionCache(tmp_path)
    assert cache.get("nominatim", "Newport") is None
    cache.put("nominatim", "Newport", [{"display_name": "Newport"}])
    assert cache.get("nominatim", "Newport") == [{"display_name": "Newport"}]


def test_read_only_cache_does_not_write(tmp_path):
    cache = ResolutionCache(tmp_path, read_only=True)
    assert cache.put("nominatim", "Newport", [{"x": 1}]) is None
    assert cache.get("nominatim", "Newport") is None


def test_corrupt_cache_entry_is_treated_as_a_miss(tmp_path):
    """The cache is an optimisation; a bad entry should cost one request."""
    cache = ResolutionCache(tmp_path)
    path = cache.path_for("nominatim", "Newport")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert cache.get("nominatim", "Newport") is None


def test_cached_entries_record_the_original_query(fixture_cache):
    """A fixture file has to be identifiable without recomputing its hash."""
    import json

    entries = fixture_cache.entries()
    assert entries, "no fixture entries found"
    payload = json.loads(entries[0].read_text(encoding="utf-8"))
    assert payload["query"], payload
    assert payload["provider"] == "nominatim", payload


def test_fixtures_cover_every_recorded_query(fixture_cache):
    """The recorder's query list and the committed fixtures must agree.

    A query added to the recorder but never run would otherwise show up as a
    confusing offline failure in whichever test used it next.
    """
    import json

    recorded = {
        json.loads(path.read_text(encoding="utf-8"))["query"]
        for path in fixture_cache.entries()
    }
    for query in (
        RESOLVES,
        AMBIGUOUS_UK_AND_US,
        AMBIGUOUS_MANY,
        AMBIGUOUS_ADMIN,
        AMBIGUOUS_CONTINENTS,
        NOT_FOUND,
    ):
        assert query in recorded, (
            f"{query!r} is used by the tests but not recorded. Run: "
            f"python test-data/record_geocoder_fixtures.py"
        )


# ---------------------------------------------------------------------------
# Geocoder tiering
# ---------------------------------------------------------------------------


def test_country_scoped_geocoder_is_preferred_where_it_applies(geocoder):
    """§4.6.1's tiering: a national gazetteer first, global as the fallback."""

    class UkOnly(NominatimGeocoder):
        name = "uk_gazetteer"
        country_scope = ("gb",)

    uk = UkOnly(cache=geocoder.cache, offline=True)
    resolver = PlaceResolver(geocoders=(geocoder, uk))

    assert [g.name for g in resolver._ordered("gb")] == ["uk_gazetteer", "nominatim"]
    # Outside its scope it is dropped rather than tried and failed.
    assert [g.name for g in resolver._ordered("us")] == ["nominatim"]
    assert [g.name for g in resolver._ordered(None)] == ["nominatim"]


def test_failed_geocoder_becomes_a_status_not_an_exception(fixture_cache):
    """Same reasoning as REQ-10 for data providers."""

    class Broken(NominatimGeocoder):
        name = "broken"

        def resolve(self, place, *, limit=10):
            raise GeocoderError("service unreachable")

    resolver = PlaceResolver(
        geocoders=(
            Broken(cache=fixture_cache, offline=True),
            NominatimGeocoder(cache=fixture_cache, offline=True),
        )
    )
    resolution = resolver.resolve(RESOLVES)

    assert resolution.state == Resolution.RESOLVED, describe(resolution.to_dict())
    states = {status.geocoder: status.state for status in resolution.statuses}
    assert states["broken"] == "failed", states
    assert states["nominatim"] == "ok", states


def test_all_geocoders_failing_is_unavailable_not_not_found(fixture_cache):
    """"Cannot reach the service" and "no such place" need different responses.

    One is worth retrying; the other is not.
    """

    class Broken(NominatimGeocoder):
        name = "broken"

        def resolve(self, place, *, limit=10):
            raise GeocoderError("service unreachable")

    resolver = PlaceResolver(geocoders=(Broken(cache=fixture_cache, offline=True),))
    resolution = resolver.resolve(RESOLVES)
    assert resolution.state == Resolution.UNAVAILABLE, describe(resolution.to_dict())
    assert "no geocoder could be reached" in resolution.message, resolution.message
    # The underlying cause travels too: "unavailable" alone would not say
    # whether to retry or to fix something.
    assert "service unreachable" in resolution.message, resolution.message


def test_no_geocoder_configured_is_unavailable():
    resolution = PlaceResolver(geocoders=()).resolve("Andover")
    assert resolution.state == Resolution.UNAVAILABLE
    assert "no geocoder is configured" in resolution.message


# ---------------------------------------------------------------------------
# The resolve_place tool
# ---------------------------------------------------------------------------


def test_tool_returns_ok_for_a_resolved_place(place_tools):
    payload = place_tools.tools["resolve_place"](place=RESOLVES)
    assert payload["ok"] is True, describe(payload)
    assert payload["state"] == Resolution.RESOLVED
    assert payload["place"]["name"] == "Andover", describe(payload["place"])
    assert payload["aoi_crs"] == "EPSG:4326"


def test_tool_returns_an_error_for_an_ambiguous_place(place_tools):
    """Ambiguity is an error so it cannot be mistaken for an answer.

    A successful response with a `state` field invites a caller to read
    `candidates[0]` and move on — which is precisely the failure REQ-9 exists
    to prevent. The candidates still travel, in the error.
    """
    payload = place_tools.tools["resolve_place"](place=AMBIGUOUS_UK_AND_US)
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.AMBIGUOUS_PLACE
    assert len(payload["error"]["candidates"]) > 1, describe(payload["error"])
    assert "aoi" not in payload["error"], (
        "an ambiguous resolution must not offer an AOI, or a caller will use it"
    )


def test_tool_distinguishes_not_found_from_ambiguous(place_tools):
    payload = place_tools.tools["resolve_place"](place=NOT_FOUND)
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.PLACE_NOT_FOUND
    assert payload["error"]["candidate_count"] == 0


def test_tool_reports_the_serving_mode(place_tools):
    payload = place_tools.tools["resolve_place"](place=RESOLVES)
    assert payload["mode"] == "headless"
    assert payload["mode_reason"]


def test_tool_description_warns_against_guessing(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()

    description = next(
        tool.description for tool in registered if tool.name == "resolve_place"
    )
    assert "AMBIGUITY IS AN ERROR" in description, description
    assert "[min_lon, min_lat, max_lon, max_lat]" in description, description
