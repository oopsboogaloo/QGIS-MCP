"""Task 4.0: the provider dispatcher, and REQ-10 specifically.

`tasks.md` 4.0 names the test outright: "REQ-10 directly -- inject one failing,
one timing-out and one credential-less adapter; assert the menu still returns
successful results with per-provider statuses attached."

So the adapters here are deliberately synthetic. A real provider cannot be made
to fail on demand, and the behaviour under test is the dispatcher's, not any
provider's. No QGIS and no network, so these run in milliseconds.
"""

from __future__ import annotations

import time

import pytest

from conftest import describe
from qgis_mcp_server.credentials import CredentialSpec, CredentialStore
from qgis_mcp_server.providers.base import (
    Dataset,
    DataType,
    LicenceDeclaration,
    LicenceTier,
    ProviderAdapter,
    ProviderCapability,
)
from qgis_mcp_server.providers.dispatcher import (
    ProviderDispatcher,
    ProviderState,
)

UK_AOI = (-1.27, 51.17, -1.255, 51.18)
ONTARIO_AOI = (-80.0, 43.0, -79.0, 44.0)

OPEN_LICENCE = LicenceDeclaration(
    tier=LicenceTier.PUBLIC_DOMAIN, summary="test licence"
)

MISSING_CREDENTIAL = CredentialSpec(
    provider="needs_key",
    config_key="needs_key",
    env_var="QGIS_MCP_DISPATCHER_TEST_KEY_THAT_IS_NOT_SET",
    registration_url="https://example.invalid/register",
)


def _capability(
    name: str,
    *,
    data_types: tuple[str, ...] = (DataType.ELEVATION,),
    coverage: tuple[float, float, float, float] | None = None,
    credential: CredentialSpec | None = None,
) -> ProviderCapability:
    return ProviderCapability(
        name=name,
        long_name=name,
        protocol_family="rest",
        data_types=data_types,
        licence=OPEN_LICENCE,
        coverage=coverage,
        coverage_description="test coverage" if coverage else "global",
        credential=credential,
        requires_credentials=credential is not None,
    )


def _dataset(provider: str, index: int, data_type: str = DataType.ELEVATION) -> Dataset:
    return Dataset(
        provider=provider,
        identifier=f"{provider}-{index}",
        title=f"{provider} dataset {index}",
        data_type=data_type,
        licence=OPEN_LICENCE,
    )


class GoodAdapter(ProviderAdapter):
    def __init__(self, name="good", count=2, data_types=(DataType.ELEVATION,), **kw):
        super().__init__(CredentialStore())
        self.capability = _capability(name, data_types=data_types, **kw)
        self.count = count
        self.calls = 0

    def search(self, aoi, data_types=None):
        self.calls += 1
        return [
            _dataset(self.capability.name, index, self.capability.data_types[0])
            for index in range(self.count)
        ]

    def fetch(self, dataset, aoi, destination):  # pragma: no cover
        raise NotImplementedError


class FailingAdapter(ProviderAdapter):
    def __init__(self, name="failing"):
        super().__init__(CredentialStore())
        self.capability = _capability(name)

    def search(self, aoi, data_types=None):
        raise RuntimeError("provider exploded")

    def fetch(self, dataset, aoi, destination):  # pragma: no cover
        raise NotImplementedError


class HangingAdapter(ProviderAdapter):
    def __init__(self, name="hanging", delay=30.0):
        super().__init__(CredentialStore())
        self.capability = _capability(name)
        self.delay = delay

    def search(self, aoi, data_types=None):
        time.sleep(self.delay)
        return [_dataset(self.capability.name, 0)]

    def fetch(self, dataset, aoi, destination):  # pragma: no cover
        raise NotImplementedError


class CredentiallessAdapter(ProviderAdapter):
    def __init__(self, name="needs_key"):
        super().__init__(CredentialStore())
        self.capability = _capability(name, credential=MISSING_CREDENTIAL)
        self.calls = 0

    def search(self, aoi, data_types=None):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("a credential-less adapter must not be queried")

    def fetch(self, dataset, aoi, destination):  # pragma: no cover
        raise NotImplementedError


class WrongShapeAdapter(ProviderAdapter):
    def __init__(self, name="wrong_shape"):
        super().__init__(CredentialStore())
        self.capability = _capability(name)

    def search(self, aoi, data_types=None):
        return {"not": "a list"}

    def fetch(self, dataset, aoi, destination):  # pragma: no cover
        raise NotImplementedError


def _status(outcome, provider):
    return next(s for s in outcome.statuses if s.provider == provider)


# ---------------------------------------------------------------------------
# REQ-10: partial results, never a failed menu
# ---------------------------------------------------------------------------


def test_req10_one_failing_one_timing_out_one_credentialless(monkeypatch):
    """The test tasks.md 4.0 specifies, in one case.

    A single unreachable provider must never cost the whole menu. The good
    adapter's results survive, and every other provider explains itself.
    """
    monkeypatch.delenv(MISSING_CREDENTIAL.env_var, raising=False)
    good = GoodAdapter(count=3)
    credentialless = CredentiallessAdapter()
    dispatcher = ProviderDispatcher(
        adapters=(good, FailingAdapter(), HangingAdapter(delay=30.0), credentialless),
        timeout=2.0,
    )

    outcome = dispatcher.search(UK_AOI)

    assert len(outcome.datasets) == 3, (
        f"the successful provider's results must survive\n"
        f"{describe(outcome.to_dict())}"
    )
    assert _status(outcome, "good").state == ProviderState.OK
    assert _status(outcome, "failing").state == ProviderState.FAILED
    assert _status(outcome, "hanging").state == ProviderState.TIMED_OUT
    assert _status(outcome, "needs_key").state == ProviderState.NO_CREDENTIALS
    assert outcome.degraded is True, describe(outcome.to_dict())
    assert credentialless.calls == 0, (
        "an adapter with no credentials must be skipped before it is called, "
        "not called and allowed to fail"
    )


def test_failure_message_names_the_exception(monkeypatch):
    """A status that says only "failed" sends the reader to the logs."""
    dispatcher = ProviderDispatcher(adapters=(FailingAdapter(),), timeout=5.0)
    outcome = dispatcher.search(UK_AOI)
    status = _status(outcome, "failing")
    assert "RuntimeError" in status.message, status.message
    assert "provider exploded" in status.message, status.message


def test_timeout_bounds_the_whole_fan_out():
    """A hung provider must not hold the menu open indefinitely."""
    dispatcher = ProviderDispatcher(
        adapters=(GoodAdapter(), HangingAdapter(delay=60.0)), timeout=1.5
    )
    began = time.monotonic()
    outcome = dispatcher.search(UK_AOI)
    elapsed = time.monotonic() - began

    assert elapsed < 10.0, (
        f"the fan-out took {elapsed:.1f}s against a 1.5s budget; the timeout "
        f"is not bounding it"
    )
    assert len(outcome.datasets) == 2, describe(outcome.to_dict())
    assert _status(outcome, "hanging").state == ProviderState.TIMED_OUT


def test_credential_status_carries_the_remedy(monkeypatch):
    """A missing key is only actionable with somewhere to go."""
    monkeypatch.delenv(MISSING_CREDENTIAL.env_var, raising=False)
    dispatcher = ProviderDispatcher(adapters=(CredentiallessAdapter(),))
    outcome = dispatcher.search(UK_AOI)
    status = _status(outcome, "needs_key")
    assert status.remedy == f"register at {MISSING_CREDENTIAL.registration_url}", (
        describe(status.to_dict())
    )


def test_configured_credential_lets_the_adapter_run(monkeypatch):
    """The other side of the credential check, so it is not vacuous."""
    monkeypatch.setenv(MISSING_CREDENTIAL.env_var, "a-key")
    adapter = GoodAdapter(name="needs_key", credential=MISSING_CREDENTIAL)
    outcome = ProviderDispatcher(adapters=(adapter,)).search(UK_AOI)
    assert _status(outcome, "needs_key").state == ProviderState.OK
    assert adapter.calls == 1


def test_wrong_return_shape_fails_only_its_own_provider():
    """An adapter returning the wrong type must not corrupt the menu.

    Extending the dataset list with arbitrary objects would push the problem
    downstream into whatever tried to read them; failing the one provider
    keeps the blame where it belongs.
    """
    dispatcher = ProviderDispatcher(
        adapters=(GoodAdapter(count=1), WrongShapeAdapter()), timeout=5.0
    )
    outcome = dispatcher.search(UK_AOI)
    assert len(outcome.datasets) == 1, describe(outcome.to_dict())
    status = _status(outcome, "wrong_shape")
    assert status.state == ProviderState.FAILED
    assert "expected a list of Dataset" in status.message, status.message


def test_every_provider_appears_in_the_status_list():
    """Silence about a provider is itself a misleading answer."""
    adapters = (
        GoodAdapter(),
        FailingAdapter(),
        GoodAdapter(name="out_of_area", coverage=(10.0, 40.0, 20.0, 50.0)),
    )
    outcome = ProviderDispatcher(adapters=adapters, timeout=5.0).search(UK_AOI)
    reported = {status.provider for status in outcome.statuses}
    assert reported == {"good", "failing", "out_of_area"}, reported


# ---------------------------------------------------------------------------
# Coverage pre-filtering
# ---------------------------------------------------------------------------


def test_out_of_coverage_provider_is_skipped_without_being_called():
    """"No querying OS Data Hub for a bounding box in Ontario" (design.md §4)."""
    uk_only = GoodAdapter(name="uk_only", coverage=(-9.0, 49.75, 2.0, 61.0))
    outcome = ProviderDispatcher(adapters=(uk_only,)).search(ONTARIO_AOI)

    assert uk_only.calls == 0, "the skip must happen before any network call"
    status = _status(outcome, "uk_only")
    assert status.state == ProviderState.NO_COVERAGE
    assert "test coverage" in status.message, status.message
    assert outcome.providers_queried == 0, describe(outcome.to_dict())


def test_skipped_is_distinguished_from_failed():
    """They call for different responses from the caller.

    A skip is a known absence to work around; a failure may be transient and
    worth retrying. Collapsing them loses that.
    """
    adapters = (
        GoodAdapter(name="uk_only", coverage=(-9.0, 49.75, 2.0, 61.0)),
        FailingAdapter(),
    )
    outcome = ProviderDispatcher(adapters=adapters, timeout=5.0).search(ONTARIO_AOI)
    assert _status(outcome, "uk_only").state in ProviderState.SKIPPED
    assert _status(outcome, "failing").state not in ProviderState.SKIPPED
    assert outcome.degraded is True


def test_provider_not_serving_the_requested_type_is_skipped():
    elevation = GoodAdapter(name="elevation_only", data_types=(DataType.ELEVATION,))
    outcome = ProviderDispatcher(adapters=(elevation,)).search(
        UK_AOI, (DataType.GEOLOGY,)
    )
    assert elevation.calls == 0
    assert _status(outcome, "elevation_only").state == ProviderState.NOT_APPLICABLE


def test_no_eligible_providers_returns_an_empty_menu_not_an_error():
    """An empty menu with explanations beats an exception.

    "Nobody serves geology in Ontario" is a usable answer; a raised error is
    not, and would make the caller unable to tell it from a broken dispatcher.
    """
    uk_only = GoodAdapter(name="uk_only", coverage=(-9.0, 49.75, 2.0, 61.0))
    outcome = ProviderDispatcher(adapters=(uk_only,)).search(ONTARIO_AOI)
    assert outcome.datasets == []
    assert outcome.statuses, "the absence must still be explained"
    assert outcome.degraded is False, (
        "a clean skip is not degradation -- nothing went wrong"
    )


# ---------------------------------------------------------------------------
# REQ-8: grouped results
# ---------------------------------------------------------------------------


def test_results_are_grouped_by_data_type_across_providers():
    """REQ-8: one grouped response, not one response per type.

    The agent presents one combined menu per request, so grouping belongs here
    rather than leaving the caller to pivot a flat list.
    """
    adapters = (
        GoodAdapter(name="dem_a", count=2, data_types=(DataType.ELEVATION,)),
        GoodAdapter(name="dem_b", count=1, data_types=(DataType.ELEVATION,)),
        GoodAdapter(name="roads", count=3, data_types=(DataType.ROADS,)),
    )
    outcome = ProviderDispatcher(adapters=adapters, timeout=5.0).search(
        UK_AOI, (DataType.ELEVATION, DataType.ROADS)
    )
    groups = outcome.grouped()

    assert set(groups) == {DataType.ELEVATION, DataType.ROADS}, sorted(groups)
    assert len(groups[DataType.ELEVATION]) == 3, describe(outcome.to_dict())
    assert len(groups[DataType.ROADS]) == 3, describe(outcome.to_dict())
    # Elevation came from two providers and is merged into one group.
    assert {d.provider for d in groups[DataType.ELEVATION]} == {"dem_a", "dem_b"}


def test_grouping_is_ordered_reproducibly():
    """Two identical searches must present the menu the same way.

    Results arrive in completion order, which is a race. Sorting makes the
    response stable so a caller referring to "the second option" is not
    referring to a different dataset each time.
    """
    adapters = (
        GoodAdapter(name="zzz", count=2),
        GoodAdapter(name="aaa", count=2),
    )
    dispatcher = ProviderDispatcher(adapters=adapters, timeout=5.0)
    first = dispatcher.search(UK_AOI).grouped()[DataType.ELEVATION]
    second = dispatcher.search(UK_AOI).grouped()[DataType.ELEVATION]
    assert [d.identifier for d in first] == [d.identifier for d in second]
    assert [d.identifier for d in first][0].startswith("aaa")


def test_outcome_counts_report_what_actually_happened():
    adapters = (
        GoodAdapter(count=1),
        FailingAdapter(),
        GoodAdapter(name="uk_only", coverage=(-9.0, 49.75, 2.0, 61.0)),
    )
    outcome = ProviderDispatcher(adapters=adapters, timeout=5.0).search(ONTARIO_AOI)
    assert outcome.providers_queried == 2, describe(outcome.to_dict())
    assert outcome.providers_succeeded == 1, describe(outcome.to_dict())


# ---------------------------------------------------------------------------
# Bounded concurrency
# ---------------------------------------------------------------------------


def test_concurrency_is_bounded():
    """Overpass and Nominatim rate-limit, so the fan-out is capped.

    Asserted by observing how many adapters are in ``search`` at once.
    """
    peak = 0
    current = 0
    import threading

    lock = threading.Lock()

    class CountingAdapter(ProviderAdapter):
        def __init__(self, name):
            super().__init__(CredentialStore())
            self.capability = _capability(name)

        def search(self, aoi, data_types=None):
            nonlocal peak, current
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.2)
            with lock:
                current -= 1
            return [_dataset(self.capability.name, 0)]

        def fetch(self, dataset, aoi, destination):  # pragma: no cover
            raise NotImplementedError

    adapters = tuple(CountingAdapter(f"p{index}") for index in range(8))
    dispatcher = ProviderDispatcher(adapters=adapters, max_workers=3, timeout=20.0)
    outcome = dispatcher.search(UK_AOI)

    assert len(outcome.datasets) == 8, describe(outcome.to_dict())
    assert peak <= 3, f"max_workers=3 but {peak} adapters ran concurrently"


def test_adapters_do_run_concurrently():
    """The other half: bounded, but still parallel.

    Four adapters sleeping 0.3 s each take about 0.3 s together and about 1.2 s
    in sequence, so the elapsed time distinguishes them.
    """

    class SlowAdapter(ProviderAdapter):
        def __init__(self, name):
            super().__init__(CredentialStore())
            self.capability = _capability(name)

        def search(self, aoi, data_types=None):
            time.sleep(0.3)
            return [_dataset(self.capability.name, 0)]

        def fetch(self, dataset, aoi, destination):  # pragma: no cover
            raise NotImplementedError

    adapters = tuple(SlowAdapter(f"s{index}") for index in range(4))
    began = time.monotonic()
    outcome = ProviderDispatcher(adapters=adapters, max_workers=4, timeout=20.0).search(
        UK_AOI
    )
    elapsed = time.monotonic() - began

    assert len(outcome.datasets) == 4
    assert elapsed < 1.0, (
        f"four 0.3s adapters took {elapsed:.2f}s; they appear to be running in "
        f"sequence rather than concurrently"
    )


def test_eligible_reports_without_running_anything():
    """Coverage pre-filtering answers "who could serve this" with no traffic."""
    adapters = (
        GoodAdapter(name="global_dem"),
        GoodAdapter(name="uk_only", coverage=(-9.0, 49.75, 2.0, 61.0)),
    )
    dispatcher = ProviderDispatcher(adapters=adapters)
    runnable, skipped = dispatcher.eligible(ONTARIO_AOI)

    assert [adapter.name for adapter in runnable] == ["global_dem"]
    assert [status.provider for status in skipped] == ["uk_only"]
    assert all(adapter.calls == 0 for adapter in adapters)
