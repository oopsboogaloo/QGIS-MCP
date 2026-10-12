"""Place resolution and the REQ-9 ambiguity contract (task 4.1).

REQ-9: resolve a place string and proceed automatically **if exactly one
candidate clears the confidence threshold**; otherwise return an error
enumerating the candidates with their types and administrative context, rather
than selecting one.

The requirement's own rationale is the design note worth keeping: "ambiguity
should cost one extra round-trip rather than silently producing data for a
similarly-named place on another continent." The failure being guarded is not a
crash -- it is a successful download of the wrong Newport, which nothing
downstream can detect.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qgis_mcp_server.geocoding.base import Geocoder, GeocoderError, PlaceCandidate

#: Minimum relevance for a candidate to count towards "exactly one".
#:
#: Set so that a strong match surrounded by weak ones still resolves, while a
#: field of equally plausible matches asks. When nothing clears it the result is
#: ambiguity, not "not found" -- the candidates are still returned. That is the
#: safe direction: the cost of asking is one round-trip, and the cost of
#: guessing is data for the wrong place.
DEFAULT_CONFIDENCE_THRESHOLD = 0.35


class Resolution:
    """Why a resolution ended the way it did."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass
class GeocoderStatus:
    """One geocoder's outcome, reported for the same reason REQ-10 does."""

    geocoder: str
    state: str
    message: str = ""
    candidate_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "geocoder": self.geocoder,
            "state": self.state,
            "candidate_count": self.candidate_count,
        }
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass
class PlaceResolution:
    """The answer, whether or not it is a single place."""

    query: str
    state: str
    candidates: list[PlaceCandidate] = field(default_factory=list)
    statuses: list[GeocoderStatus] = field(default_factory=list)
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    message: str = ""

    @property
    def resolved(self) -> PlaceCandidate | None:
        """The single candidate, when there is exactly one."""
        if self.state == Resolution.RESOLVED and self.candidates:
            return self.candidates[0]
        return None

    @property
    def above_threshold(self) -> list[PlaceCandidate]:
        return [
            candidate
            for candidate in self.candidates
            if candidate.confidence >= self.threshold
        ]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": self.query,
            "state": self.state,
            "confidence_threshold": self.threshold,
            "candidate_count": len(self.candidates),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "geocoder_status": [status.to_dict() for status in self.statuses],
        }
        if self.message:
            payload["message"] = self.message
        resolved = self.resolved
        if resolved is not None:
            # Hoisted so a caller that only wants the AOI does not have to
            # reach into the candidate list and reason about ordering.
            payload["place"] = resolved.to_dict()
            payload["aoi"] = list(resolved.bbox)
            payload["aoi_crs"] = "EPSG:4326"
        return payload


@dataclass
class PlaceResolver:
    """Resolves place names across a tiered list of geocoders.

    Tiering follows the narrative spec §4.6.1: a UK-specific gazetteer gives
    better names within the UK and nothing at all outside it, so geocoders
    declare a country scope and the resolver tries them in order, falling
    through to the global default.

    With one geocoder configured the tiering is a no-op, which is the current
    state -- see ``geocoding/__init__.py`` for what is deferred.
    """

    geocoders: tuple[Geocoder, ...] = ()
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    def resolve(
        self,
        place: str,
        *,
        limit: int = 10,
        country_code: str | None = None,
        threshold: float | None = None,
    ) -> PlaceResolution:
        """Resolve ``place``, applying the REQ-9 contract.

        Never raises for a geocoder-side problem: an unreachable geocoder
        becomes a status, and only an empty roster produces ``UNAVAILABLE``.
        """
        query = (place or "").strip()
        effective = self.threshold if threshold is None else threshold

        if not query:
            return PlaceResolution(
                query=query,
                state=Resolution.NOT_FOUND,
                threshold=effective,
                message="place name is empty",
            )

        statuses: list[GeocoderStatus] = []
        candidates: list[PlaceCandidate] = []

        for geocoder in self._ordered(country_code):
            if not geocoder.is_usable():
                statuses.append(
                    GeocoderStatus(
                        geocoder=geocoder.name,
                        state="skipped_no_credentials",
                        message="requires an API key and none is configured",
                    )
                )
                continue
            try:
                found = geocoder.resolve(query, limit=limit)
            except GeocoderError as exc:
                statuses.append(
                    GeocoderStatus(
                        geocoder=geocoder.name, state="failed", message=str(exc)
                    )
                )
                continue

            statuses.append(
                GeocoderStatus(
                    geocoder=geocoder.name,
                    state="ok",
                    candidate_count=len(found),
                )
            )
            if found:
                candidates = found
                # First geocoder with an answer wins. Merging across sources
                # would mean de-duplicating two gazetteers' views of the same
                # village and comparing confidence scores that are not on the
                # same scale -- so the tier order is the decision, not a vote.
                break

        if not statuses:
            return PlaceResolution(
                query=query,
                state=Resolution.UNAVAILABLE,
                threshold=effective,
                message="no geocoder is configured",
            )

        if not candidates:
            failed = [status for status in statuses if status.state == "failed"]
            if failed and not any(status.state == "ok" for status in statuses):
                return PlaceResolution(
                    query=query,
                    state=Resolution.UNAVAILABLE,
                    statuses=statuses,
                    threshold=effective,
                    message=(
                        f"no geocoder could be reached: "
                        f"{'; '.join(status.message for status in failed)}"
                    ),
                )
            return PlaceResolution(
                query=query,
                state=Resolution.NOT_FOUND,
                statuses=statuses,
                threshold=effective,
                message=(
                    f"no place matching {query!r} was found. Try adding "
                    f"administrative context, e.g. 'Newport, Isle of Wight', "
                    f"or pass a bounding box instead."
                ),
            )

        strong = [
            candidate for candidate in candidates if candidate.confidence >= effective
        ]

        if len(strong) == 1:
            return PlaceResolution(
                query=query,
                state=Resolution.RESOLVED,
                # The single strong candidate leads, and the rest are retained
                # so a caller can see what was set aside rather than having to
                # trust that nothing was.
                candidates=[strong[0]]
                + [c for c in candidates if c is not strong[0]],
                statuses=statuses,
                threshold=effective,
            )

        if len(strong) > 1:
            message = (
                f"{len(strong)} places match {query!r} with comparable "
                f"confidence, so this is ambiguous. Pick one by its "
                f"administrative context and pass its bbox directly, or "
                f"re-query with more context."
            )
        else:
            message = (
                f"{len(candidates)} places match {query!r} but none clearly, "
                f"all scoring below the {effective} confidence threshold. "
                f"Review the candidates and pick one rather than assuming the "
                f"first is right."
            )

        return PlaceResolution(
            query=query,
            state=Resolution.AMBIGUOUS,
            candidates=candidates,
            statuses=statuses,
            threshold=effective,
            message=message,
        )

    def _ordered(self, country_code: str | None) -> list[Geocoder]:
        """Geocoders in tier order for a country.

        Country-scoped geocoders first when they apply, then global ones. A
        scoped geocoder that does not serve the country is dropped rather than
        tried and failed.
        """
        scoped: list[Geocoder] = []
        globals_: list[Geocoder] = []
        for geocoder in self.geocoders:
            if geocoder.country_scope:
                if geocoder.serves_country(country_code):
                    scoped.append(geocoder)
            else:
                globals_.append(geocoder)
        return scoped + globals_


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "GeocoderStatus",
    "PlaceResolution",
    "PlaceResolver",
    "Resolution",
]
