"""Environment Agency LIDAR: the sixth provider.

Free and unmetered, which removes the usual brake and leaves a worse one. At
4 MB per square kilometre the thing most likely to go wrong is not a quota
error but a download nobody intended — so most of what is tested here is the
sizing arithmetic and the refusal.

The 4 MB/km² figure is measured, not assumed: a 1 km square over Sudbury came
back as 4,194,755 bytes of Float32, which is four bytes a cell plus a header.
"""

from __future__ import annotations

import pytest

from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.providers.environment_agency import (
    MAX_DOWNLOAD_BYTES,
    OUTPUT_CRS,
    NATIVE_RESOLUTION_M,
    RESOLUTIONS_M,
    SUBSETTING_CRS,
    VERTICAL_DATUM,
    EaLidarError,
    EnvironmentAgencyAdapter,
    aoi_bng_span,
    choose_resolution,
    estimate_bytes,
)

SUDBURY = (0.6899, 52.0002, 0.7699, 52.0802)      # ~48 km2
TOWN_CENTRE = (0.7260, 52.0370, 0.7330, 52.0420)  # ~0.3 km2
#: A genuine 1 km square at Sudbury's latitude, which is what makes the
#: calibration below meaningful: 1000/110570 degrees of latitude, and
#: 1000/(111320*cos 52) of longitude. Eyeballing a "roughly square" box in
#: degrees at 52 N gives something two-and-a-half times longer than it is wide.
ONE_KM = (0.729900, 52.040200, 0.744490, 52.049244)
SCOTLAND = (-4.5, 56.5, -4.0, 57.0)
WHOLE_OF_ENGLAND = (-6.0, 50.0, 1.8, 55.5)


@pytest.fixture
def adapter():
    return EnvironmentAgencyAdapter()


# ---------------------------------------------------------------------------
# Sizing — the thing most likely to cost someone an afternoon
# ---------------------------------------------------------------------------


def test_the_size_estimate_matches_what_the_service_actually_sent():
    """Calibration against a real response, not a plausible constant.

    A 1 km square at 1 m is a million Float32 cells: 4 MB. The live request
    returned 4,194,755 bytes, so the estimate should land within a few percent
    — close enough that the refusal threshold means what it says.
    """
    estimated = estimate_bytes(ONE_KM, 1.0)
    assert 3_500_000 < estimated < 4_800_000, estimated


def test_a_coarser_resolution_shrinks_quadratically():
    """Halving the resolution quarters the download, which is the whole lever."""
    fine = estimate_bytes(SUDBURY, 1.0)
    coarse = estimate_bytes(SUDBURY, 2.0)
    assert coarse == pytest.approx(fine / 4, rel=0.01)


def test_a_small_aoi_gets_native_resolution(adapter):
    """The common case must not be degraded to protect against the rare one."""
    assert choose_resolution(TOWN_CENTRE) == NATIVE_RESOLUTION_M


def test_a_large_aoi_is_coarsened_rather_than_refused():
    """A county at 1 m is gigabytes; at 10 m it is useful and reasonable.

    Coarsening is server-side, so this is a smaller download rather than a
    large one thrown away locally.
    """
    county = (-1.0, 51.0, 0.0, 51.8)
    chosen = choose_resolution(county)
    assert chosen is not None
    assert chosen > NATIVE_RESOLUTION_M
    assert estimate_bytes(county, chosen) <= MAX_DOWNLOAD_BYTES


def test_an_absurd_aoi_has_no_usable_resolution():
    """All of England, even at 25 m, is past the limit."""
    assert choose_resolution(WHOLE_OF_ENGLAND) is None


def test_the_area_is_corrected_for_latitude():
    """A degree of longitude is 111 km at the equator and 69 km at Sudbury."""
    width, _ = aoi_bng_span((0.0, 52.0, 1.0, 53.0))
    assert 65_000 < width < 72_000, width


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_needs_no_network_and_no_credentials(adapter):
    offline = EnvironmentAgencyAdapter(base_url="http://127.0.0.1:1/nope")
    assert offline.search(TOWN_CENTRE)
    assert offline.is_usable() is True


def test_search_outside_england_returns_nothing(adapter):
    """Scotland is the Scottish Remote Sensing Portal, a different service."""
    assert adapter.search(SCOTLAND) == []


def test_search_only_answers_for_elevation(adapter):
    assert adapter.search(TOWN_CENTRE, (DataType.ROADS,)) == []
    assert adapter.search(TOWN_CENTRE, (DataType.ELEVATION,))


def test_it_is_served_natively_in_british_national_grid(adapter):
    """Half the reason to prefer it for British work.

    The declared CRS has to match what the service is asked to return, or the
    pipeline reprojects a file that was already in the right projection. Note
    the grid is still fitted to the requested box rather than handed over
    untouched — see OUTPUT_CRS — so this is "no datum transform", not "no
    resampling".
    """
    for dataset in adapter.search(TOWN_CENTRE):
        assert dataset.crs == "EPSG:27700"


def test_lidar_outranks_the_global_dems_in_england(adapter):
    """1 m of surveyed ground against 30 m of modelled satellite.

    The dispatcher sorts on preference and the leading entry is what gets
    taken, so this is the difference between a town map built from LIDAR and
    one built from COP30.
    """
    elevation = next(
        d for d in adapter.search(TOWN_CENTRE) if d.identifier == "ea_lidar_dtm_1m"
    )
    assert elevation.preference == 0


def test_the_hillshade_ranks_below_the_elevation(adapter):
    """It is a picture. Taken as a heightmap it produces nonsense silently."""
    by_id = {d.identifier: d for d in adapter.search(TOWN_CENTRE)}
    assert by_id["ea_lidar_hillshade_1m"].preference > by_id["ea_lidar_dtm_1m"].preference
    assert "useless as a heightmap" in by_id["ea_lidar_hillshade_1m"].extra["notes"]


def test_the_vertical_datum_is_recorded_and_the_mismatch_warned(adapter):
    """REQ-13. ODN against EGM2008 is an offset that looks like noise.

    Up to about a metre, varying across the country — small enough to dismiss,
    large enough to matter in anything modelling water.
    """
    elevation = next(
        d for d in adapter.search(TOWN_CENTRE) if d.identifier == "ea_lidar_dtm_1m"
    )
    assert elevation.extra["vertical_datum"] == VERTICAL_DATUM
    warning = elevation.extra["vertical_datum_warning"]
    assert "EGM2008" in warning
    assert "COP30" in warning


def test_coverage_is_described_as_an_envelope_with_holes(adapter):
    """Surveys were flown where they were needed.

    An AOI inside England can legitimately return nothing, and that is a
    coverage gap rather than a failure — the two call for different responses.
    """
    for dataset in adapter.search(TOWN_CENTRE):
        assert "holes" in dataset.extra["coverage_caveat"]


def test_a_coarsened_search_explains_what_it_would_have_cost(adapter):
    county = (-1.0, 51.0, 0.0, 51.8)
    dataset = adapter.search(county)[0]
    note = dataset.extra["resolution_note"]
    assert "native" in note
    assert "MB" in note


def test_an_oversized_search_says_so_before_anything_is_confirmed(adapter):
    dataset = adapter.search(WHOLE_OF_ENGLAND)[0]
    assert "over the" in dataset.extra["too_large"]
    assert "global DEM" in dataset.extra["too_large"]


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


class RecordingAdapter(EnvironmentAgencyAdapter):
    def __init__(self, body=b"MM\x00*" + b"\x00" * 300, **kwargs):
        super().__init__(**kwargs)
        self._body = body
        self.requested: list[str] = []

    def _fake_open(self, url):
        self.requested.append(url)
        return self._body


@pytest.fixture
def recording(monkeypatch):
    adapter = RecordingAdapter()

    class Response:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    import urllib.request as request_module

    def fake_urlopen(request, timeout=None):
        adapter.requested.append(request.full_url)
        return Response(adapter._body)

    monkeypatch.setattr(request_module, "urlopen", fake_urlopen)
    return adapter


def test_the_aoi_is_sent_in_4326_with_named_axes(recording, tmp_path):
    """No local transform, and no axis order to get backwards.

    Converting to British National Grid here would mean a PROJ transform in
    the server process, which has no pyproj by design. Named Long/Lat axes
    also remove the positional-order trap that WMS 1.3.0's EPSG:4326 bbox has.
    """
    dataset = recording.search(TOWN_CENTRE)[0]
    recording.fetch(dataset, TOWN_CENTRE, tmp_path / "dtm.tif")
    url = recording.requested[-1]

    assert "subsettingCrs=" in url
    assert SUBSETTING_CRS.split("/")[-1] == "4326"
    assert "subset=Long(0.726,0.733)" in url, url
    assert "subset=Lat(52.037,52.042)" in url, url


def test_the_output_crs_is_requested_explicitly(recording, tmp_path):
    """Without it, subsetting in 4326 returns the coverage in 4326 too.

    That silently throws away the main reason to prefer this provider for
    British work --- and it is invisible until somebody opens the file and
    checks its projection, which is exactly how it was found.
    """
    dataset = recording.search(TOWN_CENTRE)[0]
    recording.fetch(dataset, TOWN_CENTRE, tmp_path / "dtm.tif")
    url = recording.requested[-1]
    assert "outputCrs=" in url, url
    assert OUTPUT_CRS.endswith("27700")


def test_coarsening_uses_scalefactor_not_scaleaxes(recording, tmp_path):
    """The service answers HTTP 500 to scaleaxes and scalesize.

    Both are in the WCS scaling extension and both are well-formed here, so
    this is the service's limitation rather than a malformed request --- found
    by trying each. scalefactor also applies to every axis at once, which is
    what is wanted: anisotropic cells would make every slope and hillshade
    wrong.
    """
    dataset = recording.search(TOWN_CENTRE)[0]
    dataset.extra["resolution_m"] = 2.0
    recording.fetch(dataset, TOWN_CENTRE, tmp_path / "dtm.tif")
    url = recording.requested[-1]
    assert "scalefactor=0.5" in url, url
    assert "scaleaxes" not in url
    assert "scalesize" not in url


def test_a_native_resolution_request_sends_no_scaling(recording, tmp_path):
    """Asking for 1:1 scaling is a parameter that can only go wrong."""
    dataset = recording.search(TOWN_CENTRE)[0]
    dataset.extra["resolution_m"] = 1.0
    recording.fetch(dataset, TOWN_CENTRE, tmp_path / "dtm.tif")
    assert "scalefactor" not in recording.requested[-1]


def test_a_fetch_over_the_limit_is_refused_before_sending(recording, tmp_path):
    """Nothing half-downloaded, and the two remedies named."""
    dataset = recording.search(TOWN_CENTRE)[0]
    dataset.extra["resolution_m"] = 1.0

    small = EnvironmentAgencyAdapter(max_download_bytes=1000)
    with pytest.raises(EaLidarError) as caught:
        small.fetch(dataset, SUDBURY, tmp_path / "dtm.tif")
    message = str(caught.value)
    assert "Refused before sending" in message
    assert "coarser resolution" in message
    assert not (tmp_path / "dtm.tif").exists()


def test_a_non_tiff_response_is_not_written(recording, tmp_path):
    """WCS reports failures as XML, often inside a 200.

    The usual cause is an AOI outside the surveyed area, which the message
    says — rather than leaving a .tif that GDAL rejects somewhere later.
    """
    recording._body = b'<?xml version="1.0"?><ExceptionReport>no data</ExceptionReport>'
    dataset = recording.search(TOWN_CENTRE)[0]
    with pytest.raises(EaLidarError) as caught:
        recording.fetch(dataset, TOWN_CENTRE, tmp_path / "dtm.tif")
    assert "outside the surveyed area" in str(caught.value)
    assert not (tmp_path / "dtm.tif").exists()


def test_an_unknown_product_lists_the_real_ones(recording, tmp_path):
    from qgis_mcp_server.providers.base import Dataset
    from qgis_mcp_server.providers.catalogue import EA_LIDAR_LICENCE

    bogus = Dataset(
        provider="environment_agency",
        identifier="ea_lidar_unicorn",
        title="?",
        data_type=DataType.ELEVATION,
        licence=EA_LIDAR_LICENCE,
    )
    with pytest.raises(EaLidarError) as caught:
        recording.fetch(bogus, TOWN_CENTRE, tmp_path / "x.tif")
    assert "ea_lidar_dtm_1m" in str(caught.value)


def test_the_format_keeps_the_raster_path():
    from qgis_mcp_server.acquisition import is_vector, suffix_for

    dataset = EnvironmentAgencyAdapter().search(TOWN_CENTRE)[0]
    assert dataset.format == "GTiff"
    assert suffix_for(dataset) == ".tif"
    assert is_vector(dataset) is False


# ---------------------------------------------------------------------------
# Live (integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_fetch_over_sudbury(tmp_path):
    """One real square kilometre, which is about 4 MB.

    Free and unmetered, but still someone's server — and this is the only
    check that the shapes this adapter expects are the ones it sends.
    """
    adapter = EnvironmentAgencyAdapter()
    dataset = next(
        d for d in adapter.search(ONE_KM) if d.identifier == "ea_lidar_dtm_1m"
    )
    destination = adapter.fetch(dataset, ONE_KM, tmp_path / "dtm.tif")

    body = destination.read_bytes()
    assert body[:2] in (b"II", b"MM"), "should be a TIFF"
    assert len(body) > 500_000, f"only {len(body)} bytes; expected roughly 4 MB"
