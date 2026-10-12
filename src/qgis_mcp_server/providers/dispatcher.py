"""Fanning a search out across provider adapters (task 4.0).

Three behaviours from ``spec/design.md`` §4, each of which exists because the
obvious alternative is worse:

**Coverage pre-filtering.** Adapters declare their extent and are skipped before
any network call when they cannot serve the AOI -- no querying OS Data Hub for a
bounding box in Ontario. The skip is *reported* rather than silent, because an
absent provider with no explanation reads as a provider that had nothing, which
is a different and misleading answer.

**Bounded parallel fan-out.** Queries are independent, so they run concurrently;
Overpass and Nominatim rate-limit, so concurrency is bounded.

**Partial results, never a failed menu** (REQ-10). A provider that fails, times
out or lacks credentials returns a status alongside the successes. A single
unreachable provider must never cost the whole menu -- and this is what makes
``check_api_credentials`` a diagnostic rather than a required pre-flight, since
a missing key surfaces inline during the search instead.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from qgis_mcp_server.providers.base import Dataset, ProviderAdapter

#: Ceiling on concurrent provider queries. Low on purpose: these are rate-limited
#: public services, and five simultaneous requests to Overpass is a good way to
#: be throttled into a slower result than running them in sequence.
DEFAULT_MAX_WORKERS = 4

#: Bound on the whole fan-out, in seconds. Because adapters run concurrently
#: this is also each adapter's budget.
DEFAULT_TIMEOUT = 30.0


class ProviderState:
    """What happened to one provider in a search."""

    OK = "ok"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    #: Credentials required and absent. Reported, never fatal (REQ-10).
    NO_CREDENTIALS = "skipped_no_credentials"
    #: Declared coverage does not reach the AOI. Skipped before any network call.
    NO_COVERAGE = "skipped_no_coverage"
    #: Does not serve any of the requested data types.
    NOT_APPLICABLE = "skipped_not_applicable"

    #: States reached without contacting the provider at all.
    SKIPPED = (NO_CREDENTIALS, NO_COVERAGE, NOT_APPLICABLE)


@dataclass
class ProviderStatus:
    """One provider's outcome, for the per-provider status REQ-10 requires."""

    provider: str
    state: str
    message: str = ""
    dataset_count: int = 0
    elapsed_seconds: float | None = None
    #: Set when the state is actionable by the caller -- a missing key, mostly.
    remedy: str = ""

    @property
    def succeeded(self) -> bool:
        return self.state == ProviderState.OK

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "state": self.state,
            "dataset_count": self.dataset_count,
        }
        if self.message:
            payload["message"] = self.message
        if self.remedy:
            payload["remedy"] = self.remedy
        if self.elapsed_seconds is not None:
            payload["elapsed_seconds"] = round(self.elapsed_seconds, 3)
        return payload


@dataclass
class SearchOutcome:
    """Everything a search produced, successes and failures together."""

    datasets: list[Dataset] = field(default_factory=list)
    statuses: list[ProviderStatus] = field(default_factory=list)

    @property
    def providers_queried(self) -> int:
        return sum(
            1 for status in self.statuses if status.state not in ProviderState.SKIPPED
        )

    @property
    def providers_succeeded(self) -> int:
        return sum(1 for status in self.statuses if status.succeeded)

    @property
    def degraded(self) -> bool:
        """Whether any provider that was actually queried failed.

        Distinct from "a provider was skipped": a skipped provider is a known
        absence the caller can act on, while a failure may be transient and
        worth retrying.
        """
        return any(
            status.state in (ProviderState.FAILED, ProviderState.TIMED_OUT)
            for status in self.statuses
        )

    def grouped(self) -> dict[str, list[Dataset]]:
        """Datasets grouped by data type, across all providers (REQ-8).

        The agent presents one combined menu per request, so the grouping is
        done here rather than leaving the caller to pivot a flat list -- and a
        call for several types returns one grouped response rather than one
        response per type.
        """
        groups: dict[str, list[Dataset]] = {}
        for dataset in self.datasets:
            groups.setdefault(dataset.data_type, []).append(dataset)
        for datasets in groups.values():
            datasets.sort(key=lambda item: (item.provider, item.identifier))
        return groups

    def to_dict(self) -> dict[str, Any]:
        groups = self.grouped()
        return {
            "dataset_count": len(self.datasets),
            "by_data_type": {
                data_type: [dataset.to_dict() for dataset in datasets]
                for data_type, datasets in sorted(groups.items())
            },
            "provider_status": [status.to_dict() for status in self.statuses],
            "providers_queried": self.providers_queried,
            "providers_succeeded": self.providers_succeeded,
            "degraded": self.degraded,
        }


@dataclass
class ProviderDispatcher:
    """Runs a search across adapters, returning whatever succeeded."""

    adapters: tuple[ProviderAdapter, ...] = ()
    max_workers: int = DEFAULT_MAX_WORKERS
    timeout: float = DEFAULT_TIMEOUT

    def eligible(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> tuple[list[ProviderAdapter], list[ProviderStatus]]:
        """Split adapters into those worth querying and those skipped.

        Every skip produces a status. The alternative -- quietly returning
        fewer providers -- makes "BGS has no geology here" indistinguishable
        from "BGS does not cover here", and only one of those is worth the
        caller changing their request over.
        """
        runnable: list[ProviderAdapter] = []
        skipped: list[ProviderStatus] = []

        for adapter in self.adapters:
            capability = adapter.capability

            if not capability.serves(data_types):
                skipped.append(
                    ProviderStatus(
                        provider=adapter.name,
                        state=ProviderState.NOT_APPLICABLE,
                        message=(
                            f"serves {', '.join(capability.data_types)}, none of "
                            f"which was requested"
                        ),
                    )
                )
                continue

            if not capability.covers(aoi):
                skipped.append(
                    ProviderStatus(
                        provider=adapter.name,
                        state=ProviderState.NO_COVERAGE,
                        message=(
                            f"covers {capability.coverage_description}, which "
                            f"does not reach this area of interest"
                        ),
                    )
                )
                continue

            if not adapter.is_usable():
                status = adapter.credential_status()
                skipped.append(
                    ProviderStatus(
                        provider=adapter.name,
                        state=ProviderState.NO_CREDENTIALS,
                        message=(
                            f"requires an API key and none is configured, so it "
                            f"was not queried"
                        ),
                        remedy=(
                            f"register at {status.registration_url}"
                            if status is not None and status.registration_url
                            else ""
                        ),
                    )
                )
                continue

            runnable.append(adapter)

        return runnable, skipped

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
        *,
        timeout: float | None = None,
    ) -> SearchOutcome:
        """Search every eligible provider, returning successes and statuses.

        Never raises for a provider-side problem. An adapter that throws, hangs
        or returns nonsense becomes a status in the outcome, because REQ-10
        makes the menu's survival more important than any one provider's
        contribution.
        """
        runnable, statuses = self.eligible(aoi, data_types)
        outcome = SearchOutcome(statuses=list(statuses))
        if not runnable:
            return outcome

        budget = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + budget
        started: dict[Any, tuple[ProviderAdapter, float]] = {}

        # Deliberately not a `with` block. ThreadPoolExecutor.__exit__ calls
        # shutdown(wait=True), which waits for every running task -- so a
        # provider hanging on a socket read would hold this method open for its
        # full duration and the timeout below would bound nothing at all.
        pool = ThreadPoolExecutor(
            max_workers=max(1, min(self.max_workers, len(runnable))),
            thread_name_prefix="qgis-mcp-provider",
        )
        try:
            for adapter in runnable:
                future = pool.submit(self._search_one, adapter, aoi, data_types)
                started[future] = (adapter, time.monotonic())

            pending = set(started)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, pending = wait(
                    pending, timeout=remaining, return_when=FIRST_COMPLETED
                )
                for future in done:
                    adapter, began = started[future]
                    elapsed = time.monotonic() - began
                    try:
                        datasets = future.result()
                    except Exception as exc:  # noqa: BLE001 - reported per REQ-10
                        outcome.statuses.append(
                            ProviderStatus(
                                provider=adapter.name,
                                state=ProviderState.FAILED,
                                message=f"{type(exc).__name__}: {exc}",
                                elapsed_seconds=elapsed,
                            )
                        )
                        continue
                    outcome.datasets.extend(datasets)
                    outcome.statuses.append(
                        ProviderStatus(
                            provider=adapter.name,
                            state=ProviderState.OK,
                            dataset_count=len(datasets),
                            elapsed_seconds=elapsed,
                        )
                    )

            # Whatever is still pending ran out of time.
            #
            # A thread blocked in a socket read cannot be cancelled from here,
            # so what the timeout bounds is *the caller's wait*, not the
            # thread's life: the result is abandoned and the menu is returned
            # without it. The thread ends when its own socket times out, which
            # is why every adapter must set one -- the dispatcher's timeout is
            # not a substitute for it.
            for future in pending:
                adapter, began = started[future]
                future.cancel()
                outcome.statuses.append(
                    ProviderStatus(
                        provider=adapter.name,
                        state=ProviderState.TIMED_OUT,
                        message=(
                            f"did not respond within {budget:.0f}s and was "
                            f"abandoned; its results are not included"
                        ),
                        elapsed_seconds=time.monotonic() - began,
                    )
                )
        finally:
            # cancel_futures discards anything still queued; wait=False means
            # we do not block on whatever is already running.
            pool.shutdown(wait=False, cancel_futures=True)

        outcome.statuses.sort(key=lambda status: status.provider)
        return outcome

    @staticmethod
    def _search_one(
        adapter: ProviderAdapter,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None,
    ) -> list[Dataset]:
        """Call one adapter, rejecting a result that is not a dataset list.

        An adapter returning the wrong shape would otherwise corrupt the
        combined menu rather than failing visibly as its own provider.
        """
        datasets = adapter.search(aoi, data_types)
        if datasets is None:
            return []
        if not isinstance(datasets, list) or any(
            not isinstance(item, Dataset) for item in datasets
        ):
            raise TypeError(
                f"{adapter.name}.search returned {type(datasets).__name__}; "
                f"expected a list of Dataset"
            )
        return datasets


__all__ = [
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_TIMEOUT",
    "ProviderDispatcher",
    "ProviderState",
    "ProviderStatus",
    "SearchOutcome",
]
