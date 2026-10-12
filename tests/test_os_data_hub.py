"""Task 4.8: the OS Data Hub (NGD Features) adapter.

Every request against this provider spends part of a paid monthly allowance,
which shapes the tests as much as the adapter: the default pass makes no
network calls at all. The HTTP layer is exercised against recorded shapes, and
the one live test is marked ``integration`` and deliberately asks for a single
feature.
"""

from __future__ import annotations

import json

import pytest

from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.providers.os_data_hub import (
    ELSEWHERE,
    THEMES,
    OsDataHubAdapter,
    OsDataHubError,
    _next_link,
    aoi_area_km2,
    latest_collection,
)

SUDBURY = (0.6899, 52.0002, 0.7699, 52.0802)
CENTRAL_SUDBURY = (0.7260, 52.0370, 0.7330, 52.0420)
ONTARIO = (-81.6, 46.2, -80.5, 47.0)

#: Collection ids as the live API returned them, trimmed to what is used here.
LIVE_COLLECTION_IDS = [
    "bld-fts-building-1",
    "bld-fts-building-2",
    "bld-fts-building-3",
    "bld-fts-building-4",
    "trn-ntwk-roadlink-1",
    "trn-ntwk-roadlink-5",
    "trn-ntwk-road-1",
    "wtr-fts-water-3",
    "lnd-fts-land-3",
    "gnm-fts-namedpoint-1",
]


class FakeAdapter(OsDataHubAdapter):
    """An adapter with the collection list pre-loaded and HTTP disabled."""

    def __init__(self, pages=None, **kwargs):
        super().__init__(**kwargs)
        self._collection_ids = list(LIVE_COLLECTION_IDS)
        self._pages = list(pages or [])
        self.requested: list[str] = []

    def _key(self):
        return "test-key"

    def _get(self, url):
        self.requested.append(url)
        if not self._pages:
            raise AssertionError(f"unexpected extra request to {url}")
        return self._pages.pop(0)


# ---------------------------------------------------------------------------
# Collection version discovery
# ---------------------------------------------------------------------------


def test_the_latest_collection_version_is_chosen():
    """OS adds versions; a hardcoded one goes stale without anything failing."""
    assert latest_collection(LIVE_COLLECTION_IDS, "trn-ntwk-roadlink") == (
        "trn-ntwk-roadlink-5"
    )
    assert latest_collection(LIVE_COLLECTION_IDS, "bld-fts-building") == (
        "bld-fts-building-4"
    )


def test_versions_are_compared_as_numbers_not_text():
    """v10 beats v9, which string ordering gets backwards.

    OS is already at v5 for road links, so this stops being hypothetical in a
    few years rather than never.
    """
    assert latest_collection(["x-1", "x-9", "x-10"], "x") == "x-10"


def test_a_family_prefix_does_not_match_a_longer_name():
    """``trn-ntwk-road-1`` is a different collection from ``trn-ntwk-roadlink``.

    Matching on prefix alone would let Road v1 masquerade as the latest Road
    Link, and the fetch would return junctions instead of the network.
    """
    assert latest_collection(LIVE_COLLECTION_IDS, "trn-ntwk-road") == (
        "trn-ntwk-road-1"
    )


def test_an_unknown_family_is_none_not_a_guess():
    assert latest_collection(LIVE_COLLECTION_IDS, "zzz-fts-unicorn") is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_outside_the_uk_returns_nothing(monkeypatch):
    """Coverage is declared, so an Ontario AOI costs no request at all."""
    adapter = FakeAdapter()
    assert adapter.search(ONTARIO) == []
    assert adapter.requested == []


def test_search_offers_the_latest_collection_of_each_theme():
    adapter = FakeAdapter()
    identifiers = {d.data_type: d.identifier for d in adapter.search(SUDBURY)}
    assert identifiers[DataType.ROADS] == "trn-ntwk-roadlink-5"
    assert identifiers[DataType.BUILDINGS] == "bld-fts-building-4"


def test_search_filters_by_data_type():
    adapter = FakeAdapter()
    datasets = adapter.search(SUDBURY, (DataType.BUILDINGS,))
    assert [d.data_type for d in datasets] == [DataType.BUILDINGS]


def test_os_outranks_crowd_sourced_and_global_data_inside_the_uk():
    """Surveyed and consistent, so it should lead the menu where it applies.

    The dispatcher sorts on preference and the leading entry is what gets
    taken, so this is the difference between a UK map built from OS and one
    built from whatever happened to be listed first.
    """
    adapter = FakeAdapter()
    assert all(d.preference == 0 for d in adapter.search(SUDBURY))


def test_search_says_what_a_fetch_will_cost():
    """REQ-4d's confirmation against a *paid* allowance, so the estimate matters."""
    adapter = FakeAdapter()
    buildings = adapter.search(SUDBURY, (DataType.BUILDINGS,))[0]
    extra = buildings.extra
    assert extra["estimated_features"] > 0
    assert extra["estimated_pages"] >= 1
    assert "chargeable transaction" in extra["transaction_note"]
    assert "order of magnitude" in extra["size_estimate_quality"]


def test_buildings_are_estimated_denser_than_named_points():
    adapter = FakeAdapter()
    datasets = {d.data_type: d for d in adapter.search(SUDBURY)}
    assert (
        datasets[DataType.BUILDINGS].estimated_size_bytes
        > datasets[DataType.PLACE_NAMES].estimated_size_bytes
    )


def test_data_types_this_api_cannot_serve_say_where_they_live():
    """OS does have elevation; this API does not.

    Returning nothing for UK elevation would read as "OS has none", which is
    both false and the opposite of useful.
    """
    adapter = FakeAdapter()
    datasets = adapter.search(SUDBURY)
    elsewhere = datasets[0].extra["not_served_here"]
    assert DataType.ELEVATION in elsewhere
    assert "Downloads API" in elsewhere[DataType.ELEVATION]
    assert "OpenTopography" in elsewhere[DataType.ELEVATION]


def test_every_declared_elsewhere_entry_names_an_alternative():
    """A dead end is only useful if it points somewhere."""
    for data_type, reason in ELSEWHERE.items():
        assert len(reason) > 60, data_type
        assert any(
            word in reason
            for word in ("Downloads API", "OpenTopography", "Natural Earth", "XYZ")
        ), (data_type, reason)


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


def test_the_key_is_reattached_to_the_next_link():
    """OS omits the key from its own next link.

    Following it verbatim gets a 401 on page two, which reads as the key
    expiring mid-fetch rather than as a missing parameter.
    """
    payload = {"links": [{"rel": "next", "href": "https://api.os.uk/x?limit=100"}]}
    assert _next_link(payload, "abc") == "https://api.os.uk/x?limit=100&key=abc"


def test_a_next_link_that_already_has_a_key_is_left_alone():
    payload = {"links": [{"rel": "next", "href": "https://api.os.uk/x?key=abc"}]}
    assert _next_link(payload, "abc") == "https://api.os.uk/x?key=abc"


def test_no_next_link_means_the_last_page():
    assert _next_link({"links": [{"rel": "self", "href": "x"}]}, "abc") is None


def test_fetch_follows_pages_and_merges_features(tmp_path):
    adapter = FakeAdapter(
        pages=[
            {
                "features": [{"id": 1}, {"id": 2}],
                "links": [{"rel": "next", "href": "https://api.os.uk/page2"}],
            },
            {"features": [{"id": 3}], "links": []},
        ]
    )
    dataset = adapter.search(SUDBURY, (DataType.ROADS,))[0]
    destination = adapter.fetch(dataset, SUDBURY, tmp_path / "roads.geojson")

    collection = json.loads(destination.read_text(encoding="utf-8"))
    assert len(collection["features"]) == 3
    assert collection["source"]["pages"] == 2
    assert collection["source"]["truncated"] is False


def test_a_truncated_fetch_says_so_rather_than_looking_complete(tmp_path):
    """A partial layer that looks whole is the failure worth preventing.

    The map simply stops having buildings part-way across, and nothing in the
    file says why.
    """
    adapter = FakeAdapter(
        pages=[
            {
                "features": [{"id": 1}],
                "links": [{"rel": "next", "href": "https://api.os.uk/p2"}],
            },
            {
                "features": [{"id": 2}],
                "links": [{"rel": "next", "href": "https://api.os.uk/p3"}],
            },
        ],
        max_pages=2,
    )
    dataset = adapter.search(SUDBURY, (DataType.BUILDINGS,))[0]
    destination = adapter.fetch(dataset, SUDBURY, tmp_path / "b.geojson")

    source = json.loads(destination.read_text(encoding="utf-8"))["source"]
    assert source["truncated"] is True
    assert "chargeable" in source["truncated_note"]
    assert str(2) in source["truncated_note"]


def test_the_bbox_is_sent_in_crs84_order(tmp_path):
    """min_lon,min_lat,max_lon,max_lat -- which for once matches our AOI.

    Four coordinate orders appear in this project; asserting the one that
    agrees is as worthwhile as asserting the ones that do not, because the
    agreement is what makes it easy to transpose by habit.
    """
    adapter = FakeAdapter(pages=[{"features": [], "links": []}])
    dataset = adapter.search(SUDBURY, (DataType.ROADS,))[0]
    adapter.fetch(dataset, SUDBURY, tmp_path / "x.geojson")
    assert "bbox=0.6899%2C52.0002%2C0.7699%2C52.0802" in adapter.requested[0]


# ---------------------------------------------------------------------------
# Credentials and errors
# ---------------------------------------------------------------------------


def test_a_missing_key_names_the_setting_and_the_signup():
    adapter = OsDataHubAdapter()
    adapter.credentials.config_values = {}
    import os as _os

    previous = _os.environ.pop("QGIS_MCP_OS_DATA_HUB_KEY", None)
    try:
        with pytest.raises(OsDataHubError) as caught:
            adapter._key()
    finally:
        if previous is not None:
            _os.environ["QGIS_MCP_OS_DATA_HUB_KEY"] = previous
    message = str(caught.value)
    assert "QGIS_MCP_OS_DATA_HUB_KEY" in message
    assert "osdatahub.os.uk" in message


def test_the_format_declares_vector_so_the_pipeline_names_the_file_right():
    from qgis_mcp_server.acquisition import is_vector, suffix_for

    dataset = FakeAdapter().search(SUDBURY, (DataType.ROADS,))[0]
    assert dataset.format == "GeoJSON"
    assert suffix_for(dataset) == ".geojson"
    assert is_vector(dataset) is True


def test_area_is_corrected_for_latitude():
    """A degree of longitude is 111 km at the equator and 69 km at Sudbury.

    Without the cosine term every UK area estimate is 60% too large, and the
    feature and page estimates with it.
    """
    equator = aoi_area_km2((0.0, 0.0, 1.0, 1.0))
    uk = aoi_area_km2((0.0, 52.0, 1.0, 53.0))
    assert uk < equator * 0.7


# ---------------------------------------------------------------------------
# Live (integration) -- spends the account's allowance
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_collections_and_one_feature(tmp_path):
    """Two chargeable requests: the collection list and a single feature.

    Kept deliberately tiny. The point is to confirm the shapes this adapter
    expects are the shapes OS sends --- the version suffixes, the next link
    without a key, GeoJSON out --- not to download anything useful.
    """
    from qgis_mcp_server.config import Config

    # The configured store, as the server context builds it. An adapter made
    # with no credentials gets an empty store and would fail here for the
    # wrong reason.
    config = Config.load()
    adapter = OsDataHubAdapter(
        config.credential_store, page_size=1, max_pages=1
    )
    if not adapter.is_usable():
        pytest.skip("no OS Data Hub key configured")

    identifiers = adapter.collection_ids()
    assert any(i.startswith("trn-ntwk-roadlink-") for i in identifiers)

    dataset = adapter.search(CENTRAL_SUDBURY, (DataType.ROADS,))[0]
    destination = adapter.fetch(
        dataset, CENTRAL_SUDBURY, tmp_path / "one_road.geojson"
    )
    collection = json.loads(destination.read_text(encoding="utf-8"))

    assert collection["features"], "central Sudbury has roads"
    feature = collection["features"][0]
    assert feature["geometry"]["type"] in {"LineString", "MultiLineString"}
    assert "Crown copyright" in collection["attribution"]
