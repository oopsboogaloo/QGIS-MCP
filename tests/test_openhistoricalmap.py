"""OpenHistoricalMap: the historic provider.

Most of this file exists because of one bug. The first implementation filtered
by year with an Overpass ``(if:)`` clause against ``start_decdate`` and
``end_decdate`` — tags OHM does not have. ``!is_tag('start_decdate')`` was
therefore always true, the clause always passed, and the filter silently did
nothing: 1500, 1900 and 2024 all returned exactly 143 buildings.

Nothing errored. The layer was real, the dates on it were real, and it was the
wrong set of features. So the tests here are weighted toward the filter being
*observably* a filter rather than toward it merely running.
"""

from __future__ import annotations

import json

import pytest

from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.providers.openhistoricalmap import (
    MAX_TILE_DEGREES_SQUARED,
    THEMES,
    OhmError,
    OpenHistoricalMapAdapter,
    build_query,
    extant_in,
    parse_year,
)
from qgis_mcp_server.providers.openstreetmap import (
    MAX_TILE_DEGREES_SQUARED as OSM_TILE_AREA,
)
from qgis_mcp_server.providers.openstreetmap import tile_aoi

LONDON = (-0.55, 51.20, 0.30, 51.70)
CITY = (-0.095, 51.505, -0.075, 51.515)


# ---------------------------------------------------------------------------
# Parsing dates people wrote by hand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1849-10-30", 1849),
        ("1849-10", 1849),
        ("1882", 1882),
        ("~1900", 1900),
        ("1890s", 1890),
        ("C19", 1801),
        ("19C", 1801),
        ("-500", -500),
        ("?", None),
        ("", None),
        (None, None),
        ("sometime in the war", None),
    ],
)
def test_year_parsing(value, expected):
    """OSM dates are written by people and vary accordingly."""
    assert parse_year(value) == expected


def test_an_unparseable_date_is_none_rather_than_a_guess():
    """Inventing a year is worse than admitting none.

    Silently placing a feature in the wrong decade is exactly the failure a
    date filter exists to prevent, so anything unrecognised declines to answer.
    """
    assert parse_year("mid-Victorian") is None
    assert parse_year("between 1880 and 1890") is None


def test_a_century_is_taken_as_its_first_year():
    """Erring toward inclusion: C19 covers 1801-1900, so a query for 1850
    should find it rather than miss it on a rounding choice."""
    assert parse_year("C19") == 1801
    assert extant_in({"start_date": "C19"}, 1850) is True


# ---------------------------------------------------------------------------
# What "existed in year N" means
# ---------------------------------------------------------------------------


def test_a_feature_is_extant_between_its_dates():
    tags = {"start_date": "1849-10-30", "end_date": "1962"}
    assert extant_in(tags, 1900) is True
    assert extant_in(tags, 1800) is False
    assert extant_in(tags, 2024) is False


def test_no_end_date_means_still_standing():
    """The Tower of London has a start_date and no end.

    Treating a missing end as "gone" would empty the map of everything that
    survives, which is most of what anyone wants to see.
    """
    assert extant_in({"start_date": "1078"}, 1900) is True
    assert extant_in({"start_date": "1078"}, 2024) is True
    assert extant_in({"start_date": "1078"}, 1000) is False


def test_no_start_date_means_it_predates_the_record():
    assert extant_in({"end_date": "1666"}, 1600) is True
    assert extant_in({"end_date": "1666"}, 1700) is False


def test_undated_features_are_kept_by_default():
    """Most of OHM has no dates at all.

    Dropping them would return a near-empty map, which reads as poor coverage
    rather than as an over-strict query — and would hide the difference.
    """
    assert extant_in({}, 1900) is True
    assert extant_in({}, 1900, keep_undated=False) is False


# ---------------------------------------------------------------------------
# The bug this file exists for
# ---------------------------------------------------------------------------


def test_the_query_carries_no_date_clause():
    """The regression guard.

    Filtering server-side was the original design and it failed silently,
    because the tags it referenced do not exist. If a date clause reappears in
    the query, the filtering has moved somewhere it cannot be tested without a
    network — which is how this went unnoticed in the first place.
    """
    buildings = next(t for t in THEMES if t.data_type == DataType.BUILDINGS)
    query = build_query(buildings, CITY)
    assert "if:" not in query, query
    assert "decdate" not in query, query
    assert "out geom;" in query


def test_the_filter_actually_removes_things():
    """Asserted on counts, which is what would have caught the original bug.

    Three features spanning different periods: a query for 1900 must keep a
    different set from one for 1700, and both must differ from no filter at
    all. "It ran" is not the property that matters.
    """
    features = [
        {"start_date": "1600", "end_date": "1750"},   # gone by 1900
        {"start_date": "1849", "end_date": "1962"},   # there in 1900
        {"start_date": "1990"},                        # not yet built
    ]
    in_1700 = [f for f in features if extant_in(f, 1700)]
    in_1900 = [f for f in features if extant_in(f, 1900)]
    in_2024 = [f for f in features if extant_in(f, 2024)]

    assert len(in_1700) == 1
    assert len(in_1900) == 1
    assert len(in_2024) == 1
    assert in_1700 != in_1900 != in_2024
    assert in_1700[0]["start_date"] == "1600"
    assert in_1900[0]["start_date"] == "1849"


# ---------------------------------------------------------------------------
# Tiling, search, and errors
# ---------------------------------------------------------------------------


def test_ohm_uses_larger_tiles_than_osm():
    """OHM holds a small fraction of OSM's density for the same ground.

    Using OSM's threshold would issue twelve queries over London where two
    suffice — against a single volunteer-run instance with no mirror.
    """
    assert MAX_TILE_DEGREES_SQUARED > OSM_TILE_AREA
    assert len(tile_aoi(LONDON, max_area=MAX_TILE_DEGREES_SQUARED)) < len(
        tile_aoi(LONDON)
    )


def test_search_needs_no_network_or_credentials():
    offline = OpenHistoricalMapAdapter(endpoints=("http://127.0.0.1:1/nope",))
    assert offline.search(LONDON)
    assert offline.is_usable() is True


def test_history_never_outranks_the_present():
    """A search for what is there now must not be led by 1900.

    The dispatcher sorts on preference and the leading entry is what gets
    taken, so this is the difference between a current map and an accidental
    period one.
    """
    adapter = OpenHistoricalMapAdapter(year=1900)
    assert all(dataset.preference >= 9 for dataset in adapter.search(LONDON))


def test_the_year_appears_in_the_dataset_title():
    """Two layers from the same theme at different years must be tellable
    apart in a layer tree."""
    adapter = OpenHistoricalMapAdapter(year=1900)
    assert "1900" in adapter.search(LONDON, (DataType.RAILWAYS,))[0].title


def test_patchy_coverage_is_stated_on_every_dataset():
    """An empty historic layer means "unmapped" far more often than "empty".

    The two call for opposite responses — go and map it, versus there was
    nothing there — so the caller has to be told which they are looking at.
    """
    for dataset in OpenHistoricalMapAdapter().search(LONDON):
        assert "volunteer-built" in dataset.extra["coverage_caveat"]
        assert "not that nothing was there" in dataset.extra["coverage_caveat"]


def test_share_alike_is_declared():
    """Same licence as OSM, and the same distinction that catches people out."""
    dataset = OpenHistoricalMapAdapter().search(LONDON)[0]
    licence = dataset.licence.to_dict()
    assert "share" in licence["summary"].lower()
    assert "OpenHistoricalMap" in licence["attribution"]
    assert "Produced Work" in licence["commercial_note"]


def test_the_format_is_declared_for_the_pipeline():
    from qgis_mcp_server.acquisition import is_vector, suffix_for

    dataset = OpenHistoricalMapAdapter().search(LONDON)[0]
    assert dataset.format == "GeoJSON"
    assert suffix_for(dataset) == ".geojson"
    assert is_vector(dataset) is True


def test_an_unknown_theme_lists_the_real_ones(tmp_path):
    from qgis_mcp_server.providers.base import Dataset
    from qgis_mcp_server.providers.catalogue import OPENHISTORICALMAP_LICENCE

    bogus = Dataset(
        provider="openhistoricalmap",
        identifier="ohm_unicorns",
        title="?",
        data_type=DataType.BUILDINGS,
        licence=OPENHISTORICALMAP_LICENCE,
    )
    with pytest.raises(OhmError) as caught:
        OpenHistoricalMapAdapter().fetch(bogus, CITY, tmp_path / "x.geojson")
    assert "ohm_buildings" in str(caught.value)


def test_a_failure_says_there_is_no_mirror(tmp_path):
    """Unlike OSM, OHM has one instance. A retry elsewhere is not an option,
    and the message should not imply it is."""
    offline = OpenHistoricalMapAdapter(
        endpoints=("http://127.0.0.1:1/nope",), socket_timeout=2.0
    )
    dataset = offline.search(CITY, (DataType.RAILWAYS,))[0]
    with pytest.raises(OhmError) as caught:
        offline.fetch(dataset, CITY, tmp_path / "x.geojson")
    assert "no mirror" in str(caught.value)


# ---------------------------------------------------------------------------
# Live (integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_fetch_differs_by_year(tmp_path):
    """The test that would have caught the original bug.

    One small AOI, three years, and the counts must differ. The broken version
    returned exactly the same number for every year and nothing else about it
    looked wrong.
    """
    aoi = CITY
    counts = {}
    for year in (1700, 1900, 2024):
        adapter = OpenHistoricalMapAdapter(year=year)
        dataset = adapter.search(aoi, (DataType.BUILDINGS,))[0]
        path = adapter.fetch(dataset, aoi, tmp_path / f"b{year}.geojson")
        collection = json.loads(path.read_text(encoding="utf-8"))
        counts[year] = collection["source"]["features"]
        assert collection["source"]["features_before_date_filter"] >= counts[year]

    assert len(set(counts.values())) > 1, (
        f"the same count for every year means the filter is not filtering: {counts}"
    )
    assert counts[1700] < counts[1900], counts
