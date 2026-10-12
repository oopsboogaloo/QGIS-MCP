"""Task 4.8: the BGS WMS adapter.

Two things here would be wrong in ways nothing downstream detects: the WMS
1.3.0 axis-order trap, which returns a valid image of the wrong place, and the
world file, which lands a valid raster in the wrong position. Both are pure
functions and both are asserted against arithmetic rather than against a
recorded output, so a change to either has to be deliberate.
"""

from __future__ import annotations

import pytest

from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.providers.bgs import (
    REQUEST_CRS,
    BgsAdapter,
    BgsError,
    BgsLayer,
    parse_capabilities,
    pixel_size,
    world_file,
)

SUDBURY = (0.6899, 52.0002, 0.7699, 52.0802)
ONTARIO = (-81.6, 46.2, -80.5, 47.0)

#: Trimmed from the live service's GetCapabilities.
CAPABILITIES = b"""<?xml version='1.0' encoding="UTF-8" standalone="no" ?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Service>
    <Name>WMS</Name>
    <Title>BGS Bedrock and Superficial geology</Title>
    <Fees>none</Fees>
    <AccessConstraints>The 1:625k DiGMap data is made available for all uses -
      including commercial use, however the British Geological Survey (BGS) at
      all times retains the copyright in this material.</AccessConstraints>
  </Service>
  <Capability>
    <Layer>
      <Title>Grouping layer with no name</Title>
      <Layer queryable="1">
        <Name>GBR_BGS_625k_BLT</Name>
        <Title>GBR BGS 1:625k Bedrock Lithology</Title>
        <Abstract>Bedrock lithology at 1:625 000 scale.</Abstract>
      </Layer>
      <Layer queryable="1">
        <Name>GBR_BGS_625k_BA</Name>
        <Title>GBR BGS 1:625k Bedrock Age</Title>
      </Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""


class FakeAdapter(BgsAdapter):
    """A BGS adapter with capabilities pre-loaded and HTTP accounted for."""

    def __init__(self, image: bytes = b"II*\x00" + b"\x00" * 200, **kwargs):
        super().__init__(**kwargs)
        self._layers, self._terms = parse_capabilities(CAPABILITIES)
        self._image = image
        self.requested: list[str] = []

    def _get(self, url):
        self.requested.append(url)
        return self._image


# ---------------------------------------------------------------------------
# Capabilities parsing
# ---------------------------------------------------------------------------


def test_named_layers_are_found_and_group_layers_skipped():
    """A grouping layer has no Name and cannot be requested.

    Offering it would produce a GetMap for a layer the server does not know,
    which comes back as an XML error inside a 200.
    """
    layers, _ = parse_capabilities(CAPABILITIES)
    assert [layer.name for layer in layers] == [
        "GBR_BGS_625k_BLT",
        "GBR_BGS_625k_BA",
    ]


def test_the_licence_terms_come_from_the_service_not_a_constant():
    """BGS's free tier is bounded by scale, so a stale copy is the worst kind.

    Read from the live capabilities, the statement cannot drift out of date
    here without the service changing it.
    """
    _, terms = parse_capabilities(CAPABILITIES)
    assert "including commercial use" in terms["access_constraints"]
    assert terms["fees"] == "none"


def test_unparseable_capabilities_say_so():
    with pytest.raises(BgsError) as caught:
        parse_capabilities(b"<not xml")
    assert "would not parse" in str(caught.value)


# ---------------------------------------------------------------------------
# The axis-order trap
# ---------------------------------------------------------------------------


def test_the_request_uses_crs84_not_epsg4326(tmp_path):
    """In WMS 1.3.0, EPSG:4326 is latitude-first and CRS:84 is longitude-first.

    Both are advertised by this service. Choosing EPSG:4326 with a lon/lat
    bbox returns a valid image of a different place --- no error, no warning,
    and the geology simply does not match the ground.
    """
    assert REQUEST_CRS == "CRS:84"

    adapter = FakeAdapter()
    dataset = adapter.search(SUDBURY)[0]
    adapter.fetch(dataset, SUDBURY, tmp_path / "x.tif")
    request = adapter.requested[-1]
    assert "crs=CRS%3A84" in request, request
    assert "bbox=0.6899%2C52.0002%2C0.7699%2C52.0802" in request, request


# ---------------------------------------------------------------------------
# Image size
# ---------------------------------------------------------------------------


def test_the_image_keeps_the_aoi_aspect_ratio():
    """A square image of a wide AOI is stretched geology that looks plausible.

    The same mistake in the other direction produced a 12995x13000 raster
    earlier in this project, from a transposed extent string.
    """
    width, height = pixel_size((0.0, 50.0, 2.0, 50.5))  # 4:1
    assert width / height == pytest.approx(4.0, rel=0.02)


def test_a_tall_aoi_is_taller_than_it_is_wide():
    width, height = pixel_size((0.0, 50.0, 0.5, 52.0))  # 1:4
    assert height > width


def test_a_very_thin_aoi_still_gets_a_usable_image():
    """Rounding a tiny dimension to zero would make the request invalid."""
    width, height = pixel_size((0.0, 50.0, 10.0, 50.001))
    assert height >= 64
    assert width >= height


def test_an_empty_aoi_is_refused():
    with pytest.raises(BgsError):
        pixel_size((1.0, 50.0, 1.0, 50.0))


# ---------------------------------------------------------------------------
# Georeferencing
# ---------------------------------------------------------------------------


def test_the_world_file_has_a_negative_y_size():
    """Image rows run south while the coordinate runs north.

    A positive y size flips the raster vertically --- which for geology is not
    obviously wrong on screen, and is wrong everywhere.
    """
    lines = world_file((0.0, 50.0, 1.0, 51.0), 100, 100).splitlines()
    assert float(lines[0]) > 0
    assert float(lines[3]) < 0


def test_the_world_file_references_the_centre_of_the_top_left_pixel():
    """The format's own convention, and the usual half-pixel error.

    For a 1-degree AOI at 100 pixels, the top-left pixel centre is half a
    pixel in and half a pixel down from the corner.
    """
    lines = world_file((0.0, 50.0, 1.0, 51.0), 100, 100).splitlines()
    x_size, y_size = float(lines[0]), float(lines[3])
    assert float(lines[4]) == pytest.approx(0.0 + x_size / 2)
    assert float(lines[5]) == pytest.approx(51.0 + y_size / 2)


def test_the_world_file_spans_exactly_the_aoi():
    width = height = 200
    aoi = (0.6899, 52.0002, 0.7699, 52.0802)
    lines = world_file(aoi, width, height).splitlines()
    x_size, y_size = float(lines[0]), float(lines[3])
    assert x_size * width == pytest.approx(aoi[2] - aoi[0])
    assert -y_size * height == pytest.approx(aoi[3] - aoi[1])


# ---------------------------------------------------------------------------
# search and fetch
# ---------------------------------------------------------------------------


def test_search_outside_the_uk_returns_nothing():
    adapter = FakeAdapter()
    assert adapter.search(ONTARIO) == []


def test_search_only_answers_for_geology():
    adapter = FakeAdapter()
    assert adapter.search(SUDBURY, (DataType.ROADS,)) == []
    assert adapter.search(SUDBURY, (DataType.GEOLOGY,))


def test_every_dataset_warns_that_this_is_an_image_not_features():
    """A caller expecting to query rock type by attribute needs telling."""
    for dataset in FakeAdapter().search(SUDBURY):
        assert "not features" in dataset.extra["raster_only"]
        assert "1:50,000" in dataset.extra["scale_note"]


def test_the_commercial_tier_is_not_offered():
    """Handing over licensed data under an open banner is worse than nothing."""
    for dataset in FakeAdapter().search(SUDBURY):
        assert "625k" in dataset.identifier


def test_the_format_is_raster_so_the_pipeline_keeps_the_tif_path():
    from qgis_mcp_server.acquisition import is_vector, suffix_for

    dataset = FakeAdapter().search(SUDBURY)[0]
    assert dataset.format == "GTiff"
    assert suffix_for(dataset) == ".tif"
    assert is_vector(dataset) is False


def test_fetch_writes_the_world_and_projection_files(tmp_path):
    adapter = FakeAdapter()
    dataset = adapter.search(SUDBURY)[0]
    destination = adapter.fetch(dataset, SUDBURY, tmp_path / "geology.tif")

    assert destination.exists()
    assert destination.with_suffix(".tfw").exists()
    assert destination.with_suffix(".prj").exists()
    assert "WGS 84" in destination.with_suffix(".prj").read_text()


def test_an_error_document_is_not_written_as_an_image(tmp_path):
    """WMS reports failures as a 200 with an XML body.

    Written to a .tif, it fails much later with a message about the file
    rather than about the request.
    """
    adapter = FakeAdapter(image=b'<?xml version="1.0"?><ServiceException>bad bbox</ServiceException>')
    dataset = adapter.search(SUDBURY)[0]
    with pytest.raises(BgsError) as caught:
        adapter.fetch(dataset, SUDBURY, tmp_path / "geology.tif")
    assert "bad bbox" in str(caught.value)
    assert not (tmp_path / "geology.tif").exists()


def test_an_unknown_layer_lists_the_real_ones(tmp_path):
    from qgis_mcp_server.providers.base import Dataset
    from qgis_mcp_server.providers.catalogue import BGS_LICENCE

    adapter = FakeAdapter()
    bogus = Dataset(
        provider="bgs",
        identifier="GBR_BGS_625k_UNICORN",
        title="?",
        data_type=DataType.GEOLOGY,
        licence=BGS_LICENCE,
    )
    with pytest.raises(BgsError) as caught:
        adapter.fetch(bogus, SUDBURY, tmp_path / "x.tif")
    assert "GBR_BGS_625k_BLT" in str(caught.value)


def test_it_needs_no_credentials():
    assert BgsAdapter().is_usable() is True


# ---------------------------------------------------------------------------
# Live (integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_capabilities_and_a_small_image(tmp_path):
    """BGS is free and unmetered, but it is still someone's server."""
    adapter = BgsAdapter(max_pixels=256)
    layers = adapter.layers()
    assert any("625k" in layer.name for layer in layers), [l.name for l in layers]

    dataset = next(d for d in adapter.search(SUDBURY) if "BLT" in d.identifier)
    destination = adapter.fetch(dataset, SUDBURY, tmp_path / "bedrock.tif")

    assert destination.stat().st_size > 1000
    # A TIFF begins with a byte-order mark: "II" little-endian or "MM" big.
    assert destination.read_bytes()[:2] in (b"II", b"MM")
    assert destination.with_suffix(".tfw").exists()
