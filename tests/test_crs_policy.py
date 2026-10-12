"""Task 4.3: target CRS selection (REQ-12).

`tasks.md` asks for "tier selection against AOIs with known correct answers — a
UK bbox resolves to EPSG:27700, a single-zone non-UK bbox to its UTM zone, a
continental bbox returns candidates rather than a CRS", and notes that tiers 1
and 2 are "deterministic and fully testable offline" while tier 3 "needs no
projection maths at all — only the candidate list".

So almost everything here runs without QGIS and takes milliseconds. The one
exception verifies the hardcoded EPSG:27700 bounds against the PROJ database
QGIS ships, so a PROJ update that moves them is reported rather than leaving
this module quietly believing something stale.
"""

from __future__ import annotations

import pytest

from conftest import assert_ok, describe
from qgis_mcp_server.crs_policy import (
    EUROPE_BOUNDS,
    UK_BOUNDS,
    UK_CRS,
    AoiError,
    RasterInput,
    select_target_crs,
    utm_epsg,
    utm_zone,
)

# Bounding boxes with known answers, in (min_lon, min_lat, max_lon, max_lat).
HAMPSHIRE = (-1.2700, 51.1700, -1.2550, 51.1800)   # the fixture's own ground
LONDON = (-0.51, 51.28, 0.33, 51.69)
SCOTLAND = (-5.0, 56.0, -3.0, 57.0)
COLORADO = (-106.0, 38.5, -105.0, 39.5)            # UTM 13N
NEW_ZEALAND = (172.0, -44.0, 173.0, -43.0)         # UTM 59S
CONTINENTAL_EUROPE = (-10.0, 36.0, 30.0, 60.0)     # many zones
CONTINENTAL_US = (-125.0, 25.0, -67.0, 49.0)       # many zones
EQUATOR_STRADDLE = (11.0, -1.0, 12.0, 1.0)         # one zone, two hemispheres


# ---------------------------------------------------------------------------
# Zone arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "longitude, expected",
    [
        (-180.0, 1),
        (-177.0, 1),
        (-6.0, 30),
        (-1.27, 30),
        (-0.001, 30),
        (0.0, 31),
        (5.999, 31),
        (6.0, 32),
        (177.0, 60),
        (180.0, 60),
    ],
)
def test_utm_zone_arithmetic(longitude, expected):
    assert utm_zone(longitude) == expected, (
        f"utm_zone({longitude}) should be {expected}, got {utm_zone(longitude)}"
    )


def test_utm_epsg_codes():
    assert utm_epsg(30, northern=True) == "EPSG:32630"
    assert utm_epsg(30, northern=False) == "EPSG:32730"
    assert utm_epsg(59, northern=False) == "EPSG:32759"


# ---------------------------------------------------------------------------
# Tier 1: the UK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("aoi", [HAMPSHIRE, LONDON, SCOTLAND])
def test_uk_aoi_resolves_to_the_national_grid(aoi):
    selection = select_target_crs(aoi)
    assert selection.tier == 1, describe(selection.to_dict())
    assert selection.crs == UK_CRS, (
        f"a UK AOI must resolve to {UK_CRS}\n"
        f"    actual: {selection.crs}\n{describe(selection.to_dict())}"
    )
    assert selection.requires_choice is False


def test_fixture_ground_resolves_to_the_national_grid():
    """The fixtures sit on real SU4080 coordinates for exactly this reason."""
    selection = select_target_crs(HAMPSHIRE)
    assert selection.tier == 1
    assert selection.crs == UK_CRS


def test_tier_1_takes_precedence_over_the_utm_zone():
    """A UK AOI is also inside UTM 30N; the national grid wins.

    Tier order is the whole point of the policy: for UK work EPSG:27700 is
    what the national data already uses, so choosing UTM would resample the
    highest-resolution input for nothing.
    """
    selection = select_target_crs(SCOTLAND)
    assert selection.tier == 1
    assert selection.crs == UK_CRS
    assert utm_zone(SCOTLAND[0]) == 30


# ---------------------------------------------------------------------------
# Tier 2: a single UTM zone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "aoi, expected_crs, expected_zone",
    [
        (COLORADO, "EPSG:32613", 13),
        (NEW_ZEALAND, "EPSG:32759", 59),
    ],
)
def test_single_zone_aoi_resolves_to_that_zone(aoi, expected_crs, expected_zone):
    selection = select_target_crs(aoi)
    assert selection.tier == 2, describe(selection.to_dict())
    assert selection.crs == expected_crs, (
        f"expected {expected_crs}, got {selection.crs}\n"
        f"{describe(selection.to_dict())}"
    )
    assert selection.utm_zones == (expected_zone,)


def test_southern_hemisphere_gets_a_southern_zone():
    """32759, not 32659. The difference is a 10,000 km false northing."""
    selection = select_target_crs(NEW_ZEALAND)
    assert selection.crs == "EPSG:32759", describe(selection.to_dict())


def test_aoi_ending_exactly_on_a_zone_boundary_stays_in_one_zone():
    """An AOI drawn deliberately to a zone's edge should not be pushed to tier 3.

    ``utm_zone(6.0)`` is 32 because the boundary belongs to the next zone, so a
    naive span calculation reads ``[0, 6]`` as two zones and asks the caller to
    choose for no reason.
    """
    selection = select_target_crs((0.0, 45.0, 6.0, 46.0))
    assert selection.tier == 2, describe(selection.to_dict())
    assert selection.utm_zones == (31,), describe(selection.to_dict())


def test_aoi_straddling_the_equator_is_not_tier_2():
    """One zone, but two hemispheres — neither serves the whole area."""
    selection = select_target_crs(EQUATOR_STRADDLE)
    assert selection.tier == 3, describe(selection.to_dict())
    assert any("equator" in note for note in selection.notes), describe(
        selection.to_dict()
    )


# ---------------------------------------------------------------------------
# Tier 3: the server does not choose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("aoi", [CONTINENTAL_EUROPE, CONTINENTAL_US])
def test_multi_zone_aoi_returns_candidates_and_no_crs(aoi):
    """REQ-12: the server SHALL NOT select a projection automatically here."""
    selection = select_target_crs(aoi)
    assert selection.tier == 3, describe(selection.to_dict())
    assert selection.crs is None, (
        f"tier 3 must not choose a CRS; it returned {selection.crs}\n"
        f"{describe(selection.to_dict())}"
    )
    assert selection.requires_choice is True
    assert selection.candidates, describe(selection.to_dict())


def test_tier_3_offers_both_an_equal_area_and_a_conformal_option(tmp_path):
    """REQ-12 asks for both, with the trade-off stated.

    The trade-off text is the load-bearing part: the decision is cartographic,
    and an agent presenting the choice has to be able to say what is lost
    either way.
    """
    selection = select_target_crs(CONTINENTAL_EUROPE)
    kinds = {candidate.kind for candidate in selection.candidates}
    assert "equal_area" in kinds, describe(selection.to_dict())
    assert "conformal" in kinds, describe(selection.to_dict())
    for candidate in selection.candidates:
        assert candidate.trade_off, f"{candidate.crs} has no stated trade-off"
    equal_area = next(c for c in selection.candidates if c.kind == "equal_area")
    conformal = next(c for c in selection.candidates if c.kind == "conformal")
    assert "area" in equal_area.trade_off
    assert "shape" in conformal.trade_off


def test_european_tier_3_offers_the_standard_european_codes():
    """A standard EPSG code beats a bespoke PROJ string when one fits."""
    selection = select_target_crs(CONTINENTAL_EUROPE)
    codes = {candidate.crs for candidate in selection.candidates}
    assert "EPSG:3035" in codes, describe(selection.to_dict())
    assert "EPSG:3034" in codes, describe(selection.to_dict())


def test_non_european_tier_3_still_offers_centred_projections():
    """Outside Europe there is no ready-made pair, so PROJ strings are built."""
    selection = select_target_crs(CONTINENTAL_US)
    codes = {candidate.crs for candidate in selection.candidates}
    assert "EPSG:3035" not in codes, describe(selection.to_dict())
    laea = [c for c in selection.candidates if c.kind == "equal_area"]
    assert laea and laea[0].crs.startswith("+proj=laea"), describe(
        selection.to_dict()
    )
    # Centred on the AOI, which is what REQ-12 asks for.
    assert "+lon_0=-96.0000" in laea[0].crs, laea[0].crs


def test_tier_3_lists_the_zones_it_spans(tmp_path):
    selection = select_target_crs(CONTINENTAL_EUROPE)
    assert len(selection.utm_zones) > 1, describe(selection.to_dict())
    assert selection.utm_zones[0] == utm_zone(CONTINENTAL_EUROPE[0])


def test_polar_aoi_is_flagged_as_beyond_utm():
    selection = select_target_crs((-30.0, 70.0, 30.0, 88.0))
    assert selection.tier == 3
    assert any("84" in note for note in selection.notes), describe(
        selection.to_dict()
    )


# ---------------------------------------------------------------------------
# The resampling refinement
# ---------------------------------------------------------------------------


def test_uk_dem_already_in_27700_is_not_resampled():
    """`tasks.md` names this case: a 27700 DEM and a 4326 vector.

    "Assert the DEM was not resampled." For UK work the refinement falls out
    for free, since tier 1 already lands on EPSG:27700 — which is the point
    `design.md` §5 makes about OS Terrain 5.
    """
    selection = select_target_crs(
        LONDON,
        inputs=(
            RasterInput(name="os_terrain_5", crs="EPSG:27700", resolution_m=5.0),
            RasterInput(name="copernicus_glo30", crs="EPSG:4326", resolution_m=30.0),
        ),
    )
    assert selection.crs == UK_CRS
    report = selection.resampling
    assert report is not None, describe(selection.to_dict())
    assert report["highest_resolution_input"] == "os_terrain_5", describe(report)
    assert report["will_be_resampled"] is False, (
        f"the 5 m national DEM must not be resampled; something always is, and "
        f"it should be the 30 m global one.\n{describe(report)}"
    )


def test_the_coarser_raster_is_the_one_that_gets_resampled():
    """Stated from the other direction, so the report cannot be vacuous."""
    selection = select_target_crs(
        COLORADO,
        inputs=(
            RasterInput(name="global_dem", crs="EPSG:4326", resolution_m=30.0),
        ),
    )
    report = selection.resampling
    assert report["will_be_resampled"] is True, describe(report)
    assert "global_dem" in report["note"], describe(report)


def test_prefer_crs_overrides_the_tier_default():
    """The escape hatch for the refinement's general form.

    Deciding whether an arbitrary candidate CRS covers the AOI needs a PROJ
    lookup, which this module deliberately lacks. The caller that has PyQGIS
    resolves coverage and passes the answer in.
    """
    selection = select_target_crs(COLORADO, prefer_crs="EPSG:26913")
    assert selection.crs == "EPSG:26913", describe(selection.to_dict())
    assert selection.tier == 2
    assert "overridden" in selection.reason, selection.reason


# ---------------------------------------------------------------------------
# Rejected input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "aoi, fragment",
    [
        ((10.0, 50.0, 5.0, 51.0), "antimeridian"),
        ((5.0, 51.0, 10.0, 50.0), "min_lat"),
        ((-200.0, 50.0, 5.0, 51.0), "min_lon"),
        ((5.0, -100.0, 10.0, 51.0), "min_lat"),
    ],
)
def test_malformed_aoi_is_refused_with_a_reason(aoi, fragment):
    with pytest.raises(AoiError) as caught:
        select_target_crs(aoi)
    assert fragment in str(caught.value), (
        f"the message should mention {fragment!r}\nmessage: {caught.value}"
    )


def test_reversed_longitude_suggests_the_antimeridian_split():
    """A reversed bbox is as likely an antimeridian crossing as a typo.

    Silently sorting the values would turn a real wrap-around AOI into a box
    covering most of the planet, which no projected CRS serves.
    """
    with pytest.raises(AoiError) as caught:
        select_target_crs((170.0, -10.0, -170.0, 10.0))
    assert "split it into two" in str(caught.value), str(caught.value)


def test_zero_area_aoi_still_answers_but_says_so():
    """A point AOI has a perfectly good target CRS, and is probably a mistake."""
    selection = select_target_crs((-1.0, 51.0, -1.0, 51.0))
    assert selection.crs == UK_CRS
    assert any("zero width" in note for note in selection.notes), describe(
        selection.to_dict()
    )


@pytest.mark.parametrize("aoi", [None, (1.0, 2.0), (1.0, 2.0, 3.0, 4.0, 5.0)])
def test_wrong_shaped_aoi_is_refused(aoi):
    with pytest.raises(AoiError):
        select_target_crs(aoi)


# ---------------------------------------------------------------------------
# The hardcoded bounds, checked against PROJ
# ---------------------------------------------------------------------------


def test_hardcoded_crs_bounds_match_the_proj_database(runner):
    """The one test here that needs QGIS.

    ``crs_policy`` is pure Python so the tier logic stays fast and dependency
    free, which means the EPSG:27700 and EPSG:3035 bounds are copied constants.
    Copied constants rot. This checks them against the PROJ database QGIS
    actually ships, so a PROJ update that moves an area of use is reported here
    rather than silently changing which AOIs count as British.
    """
    envelope = runner.run_code(
        "from qgis.core import QgsCoordinateReferenceSystem as C\n"
        "def bounds(code):\n"
        "    b = C(code).bounds()\n"
        "    return [b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum()]\n"
        "result = {'uk': bounds('EPSG:27700'), 'europe': bounds('EPSG:3035')}\n"
    )
    assert_ok(envelope, context_note="reading CRS bounds from PROJ")

    assert envelope.result["uk"] == pytest.approx(list(UK_BOUNDS)), (
        f"EPSG:27700 bounds have moved in PROJ\n"
        f"    crs_policy.UK_BOUNDS: {list(UK_BOUNDS)}\n"
        f"    PROJ reports:         {envelope.result['uk']}"
    )
    assert envelope.result["europe"] == pytest.approx(list(EUROPE_BOUNDS)), (
        f"EPSG:3035 bounds have moved in PROJ\n"
        f"    crs_policy.EUROPE_BOUNDS: {list(EUROPE_BOUNDS)}\n"
        f"    PROJ reports:             {envelope.result['europe']}"
    )
