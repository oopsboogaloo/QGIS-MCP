"""Task 4.8: the Natural Earth static-catalogue adapter.

Nothing here needs a network, because nothing in the adapter does until
``fetch``. The judgement worth testing is the scale choice and the honesty
about what Natural Earth cannot do: it has no bbox filtering, so a town-sized
AOI downloads the whole world and gets linework too coarse to mean anything at
that size. A menu entry that did not say so would be offering a wrong answer
politely.
"""

from __future__ import annotations

import pytest

from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.providers.natural_earth import (
    SCALE_DENOMINATOR,
    THEMES,
    NaturalEarthAdapter,
    NaturalEarthError,
    recommend_scale,
)

SUDBURY = (0.6899, 52.0002, 0.7699, 52.0802)   # 0.08 degrees
BRITAIN = (-8.0, 49.9, 2.0, 61.0)              # 11.1 degrees
MEDITERRANEAN = (-10.0, 30.0, 36.0, 47.0)      # 46 degrees
EUROPE = (-25.0, 34.0, 45.0, 72.0)             # 70 degrees


@pytest.fixture
def adapter():
    return NaturalEarthAdapter()


# ---------------------------------------------------------------------------
# Scale selection
# ---------------------------------------------------------------------------


def test_scale_follows_how_much_of_the_world_is_in_view():
    """Natural Earth's scales are about extent, not about detail wanted.

    A country still takes the most detailed scale: 1:10m is what Natural Earth
    intends for a map of Britain. The coarser sets are for when a continent or
    the globe is in view, where 1:10m is both unnecessary and large.
    """
    assert recommend_scale(SUDBURY) == "10m"
    assert recommend_scale(BRITAIN) == "10m"
    assert recommend_scale(MEDITERRANEAN) == "50m"
    assert recommend_scale(EUROPE) == "110m"


def test_a_continental_aoi_does_not_fetch_the_detailed_file():
    """Nine megabytes of world roads to draw a backdrop behind Europe."""
    adapter = NaturalEarthAdapter()
    datasets = {d.data_type: d for d in adapter.search(EUROPE)}
    coastline = datasets[DataType.COASTLINE]
    assert coastline.extra["scale"] == "110m"
    assert coastline.estimated_size_bytes < 500_000


def test_a_theme_published_at_one_scale_only_says_so():
    """Roads exist at 10m alone; asking for 110m returns a 404, not a coarser file."""
    adapter = NaturalEarthAdapter()
    roads = next(
        d for d in adapter.search(EUROPE, (DataType.ROADS,))
    )
    assert roads.extra["scale"] == "10m"
    assert "published at 10m only" in roads.extra["scale_note"]


# ---------------------------------------------------------------------------
# Honesty about what it cannot do
# ---------------------------------------------------------------------------


def test_a_town_sized_aoi_is_warned_that_this_is_the_wrong_source():
    """The useful answer for Sudbury is "not this provider".

    Natural Earth will cheerfully serve a coastline for a 5 km box. It will
    have vertices hundreds of metres apart and visibly miss the coast. Saying
    so is worth more than the data.
    """
    adapter = NaturalEarthAdapter()
    dataset = adapter.search(SUDBURY, (DataType.COASTLINE,))[0]
    warning = dataset.extra["too_coarse_warning"]
    assert "OpenStreetMap" in warning, warning
    assert "1:10,000,000" in warning, warning


def test_a_country_sized_aoi_is_not_warned(adapter):
    dataset = adapter.search(BRITAIN, (DataType.COASTLINE,))[0]
    assert "too_coarse_warning" not in dataset.extra


def test_every_dataset_says_it_has_no_bbox_filtering(adapter):
    """The defining property of a static catalogue, stated per dataset.

    It changes what the caller must do next --- clip locally --- so it belongs
    on the dataset rather than only in the provider's notes.
    """
    for dataset in adapter.search(BRITAIN):
        assert dataset.extra["whole_world"] is True
        assert "clip_to_aoi" in dataset.extra["clip_required"]


def test_natural_earth_ranks_below_providers_with_local_data(adapter):
    """It covers everywhere, which would otherwise make it win everywhere.

    The dispatcher sorts on preference, and the leading entry in a menu is the
    one that gets taken.
    """
    for dataset in adapter.search(BRITAIN):
        assert dataset.preference >= 5


def test_it_needs_no_credentials(adapter):
    assert adapter.is_usable() is True
    assert adapter.credential_status() is None


def test_public_domain_means_no_attribution_required(adapter):
    dataset = adapter.search(BRITAIN, (DataType.COASTLINE,))[0]
    assert dataset.attribution == ""
    assert dataset.licence.to_dict()["needs_commercial_check"] is False


# ---------------------------------------------------------------------------
# URLs and identifiers
# ---------------------------------------------------------------------------


def test_urls_follow_the_published_cdn_pattern(adapter):
    dataset = adapter.search(BRITAIN, (DataType.COASTLINE,))[0]
    assert dataset.extra["url"] == (
        "https://naciscdn.org/naturalearth/10m/physical/ne_10m_coastline.zip"
    )


def test_cultural_and_physical_themes_use_their_own_directories():
    """The category is part of the path, and the wrong one is a 404."""
    by_slug = {theme.slug: theme for theme in THEMES}
    assert "/cultural/" in by_slug["populated_places"].url("10m")
    assert "/physical/" in by_slug["coastline"].url("10m")


def test_identifiers_carry_the_scale():
    """ne_10m_coastline and ne_110m_coastline are different products.

    An identifier without the scale would make them collide in the download
    cache, where the path is derived from provider, identifier and AOI.
    """
    coastline = next(theme for theme in THEMES if theme.slug == "coastline")
    assert coastline.identifier("10m") != coastline.identifier("110m")


def test_the_format_is_declared_so_the_pipeline_names_the_file_correctly():
    """A zipped shapefile written to a path ending .tif is a lie on disk.

    The acquisition pipeline derives the extension from this field; before task
    4.8 it was hardcoded to .tif, which was correct only while every adapter
    served GeoTIFF.
    """
    from qgis_mcp_server.acquisition import is_vector, suffix_for

    dataset = NaturalEarthAdapter().search(BRITAIN, (DataType.COASTLINE,))[0]
    assert dataset.format == "SHP-ZIP"
    assert suffix_for(dataset) == ".zip"
    assert is_vector(dataset) is True


def test_an_unknown_identifier_lists_what_exists(tmp_path, adapter):
    from qgis_mcp_server.providers.base import Dataset
    from qgis_mcp_server.providers.catalogue import PUBLIC_DOMAIN_LICENCE

    bogus = Dataset(
        provider="natural_earth",
        identifier="ne_10m_unicorns",
        title="?",
        data_type=DataType.COASTLINE,
        licence=PUBLIC_DOMAIN_LICENCE,
    )
    with pytest.raises(NaturalEarthError) as caught:
        adapter.fetch(bogus, BRITAIN, tmp_path / "out.zip")
    assert "ne_10m_coastline" in str(caught.value)


# ---------------------------------------------------------------------------
# Live download (integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_download_of_the_smallest_product(tmp_path):
    """The 110m coastline: about 85 KB, the cheapest real check available."""
    adapter = NaturalEarthAdapter()
    dataset = adapter.search(EUROPE, (DataType.COASTLINE,))[0]
    assert dataset.extra["scale"] == "110m"

    destination = adapter.fetch(dataset, EUROPE, tmp_path / "coastline.zip")
    assert destination.exists()
    assert destination.stat().st_size > 10_000
    assert destination.read_bytes()[:2] == b"PK", "should be a zip archive"
