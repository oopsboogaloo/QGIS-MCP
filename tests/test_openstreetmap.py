"""Task 4.8: the OpenStreetMap / Overpass adapter.

``design.md`` §7 predicted this would be where the acquisition layer needed
real work beyond the interface, and the prediction holds: tiling and merging
are the substance, and both are where a silent wrong answer is easiest to
produce. A road counted twice because it crossed a tile boundary looks correct
on screen --- the two copies are identical and coincident --- and is wrong in
every number derived from the layer.

So the arithmetic is pure and tested directly here, with no network. The one
test that does reach Overpass is marked ``integration`` and excluded from the
default pass, like the other live-provider tests (REQ-T2).
"""

from __future__ import annotations

import json

import pytest

from conftest import describe
from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.providers.openstreetmap import (
    MAX_TILE_DEGREES_SQUARED,
    THEMES,
    OpenStreetMapAdapter,
    OverpassError,
    aoi_area_degrees_squared,
    build_query,
    elements_to_geojson,
    merge_elements,
    tile_aoi,
)

#: Sudbury, Suffolk -- the AOI this adapter was written against.
SUDBURY = (0.6899, 52.0002, 0.7699, 52.0802)


# ---------------------------------------------------------------------------
# Tiling
# ---------------------------------------------------------------------------


def test_a_small_aoi_is_one_query():
    """The common case must not pay for the hard case.

    Sudbury is 0.08 degrees square, well inside the threshold. Splitting it
    would turn one query into several against a free shared service for no
    benefit.
    """
    assert aoi_area_degrees_squared(SUDBURY) < MAX_TILE_DEGREES_SQUARED
    assert tile_aoi(SUDBURY) == [SUDBURY]


def test_a_large_aoi_is_split():
    aoi = (0.0, 50.0, 1.0, 51.0)  # 1 square degree
    tiles = tile_aoi(aoi)
    assert len(tiles) > 1
    assert all(
        aoi_area_degrees_squared(tile) <= MAX_TILE_DEGREES_SQUARED * 1.01
        for tile in tiles
    ), [round(aoi_area_degrees_squared(t), 4) for t in tiles]


def test_tiles_cover_the_whole_aoi_exactly():
    """Nothing missed at the edges, and nothing invented beyond them.

    A gap between tiles loses data silently --- the layer simply has a strip of
    nothing, which reads as "OSM has no roads there" rather than as a bug.
    """
    # Comfortably inside MAX_TILES: this is testing the grid arithmetic, not
    # the refusal, and an AOI that trips the ceiling tests the wrong thing.
    aoi = (-1.0, 50.0, 0.0, 50.75)
    tiles = tile_aoi(aoi)

    assert min(tile[0] for tile in tiles) == pytest.approx(aoi[0])
    assert min(tile[1] for tile in tiles) == pytest.approx(aoi[1])
    assert max(tile[2] for tile in tiles) == pytest.approx(aoi[2])
    assert max(tile[3] for tile in tiles) == pytest.approx(aoi[3])

    # Areas sum to the whole, which can only hold if the tiles neither overlap
    # nor leave gaps.
    total = sum(aoi_area_degrees_squared(tile) for tile in tiles)
    assert total == pytest.approx(aoi_area_degrees_squared(aoi), rel=1e-9)


def test_a_long_thin_aoi_splits_along_its_long_axis():
    """Square tiles, not square tile counts.

    A 4x0.1 degree strip split 2x2 would give tiles twice as long as the whole
    AOI is tall, which defeats the purpose: it is the element count per tile
    that matters, and that tracks tile area and shape.
    """
    tiles = tile_aoi((0.0, 50.0, 4.0, 50.1))
    columns = len({round(tile[0], 6) for tile in tiles})
    rows = len({round(tile[1], 6) for tile in tiles})
    assert columns > rows, f"{columns} columns against {rows} rows"


def test_an_absurd_aoi_is_refused_before_any_request():
    """A continental AOI is Overpass being the wrong tool, and it should say so.

    Silently issuing hundreds of queries against a free shared service is how a
    client gets blocked for everyone.
    """
    with pytest.raises(OverpassError) as caught:
        tile_aoi((-10.0, 35.0, 30.0, 60.0))
    message = str(caught.value)
    assert "planet extract" in message, message
    assert "no requests were spent" in message, message


def test_a_reversed_aoi_is_refused_rather_than_silently_empty():
    """Transposed corners produce a negative span, which tiles into nonsense."""
    with pytest.raises(OverpassError) as caught:
        tile_aoi((1.0, 52.0, 0.0, 51.0))
    assert "wrong way round" in str(caught.value)


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def test_an_element_returned_by_two_tiles_is_kept_once():
    """The reason merging is not concatenation.

    ``out geom`` returns a way's whole geometry whenever it intersects the
    queried box, so a road crossing a tile boundary comes back complete from
    both tiles.
    """
    shared = {"type": "way", "id": 42, "geometry": [{"lat": 1, "lon": 1}]}
    merged = merge_elements(
        [
            {"elements": [shared, {"type": "way", "id": 1}]},
            {"elements": [shared, {"type": "way", "id": 2}]},
        ]
    )
    ids = sorted(element["id"] for element in merged)
    assert ids == [1, 2, 42], ids


def test_a_node_and_a_way_may_share_an_id():
    """Separate id spaces in OSM, so the key has to include the type.

    Keyed on id alone, a node would silently evict a way with the same number.
    """
    merged = merge_elements(
        [{"elements": [{"type": "node", "id": 7}, {"type": "way", "id": 7}]}]
    )
    assert len(merged) == 2, merged


def test_merging_nothing_is_empty_not_an_error():
    assert merge_elements([]) == []
    assert merge_elements([{"elements": []}]) == []


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------


def test_the_query_uses_overpass_bbox_order():
    """south,west,north,east -- the fourth coordinate convention in this project.

    GeoJSON is [lon, lat], QGIS extents are xmin,xmax,ymin,ymax, Nominatim
    returns [south, north, west, east], and Overpass wants a fourth order
    again. A transposition here returns data for somewhere else entirely, and
    looks like a successful fetch.
    """
    roads = next(theme for theme in THEMES if theme.data_type == DataType.ROADS)
    query = build_query(roads, SUDBURY)
    assert "(52.0002,0.6899,52.0802,0.7699)" in query, query


def test_the_query_asks_for_geometry():
    """Without `out geom` the ways come back as node ids and no coordinates."""
    roads = next(theme for theme in THEMES if theme.data_type == DataType.ROADS)
    assert "out geom;" in build_query(roads, SUDBURY)


def test_the_query_carries_a_timeout_overpass_will_honour():
    roads = next(theme for theme in THEMES if theme.data_type == DataType.ROADS)
    assert "[timeout:120]" in build_query(roads, SUDBURY, timeout=120)


def test_a_multi_selector_theme_queries_every_selector_and_kind():
    """Water is waterway lines and natural=water polygons, as ways and relations."""
    water = next(
        theme for theme in THEMES if theme.data_type == DataType.HYDROLOGY
    )
    query = build_query(water, SUDBURY)
    assert query.count('["waterway"]') == len(water.element_kinds)
    assert query.count('["natural"="water"]') == len(water.element_kinds)


def test_railways_are_a_separate_theme_from_roads():
    """Two networks, not one.

    Folding rail into "roads" would mean a caller asking for a road network
    silently receiving railway lines through it, which is wrong cartography and
    wrong topology — they do not connect.
    """
    adapter = OpenStreetMapAdapter()
    roads = adapter.search(SUDBURY, (DataType.ROADS,))
    rail = adapter.search(SUDBURY, (DataType.RAILWAYS,))
    assert [d.identifier for d in roads] == ["osm_roads"]
    assert [d.identifier for d in rail] == ["osm_railways"]


def test_the_railway_query_selects_the_railway_tag():
    rail = next(theme for theme in THEMES if theme.data_type == DataType.RAILWAYS)
    query = build_query(rail, SUDBURY)
    assert '["railway"]' in query, query
    assert '["highway"]' not in query


def test_disused_lines_are_kept_rather_than_filtered():
    """`railway=abandoned` is still a railway, and over LIDAR it is visible.

    The selector is the bare tag rather than railway=rail, so a line lifted
    decades ago still comes back — which is the point when the embankment is
    legible in 1 m relief underneath it.
    """
    rail = next(theme for theme in THEMES if theme.data_type == DataType.RAILWAYS)
    assert rail.selectors == ('["railway"]',)
    assert "abandoned" in rail.notes


# ---------------------------------------------------------------------------
# GeoJSON conversion
# ---------------------------------------------------------------------------


def test_a_closed_way_becomes_a_polygon():
    """OSM has no area type: a closed way *is* the area.

    Emitting it as a LineString would make every building a hollow outline --
    valid geometry, wrong type, and no area to compute.
    """
    ring = [
        {"lon": 0.0, "lat": 0.0},
        {"lon": 1.0, "lat": 0.0},
        {"lon": 1.0, "lat": 1.0},
        {"lon": 0.0, "lat": 0.0},
    ]
    collection = elements_to_geojson(
        [{"type": "way", "id": 1, "geometry": ring, "tags": {"building": "yes"}}]
    )
    geometry = collection["features"][0]["geometry"]
    assert geometry["type"] == "Polygon", describe(geometry)
    assert geometry["coordinates"][0][0] == geometry["coordinates"][0][-1]


def test_an_open_way_becomes_a_linestring():
    line = [{"lon": 0.0, "lat": 0.0}, {"lon": 1.0, "lat": 1.0}]
    collection = elements_to_geojson([{"type": "way", "id": 2, "geometry": line}])
    assert collection["features"][0]["geometry"]["type"] == "LineString"


def test_a_node_becomes_a_point():
    collection = elements_to_geojson(
        [{"type": "node", "id": 3, "lat": 52.0, "lon": 0.7, "tags": {"place": "town"}}]
    )
    feature = collection["features"][0]
    assert feature["geometry"] == {"type": "Point", "coordinates": [0.7, 52.0]}
    assert feature["properties"]["place"] == "town"


def test_coordinates_are_lon_lat_not_lat_lon():
    """Overpass names its coordinates; GeoJSON positions them.

    Overpass gives {"lat": .., "lon": ..} and GeoJSON wants [lon, lat]. Getting
    this backwards puts Suffolk in the Indian Ocean, and every downstream step
    still succeeds.
    """
    collection = elements_to_geojson(
        [{"type": "node", "id": 4, "lat": 52.04, "lon": 0.73}]
    )
    longitude, latitude = collection["features"][0]["geometry"]["coordinates"]
    assert (longitude, latitude) == (0.73, 52.04)


def test_an_element_without_usable_geometry_is_skipped_not_guessed():
    """A relation Overpass could not resolve has no geometry to invent."""
    collection = elements_to_geojson(
        [
            {"type": "relation", "id": 5, "tags": {"building": "yes"}},
            {"type": "way", "id": 6, "geometry": [{"lon": 0, "lat": 0}]},
        ]
    )
    assert collection["features"] == []


def test_tags_survive_as_properties():
    collection = elements_to_geojson(
        [
            {
                "type": "way",
                "id": 7,
                "geometry": [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 1}],
                "tags": {"highway": "primary", "name": "Melford Road", "ref": "A134"},
            }
        ]
    )
    properties = collection["features"][0]["properties"]
    assert properties["highway"] == "primary"
    assert properties["name"] == "Melford Road"
    assert properties["osm_id"] == 7
    assert properties["osm_type"] == "way"


def test_the_collection_declares_crs84_and_attribution():
    """ODbL requires attribution, so it travels with the data, not beside it."""
    collection = elements_to_geojson([], attribution="(c) OpenStreetMap contributors")
    assert collection["crs"]["properties"]["name"] == "urn:ogc:def:crs:OGC:1.3:CRS84"
    assert "OpenStreetMap" in collection["attribution"]


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter():
    return OpenStreetMapAdapter()


def test_search_needs_no_network_and_no_credentials(adapter):
    """OSM has no catalogue to query and no key to hold.

    Pointed at endpoints that do not exist, so a search that reached the
    network would fail rather than quietly pass.
    """
    offline = OpenStreetMapAdapter(endpoints=("http://127.0.0.1:1/nope",))
    datasets = offline.search(SUDBURY)
    assert datasets
    assert offline.is_usable() is True


def test_search_filters_by_data_type(adapter):
    datasets = adapter.search(SUDBURY, (DataType.ROADS,))
    assert [dataset.identifier for dataset in datasets] == ["osm_roads"]


def test_search_reports_the_tile_count_before_committing(adapter):
    """REQ-4d's confirmation is only a decision if it says what it will cost."""
    small = adapter.search(SUDBURY, (DataType.ROADS,))[0]
    assert small.extra["overpass_tiles"] == 1
    assert "tiling_note" not in small.extra

    large = adapter.search((0.0, 50.0, 1.0, 51.0), (DataType.ROADS,))[0]
    assert large.extra["overpass_tiles"] > 1
    assert "merged" in large.extra["tiling_note"]


def test_search_carries_the_share_alike_obligation(adapter):
    """ODbL is not "open, do as you like", and the difference is the point."""
    dataset = adapter.search(SUDBURY, (DataType.ROADS,))[0]
    licence = dataset.licence.to_dict()
    assert "share" in licence["summary"].lower()
    assert "OpenStreetMap" in licence["attribution"]
    assert "Produced Work" in licence["commercial_note"]


def test_search_estimates_a_size_that_scales_with_area(adapter):
    small = adapter.search(SUDBURY, (DataType.BUILDINGS,))[0]
    bigger = adapter.search((0.0, 50.0, 0.4, 50.4), (DataType.BUILDINGS,))[0]
    assert bigger.estimated_size_bytes > small.estimated_size_bytes


def test_buildings_are_estimated_denser_than_roads(adapter):
    """The estimate is rough, but it must get the ordering right."""
    datasets = {d.data_type: d for d in adapter.search(SUDBURY)}
    assert (
        datasets[DataType.BUILDINGS].estimated_size_bytes
        > datasets[DataType.ROADS].estimated_size_bytes
    )


def test_an_unfetchable_aoi_says_so_in_the_search(adapter):
    """Better to learn at menu time than after confirming a download."""
    datasets = adapter.search((-10.0, 35.0, 30.0, 60.0), (DataType.ROADS,))
    assert "planet extract" in datasets[0].extra["unavailable_reason"]


def test_fetch_rejects_an_unknown_theme(adapter, tmp_path):
    from qgis_mcp_server.providers.base import Dataset
    from qgis_mcp_server.providers.catalogue import ODBL_LICENCE

    bogus = Dataset(
        provider="openstreetmap",
        identifier="osm_unicorns",
        title="?",
        data_type=DataType.ROADS,
        licence=ODBL_LICENCE,
    )
    with pytest.raises(OverpassError) as caught:
        adapter.fetch(bogus, SUDBURY, tmp_path / "out.geojson")
    assert "osm_roads" in str(caught.value)


def test_every_mirror_failing_is_reported_with_each_reason(adapter, tmp_path):
    """Which mirrors were tried, and what each said.

    "Overpass failed" sends someone to check their network; "all three said
    504" tells them to wait and retry.
    """
    offline = OpenStreetMapAdapter(
        endpoints=("http://127.0.0.1:1/a", "http://127.0.0.1:1/b"),
        socket_timeout=2.0,
    )
    dataset = offline.search(SUDBURY, (DataType.ROADS,))[0]
    with pytest.raises(OverpassError) as caught:
        offline.fetch(dataset, SUDBURY, tmp_path / "out.geojson")
    message = str(caught.value)
    assert message.count("127.0.0.1:1") == 2, message
    assert "retrying" in message


def test_a_working_mirror_is_tried_first_next_time():
    """Rediscovering a dead mirror once per tile is the expensive mistake.

    A saturated instance takes seconds to answer 504 and tens of seconds to
    time out. Paying that on every tile of a sixteen-tile fetch costs more than
    the queries themselves, so the first success is remembered.
    """
    adapter = OpenStreetMapAdapter(
        endpoints=("https://a.invalid/x", "https://b.invalid/x", "https://c.invalid/x")
    )
    assert adapter._ordered_endpoints()[0] == "https://a.invalid/x"

    adapter.last_endpoint = "https://c.invalid/x"
    order = adapter._ordered_endpoints()
    assert order[0] == "https://c.invalid/x"
    # The others are still there, so a mirror that later fails falls through.
    assert set(order) == set(adapter.endpoints)


def test_an_unknown_last_endpoint_does_not_corrupt_the_order():
    """A stale value from a reconfigured adapter must not inject a bad URL."""
    adapter = OpenStreetMapAdapter(endpoints=("https://a.invalid/x",))
    adapter.last_endpoint = "https://gone.invalid/x"
    assert adapter._ordered_endpoints() == ("https://a.invalid/x",)


def test_the_mirror_list_has_no_duplicates():
    from qgis_mcp_server.providers.openstreetmap import ENDPOINTS

    assert len(set(ENDPOINTS)) == len(ENDPOINTS)


def test_a_working_mirror_is_tried_first_next_time():
    """Rediscovering a dead mirror once per tile is the expensive mistake.

    A saturated instance takes seconds to answer 504 and tens of seconds to
    time out. Paying that on every tile of a sixteen-tile fetch costs more than
    the queries themselves, so the first success is remembered.
    """
    adapter = OpenStreetMapAdapter(
        endpoints=("https://a.invalid/x", "https://b.invalid/x", "https://c.invalid/x")
    )
    assert adapter._ordered_endpoints()[0] == "https://a.invalid/x"

    adapter.last_endpoint = "https://c.invalid/x"
    order = adapter._ordered_endpoints()
    assert order[0] == "https://c.invalid/x"
    # The others are still there, so a mirror that later fails falls through.
    assert set(order) == set(adapter.endpoints)


def test_an_unknown_last_endpoint_does_not_corrupt_the_order():
    """A stale value from a reconfigured adapter must not inject a bad URL."""
    adapter = OpenStreetMapAdapter(endpoints=("https://a.invalid/x",))
    adapter.last_endpoint = "https://gone.invalid/x"
    assert adapter._ordered_endpoints() == ("https://a.invalid/x",)


def test_the_mirror_list_has_no_duplicates():
    from qgis_mcp_server.providers.openstreetmap import ENDPOINTS

    assert len(set(ENDPOINTS)) == len(ENDPOINTS)


# ---------------------------------------------------------------------------
# Live Overpass (integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_fetch_of_a_small_area(tmp_path):
    """One real query, against a deliberately tiny AOI.

    Excluded from the default pass: Overpass is a free shared service and the
    suite should not query it on every run. Kept because the conversion above
    is tested against hand-made elements, and only a real response proves the
    shapes this adapter expects are the shapes Overpass sends.
    """
    adapter = OpenStreetMapAdapter()
    # A few streets in the middle of Sudbury: small enough to be polite.
    aoi = (0.7260, 52.0370, 0.7330, 52.0420)
    dataset = adapter.search(aoi, (DataType.ROADS,))[0]

    destination = adapter.fetch(dataset, aoi, tmp_path / "roads.geojson")
    collection = json.loads(destination.read_text(encoding="utf-8"))

    assert collection["type"] == "FeatureCollection"
    assert collection["features"], "central Sudbury should have roads"
    assert collection["source"]["tiles"] == 1
    assert collection["source"]["endpoint"]
    assert "OpenStreetMap" in collection["attribution"]

    highways = {
        feature["properties"].get("highway")
        for feature in collection["features"]
    }
    assert highways - {None}, "every feature should carry a highway tag"
