"""Task 4.4: the OpenTopography adapter.

`tasks.md` asks for "recorded real response committed as a fixture, pinning the
parsing". The fixtures in `test-data/provider-fixtures/opentopography/` are real
responses captured from the live API, and the two **error** bodies are the ones
that pin actual parsing — the successful fetch is raw GeoTIFF bytes.

Every test here is offline. `urlopen` is patched to replay a recorded response,
which exercises the whole fetch path — validation, atomic write, error
translation — against bytes the service really sent. The live call lives behind
an `integration` marker, excluded from the default pass, because the free
allowance is 50 calls per 24 hours and a test that spends one per run would
exhaust an afternoon of iteration.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import assert_ok, describe
from qgis_mcp_server.credentials import CredentialStore
from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.providers.catalogue import OPENTOPOGRAPHY_CREDENTIAL
from qgis_mcp_server.providers.opentopography import (
    DEMS_BY_TYPE,
    GLOBAL_DEMS,
    OpenTopographyAdapter,
    OpenTopographyError,
    VerticalDatum,
    aoi_area_km2,
    extract_api_error,
)

SU4080_AOI = (-1.270, 51.170, -1.255, 51.180)
CONTINENTAL_AOI = (-10.0, 36.0, 30.0, 60.0)
ARCTIC_AOI = (10.0, 70.0, 11.0, 71.0)

#: The exact box used for the recorded over-limit error, and the area the API
#: itself computed for it. Used to check the local estimate against the
#: authority rather than against another estimate.
API_AREA_CHECK_AOI = (-170.0, -60.0, 170.0, 70.0)
API_REPORTED_AREA_KM2 = 435_382_432.0


@pytest.fixture(scope="module")
def fixture_dir(request):
    path = (
        request.config.rootpath / "test-data" / "provider-fixtures" / "opentopography"
    )
    if not path.is_dir():
        pytest.skip(
            "provider fixtures missing. Run: "
            "python test-data/record_provider_fixtures.py"
        )
    return path


@pytest.fixture(scope="module")
def recorded_geotiff(fixture_dir) -> bytes:
    return (fixture_dir / "cop30_su4080.tif").read_bytes()


@pytest.fixture
def adapter(monkeypatch):
    """An adapter with a key configured, and no network access."""
    monkeypatch.setenv(OPENTOPOGRAPHY_CREDENTIAL.env_var, "test-key-0123456789")

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "this test must not reach the network; patch urlopen to replay a "
            "recorded response instead"
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    return OpenTopographyAdapter(CredentialStore())


def _replay(monkeypatch, body: bytes, *, status: int = 200):
    """Patch urlopen to return a recorded body."""

    class Response:
        def __init__(self):
            self.status = status
            self.headers = {"Content-Type": "application/octet-stream"}

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())


def _replay_error(monkeypatch, body: bytes, *, code: int = 400):
    """Patch urlopen to raise an HTTPError carrying a recorded body."""

    def raise_http_error(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://portal.opentopography.org/API/globaldem",
            code,
            "Bad Request",
            {"Content-Type": "application/xml"},
            io.BytesIO(body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)


# ---------------------------------------------------------------------------
# REQ-13: vertical datums
# ---------------------------------------------------------------------------


def test_every_dataset_declares_a_vertical_datum():
    """REQ-13 requires the datum to be recorded, so none may be left implicit.

    An undeclared datum would silently become "whatever the code assumed",
    which is the condition REQ-13's warning exists to prevent.
    """
    for dem in GLOBAL_DEMS:
        assert dem.vertical_datum, f"{dem.demtype} declares no vertical datum"
        assert dem.vertical_datum != VerticalDatum.UNKNOWN or dem.notes, (
            f"{dem.demtype} has an unknown datum and no note explaining it"
        )


def test_ellipsoidal_variants_are_distinguished_from_orthometric():
    """The pair REQ-13's 50 m figure is about.

    SRTMGL1 and SRTMGL1_E differ by nothing in the name but a suffix, and by
    roughly 50 m in value over the UK. Treating them as interchangeable is the
    silent error; declaring different datums is what makes a mismatch
    detectable.
    """
    assert DEMS_BY_TYPE["SRTMGL1"].vertical_datum == VerticalDatum.EGM96
    assert (
        DEMS_BY_TYPE["SRTMGL1_E"].vertical_datum == VerticalDatum.WGS84_ELLIPSOID
    )
    assert DEMS_BY_TYPE["AW3D30"].vertical_datum == VerticalDatum.EGM96
    assert DEMS_BY_TYPE["AW3D30_E"].vertical_datum == VerticalDatum.WGS84_ELLIPSOID
    # And the Copernicus products are a third datum again.
    assert DEMS_BY_TYPE["COP30"].vertical_datum == VerticalDatum.EGM2008


def test_ellipsoidal_datasets_warn_in_their_notes():
    """A caller choosing from a menu needs the warning at the point of choice."""
    for demtype in ("SRTMGL1_E", "AW3D30_E"):
        notes = DEMS_BY_TYPE[demtype].notes.lower()
        assert "ellipsoid" in notes, DEMS_BY_TYPE[demtype].notes


def test_search_carries_the_datum_into_the_dataset(adapter):
    """It has to reach the caller, not just sit in the catalogue."""
    datasets = {d.identifier: d for d in adapter.search(SU4080_AOI)}
    assert datasets["COP30"].extra["vertical_datum"] == VerticalDatum.EGM2008
    assert (
        datasets["SRTMGL1_E"].extra["vertical_datum"]
        == VerticalDatum.WGS84_ELLIPSOID
    )


# ---------------------------------------------------------------------------
# search: no network, filtered locally
# ---------------------------------------------------------------------------


def test_search_makes_no_network_call(adapter):
    """The catalogue is fixed, so availability is decided locally.

    The ``adapter`` fixture patches urlopen to raise, so any request would fail
    this test rather than quietly working.
    """
    datasets = adapter.search(SU4080_AOI)
    assert datasets, "expected datasets for a small UK AOI"


def test_search_returns_finest_resolution_first(adapter):
    """"What is the best elevation available here" is the usual question."""
    resolutions = [d.resolution_m for d in adapter.search(SU4080_AOI)]
    assert resolutions == sorted(resolutions), resolutions
    assert resolutions[0] == 30.0


def test_copernicus_leads_its_resolution_band(adapter):
    """The leading entry is what gets chosen, so it must be safe to take.

    Sorting 30 m datasets by identifier alone put AW3D30 first, which is
    defensible on resolution and wrong on merit: COP30 is truly global and
    current. An explicit preference rank decides it rather than the alphabet.
    """
    datasets = adapter.search(SU4080_AOI)
    assert datasets[0].identifier == "COP30", (
        f"COP30 should lead; got {[d.identifier for d in datasets[:3]]}"
    )
    by_resolution: dict[float, list[str]] = {}
    for dataset in datasets:
        by_resolution.setdefault(dataset.resolution_m, []).append(dataset.identifier)
    assert by_resolution[90.0][0] == "COP90", by_resolution[90.0]


def test_ellipsoidal_variants_never_lead(adapter):
    """Leading with one would hand over heights ~50 m off, unprompted.

    They are legitimate products and stay in the menu — they are just not
    something to pick without having read why (REQ-13).
    """
    datasets = adapter.search(SU4080_AOI)
    identifiers = [d.identifier for d in datasets]
    assert identifiers[0] not in ("SRTMGL1_E", "AW3D30_E"), identifiers

    thirty_metre = [d.identifier for d in datasets if d.resolution_m == 30.0]
    for ellipsoidal in ("SRTMGL1_E", "AW3D30_E"):
        assert ellipsoidal in thirty_metre, thirty_metre
        orthometric = [i for i in thirty_metre if not i.endswith("_E")]
        assert thirty_metre.index(ellipsoidal) > max(
            thirty_metre.index(i) for i in orthometric
        ), (
            f"{ellipsoidal} ranks above an orthometric product: {thirty_metre}"
        )


def test_search_excludes_datasets_that_cannot_cover_the_latitude(adapter):
    """SRTM stops at 60°N, which is a real trap for northern work.

    Above it, SRTM-derived products return nothing — so they are filtered out
    rather than offered and allowed to fail.
    """
    identifiers = {d.identifier for d in adapter.search(ARCTIC_AOI)}
    assert "COP30" in identifiers, identifiers
    assert "AW3D30" in identifiers, identifiers
    for srtm in ("SRTMGL1", "SRTMGL3", "NASADEM"):
        assert srtm not in identifiers, (
            f"{srtm} does not reach 70°N and must not be offered there"
        )


def test_search_excludes_datasets_over_their_area_limit(adapter):
    """A continental AOI exceeds the 30 m per-request cap."""
    identifiers = {d.identifier for d in adapter.search(CONTINENTAL_AOI)}
    assert "COP30" not in identifiers, identifiers
    assert "SRTM15Plus" in identifiers, (
        f"the 450 m product allows 125 million km2 and should survive: "
        f"{identifiers}"
    )


def test_exclusions_are_reported_with_reasons(adapter):
    """Silence would make a coarse-only menu look like all that exists.

    The remedy — split the AOI — is only obvious once the reason is stated.
    """
    excluded = {entry["identifier"]: entry for entry in adapter.unavailable_for(CONTINENTAL_AOI)}
    assert "COP30" in excluded, sorted(excluded)
    reason = excluded["COP30"]["reason"]
    assert "450,000 km2" in reason, reason
    assert excluded["COP30"]["remedy"], describe(excluded["COP30"])


def test_latitude_exclusions_say_so(adapter):
    excluded = {e["identifier"]: e for e in adapter.unavailable_for(ARCTIC_AOI)}
    assert "SRTMGL1" in excluded, sorted(excluded)
    assert "latitudes" in excluded["SRTMGL1"]["reason"], excluded["SRTMGL1"]


def test_search_ignores_requests_for_other_data_types(adapter):
    """This provider serves elevation and nothing else."""
    assert adapter.search(SU4080_AOI, (DataType.BUILDINGS,)) == []
    assert adapter.search(SU4080_AOI, (DataType.ELEVATION,))


def test_datasets_report_their_native_crs(adapter):
    """Always 4326, which is why REQ-12's refinement matters.

    Everything from here needs reprojecting, so the policy prefers leaving a
    higher-resolution national DEM unresampled instead.
    """
    for dataset in adapter.search(SU4080_AOI):
        assert dataset.crs == "EPSG:4326", dataset.identifier


def test_datasets_estimate_their_size_for_req4d(adapter):
    """REQ-4d requires the confirmation to state approximate size."""
    datasets = {d.identifier: d for d in adapter.search(SU4080_AOI)}
    fine = datasets["COP30"].estimated_size_bytes
    coarse = datasets["COP90"].estimated_size_bytes
    assert fine > coarse, (
        f"a 30 m product over the same area should estimate larger than 90 m; "
        f"got {fine} vs {coarse}"
    )
    assert fine > 0


# ---------------------------------------------------------------------------
# The area estimate, checked against the API's own figure
# ---------------------------------------------------------------------------


def test_area_estimate_agrees_with_the_api():
    """Checked against the authority, not against another estimate.

    The recorded over-limit error reports 435,382,432 km2 for this box. The
    local spherical estimate exists to refuse an obviously over-large request
    without spending one of a 50-per-day allowance, so it only has to agree
    closely enough to make the same decision.
    """
    estimated = aoi_area_km2(API_AREA_CHECK_AOI)
    difference = abs(estimated - API_REPORTED_AREA_KM2) / API_REPORTED_AREA_KM2
    assert difference < 0.005, (
        f"local area estimate drifts from the API's own figure by "
        f"{difference:.2%}\n"
        f"    API:       {API_REPORTED_AREA_KM2:,.0f} km2\n"
        f"    estimated: {estimated:,.0f} km2"
    )


def test_small_aoi_area_is_about_a_square_kilometre():
    """The fixture AOI, sanity-checked so the estimate is not merely consistent."""
    area = aoi_area_km2(SU4080_AOI)
    assert 0.5 < area < 3.0, f"expected roughly 1 km2, got {area}"


# ---------------------------------------------------------------------------
# Error-body parsing, against the recorded bodies
# ---------------------------------------------------------------------------


def test_extract_error_from_the_recorded_bad_demtype_body(fixture_dir):
    body = (fixture_dir / "error_bad_demtype.xml").read_bytes()
    message = extract_api_error(body)
    assert message == "Error: NOTADEM is not a valid dataset. Please check the API doc."


def test_extract_error_from_the_recorded_area_limit_body(fixture_dir):
    """The message names both the limit and the computed area.

    That is better than anything we could word ourselves, which is why it is
    passed through rather than replaced.
    """
    body = (fixture_dir / "error_area_limit.xml").read_bytes()
    message = extract_api_error(body)
    assert "maximum area for COP30 is 450,000 km2" in message, message
    assert "435,382,432" in message, message


def test_extract_error_falls_back_on_an_unexpected_body():
    """A changed convention should still yield something readable."""
    assert "gateway" in extract_api_error(b"<html>502 bad gateway</html>").lower()
    assert extract_api_error(b"") == "(no message)"


# ---------------------------------------------------------------------------
# fetch: pre-flight refusals, before any request
# ---------------------------------------------------------------------------


def test_fetch_refuses_an_over_limit_area_locally(adapter, tmp_path):
    """Refused before the request, to protect the daily allowance.

    The ``adapter`` fixture makes any network call fail the test, so this
    passing is itself the evidence that no request was made.
    """
    dataset = adapter.search(SU4080_AOI)[0]
    assert dataset.identifier == "COP30"
    with pytest.raises(OpenTopographyError) as caught:
        adapter.fetch(dataset, API_AREA_CHECK_AOI, tmp_path / "out.tif")
    message = str(caught.value)
    assert "450,000 km2" in message, message
    assert "daily allowance" in message, message


def test_fetch_refuses_an_aoi_outside_the_datasets_latitude_band(
    adapter, tmp_path
):
    dataset = next(
        d for d in adapter.search(SU4080_AOI) if d.identifier == "SRTMGL1"
    )
    with pytest.raises(OpenTopographyError) as caught:
        adapter.fetch(dataset, ARCTIC_AOI, tmp_path / "out.tif")
    message = str(caught.value)
    assert "latitudes" in message, message
    assert "COP30" in message, (
        f"the error should name a dataset that would work.\n{message}"
    )


def test_fetch_refuses_an_unknown_dataset(adapter, tmp_path):
    from qgis_mcp_server.providers.base import Dataset, LicenceDeclaration, LicenceTier

    bogus = Dataset(
        provider="opentopography",
        identifier="NOTADEM",
        title="nope",
        data_type=DataType.ELEVATION,
        licence=LicenceDeclaration(tier=LicenceTier.PUBLIC_DOMAIN, summary="x"),
    )
    with pytest.raises(OpenTopographyError) as caught:
        adapter.fetch(bogus, SU4080_AOI, tmp_path / "out.tif")
    assert "not an OpenTopography global" in str(caught.value)


def test_fetch_without_a_key_says_where_to_get_one(monkeypatch, tmp_path):
    monkeypatch.delenv(OPENTOPOGRAPHY_CREDENTIAL.env_var, raising=False)
    adapter = OpenTopographyAdapter(CredentialStore())
    dataset = adapter.search(SU4080_AOI)[0]
    with pytest.raises(OpenTopographyError) as caught:
        adapter.fetch(dataset, SU4080_AOI, tmp_path / "out.tif")
    message = str(caught.value)
    assert OPENTOPOGRAPHY_CREDENTIAL.registration_url in message, message
    assert OPENTOPOGRAPHY_CREDENTIAL.env_var in message, message


# ---------------------------------------------------------------------------
# fetch: the request URL
# ---------------------------------------------------------------------------


def test_build_url_maps_the_bbox_to_the_apis_parameter_names(adapter):
    """A fourth coordinate convention: four separate named parameters.

    south/north/west/east are unambiguous individually, which is a mercy —
    but the mapping from (min_lon, min_lat, max_lon, max_lat) still has to be
    right, and a swap here would fetch a different place without erroring.
    """
    url = adapter.build_url("COP30", SU4080_AOI, api_key="KEY")
    assert "south=51.17" in url, url
    assert "north=51.18" in url, url
    assert "west=-1.27" in url, url
    assert "east=-1.255" in url, url
    assert "demtype=COP30" in url, url
    assert "outputFormat=GTiff" in url, url


def test_build_url_rejects_an_unknown_output_format():
    with pytest.raises(ValueError):
        OpenTopographyAdapter(CredentialStore(), output_format="JPEG2000")


# ---------------------------------------------------------------------------
# fetch: replaying the recorded responses
# ---------------------------------------------------------------------------


def test_fetch_writes_the_recorded_raster(adapter, monkeypatch, recorded_geotiff, tmp_path):
    _replay(monkeypatch, recorded_geotiff)
    dataset = adapter.search(SU4080_AOI)[0]
    destination = tmp_path / "dem.tif"

    written = adapter.fetch(dataset, SU4080_AOI, destination)

    assert written == destination
    assert destination.read_bytes() == recorded_geotiff
    assert destination.stat().st_size == 9033


def test_fetch_leaves_no_partial_file_behind(adapter, monkeypatch, recorded_geotiff, tmp_path):
    """The download is staged and moved, so an interruption cannot leave a
    truncated raster that later looks loadable."""
    _replay(monkeypatch, recorded_geotiff)
    destination = tmp_path / "dem.tif"
    adapter.fetch(adapter.search(SU4080_AOI)[0], SU4080_AOI, destination)
    assert list(tmp_path.glob("*.part")) == [], "a staging file was left behind"


def test_fetch_translates_the_recorded_api_error(
    adapter, monkeypatch, fixture_dir, tmp_path
):
    """The API's own message survives to the caller."""
    body = (fixture_dir / "error_bad_demtype.xml").read_bytes()
    _replay_error(monkeypatch, body)
    dataset = adapter.search(SU4080_AOI)[0]

    with pytest.raises(OpenTopographyError) as caught:
        adapter.fetch(dataset, SU4080_AOI, tmp_path / "out.tif")

    message = str(caught.value)
    assert "HTTP 400" in message, message
    assert "not a valid dataset" in message, message
    assert not (tmp_path / "out.tif").exists(), "no file should be written"


@pytest.mark.parametrize(
    "body, fragment",
    [
        (b"", "empty response"),
        (
            b'<?xml version="1.0"?><error>Error: something</error>',
            "error document rather than raster",
        ),
        (b"<html><body>502 Bad Gateway</body></html>", "TIFF byte-order marker"),
        (b"not a tiff at all, just text", "TIFF byte-order marker"),
    ],
)
def test_fetch_refuses_to_write_a_non_raster(
    adapter, monkeypatch, tmp_path, body, fragment
):
    """A 200 carrying the wrong content is the worst outcome.

    The service answers errors with HTTP 400, so this should never fire against
    it as it behaves today. It exists because the alternative failure is
    silent: a proxy error page written out with a ``.tif`` extension fails much
    later, somewhere that cannot explain why.
    """
    _replay(monkeypatch, body)
    dataset = adapter.search(SU4080_AOI)[0]
    destination = tmp_path / "out.tif"

    with pytest.raises(OpenTopographyError) as caught:
        adapter.fetch(dataset, SU4080_AOI, destination)
    assert fragment in str(caught.value), str(caught.value)
    assert not destination.exists(), "nothing should have been written"


def test_fetch_reports_an_unreachable_service(adapter, monkeypatch, tmp_path):
    def unreachable(*_args, **_kwargs):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    dataset = adapter.search(SU4080_AOI)[0]
    with pytest.raises(OpenTopographyError) as caught:
        adapter.fetch(dataset, SU4080_AOI, tmp_path / "out.tif")
    assert "could not reach OpenTopography" in str(caught.value)


def test_api_key_never_appears_in_an_error(adapter, monkeypatch, fixture_dir, tmp_path):
    """The URL carries the key, so error messages must not carry the URL.

    An exception propagates into a tool response and into logs; a key echoed
    there is a leaked key.
    """
    secret = "SECRET-KEY-DO-NOT-LEAK-0001"
    monkeypatch.setenv(OPENTOPOGRAPHY_CREDENTIAL.env_var, secret)
    leaky = OpenTopographyAdapter(CredentialStore())
    _replay_error(monkeypatch, (fixture_dir / "error_area_limit.xml").read_bytes())

    with pytest.raises(OpenTopographyError) as caught:
        leaky.fetch(leaky.search(SU4080_AOI)[0], SU4080_AOI, tmp_path / "out.tif")

    assert secret not in str(caught.value), "the API key leaked into the error"
    assert "API_Key" not in str(caught.value), str(caught.value)


# ---------------------------------------------------------------------------
# The recorded raster really is elevation data
# ---------------------------------------------------------------------------


def test_the_recorded_raster_loads_in_qgis(runner, fixture_dir):
    """End-to-end proof that a fetch produces something QGIS can open.

    Validating the TIFF magic byte only proves the first two bytes. This opens
    the recorded response as a raster layer and reads its elevations, which is
    the assertion that the pipeline from here to a heightmap has real data to
    work with.
    """
    path = (fixture_dir / "cop30_su4080.tif").resolve()
    envelope = runner.run_code(
        "import numpy as np\n"
        "from osgeo import gdal, osr\n"
        f"dataset = gdal.Open({str(path)!r})\n"
        "band = dataset.GetRasterBand(1)\n"
        "array = band.ReadAsArray()\n"
        "srs = osr.SpatialReference()\n"
        "srs.ImportFromWkt(dataset.GetProjection())\n"
        "gt = dataset.GetGeoTransform()\n"
        "result = {\n"
        "    'width': dataset.RasterXSize,\n"
        "    'height': dataset.RasterYSize,\n"
        "    'bands': dataset.RasterCount,\n"
        "    'crs': '{}:{}'.format(srs.GetAuthorityName(None), srs.GetAuthorityCode(None)),\n"
        "    'min': float(np.nanmin(array)),\n"
        "    'max': float(np.nanmax(array)),\n"
        "    'origin': [gt[0], gt[3]],\n"
        "}\n"
    )
    assert_ok(envelope, context_note="opening the recorded COP30 response")
    result = envelope.result

    assert result["bands"] == 1, describe(result)
    assert result["crs"] == "EPSG:4326", (
        f"OpenTopography serves 4326; got {result['crs']}"
    )
    assert result["width"] > 0 and result["height"] > 0, describe(result)

    # Real Hampshire chalk downland: roughly 70-130 m. A plausibility band
    # rather than exact values, since this is live data rather than a
    # constructed fixture -- but wide enough to catch nodata-filled or
    # byte-swapped output, which is what would actually go wrong.
    assert 0.0 < result["min"] < 200.0, (
        f"elevations look wrong for Hampshire downland: {describe(result)}"
    )
    assert result["min"] < result["max"] < 300.0, describe(result)

    # The requested AOI's south-west corner, give or take a pixel.
    assert result["origin"][0] == pytest.approx(SU4080_AOI[0], abs=0.01), describe(
        result
    )


# ---------------------------------------------------------------------------
# The live call, excluded from the default pass
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_fetch_against_the_real_api(tmp_path):
    """Opt-in. Spends one of 50 daily calls.

    Run with: pytest -m integration

    `tasks.md` 4.7 specifies live fetching be "covered by a single opt-in
    integration test against a known small dataset, run occasionally rather
    than in the default pass". The allowance is what makes that necessary: a
    live call per test run would exhaust a day's quota in one session of
    iteration.
    """
    from qgis_mcp_server.config import Config

    store = Config.load().credential_store
    if not store.get(OPENTOPOGRAPHY_CREDENTIAL):
        pytest.skip("no OpenTopography API key configured")

    adapter = OpenTopographyAdapter(store)
    dataset = next(
        d for d in adapter.search(SU4080_AOI) if d.identifier == "COP30"
    )
    destination = adapter.fetch(dataset, SU4080_AOI, tmp_path / "live.tif")

    assert destination.exists()
    assert destination.stat().st_size > 1024
    assert destination.read_bytes()[:2] in (b"II", b"MM")
