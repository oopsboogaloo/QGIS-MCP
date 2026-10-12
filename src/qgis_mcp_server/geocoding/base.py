"""Place resolution types and the geocoder interface (task 4.1).

REQ-9 governs the contract: a place string resolves automatically when exactly
one candidate clears the confidence threshold, and otherwise comes back as an
enumerated candidate list with types and administrative context -- never a
guess. The rationale is that "ambiguity should cost one extra round-trip rather
than silently producing data for a similarly-named place on another continent",
and the three candidate ``Newport``s in the UK alone make that concrete.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

#: Below this span in degrees, a resolved place is too small to acquire
#: useful data for. See PlaceCandidate.is_small_for_an_aoi.
SMALL_AOI_DEGREES = 0.005


@dataclass(frozen=True)
class PlaceCandidate:
    """One possible meaning of a place name.

    The bounding box is the payload that matters: it becomes the AOI for
    everything downstream, so it is always ``(min_lon, min_lat, max_lon,
    max_lat)`` in EPSG:4326 -- the same order ``clip_to_aoi`` and
    ``crs_policy`` use. Geocoders order their own boxes differently and each
    adapter converts at its own boundary.
    """

    name: str
    display_name: str
    #: What kind of place: ``city``, ``village``, ``administrative``, ``suburb``.
    place_type: str
    #: Where it is, in human terms -- "Hampshire, England, United Kingdom".
    #: REQ-9 requires this: it is what lets a caller tell two Newports apart.
    admin_context: str
    latitude: float
    longitude: float
    bbox: tuple[float, float, float, float]
    #: 0..1. Comparable within one source; not across sources.
    confidence: float
    source: str
    country_code: str = ""
    #: A real boundary polygon as GeoJSON, when the geocoder returns one.
    #: Worth carrying: clipping a city's streets to its actual built-up
    #: boundary rather than its bounding rectangle is the difference between a
    #: render of a city and a render of a city plus surrounding farmland.
    geometry: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_small_for_an_aoi(self) -> bool:
        """Whether the bbox is too small to acquire useful data for.

        Calibrated against what the AOI is *for* rather than against a notion
        of degeneracy. The threshold is about 0.005 degrees -- roughly 550 m --
        because the elevation data this feeds is 30 m per pixel, so a box that
        size is some 18 pixels across. Any smaller and a download succeeds and
        returns almost nothing, which reads as a broken pipeline rather than
        as an AOI that was never big enough.

        Real example from the fixtures: "Stonehenge" resolves to a 140 m by
        120 m archaeological site. A perfectly correct answer to the question
        asked, and useless as an area to fetch terrain for.
        """
        min_lon, min_lat, max_lon, max_lat = self.bbox
        return (max_lon - min_lon) < SMALL_AOI_DEGREES and (
            max_lat - min_lat
        ) < SMALL_AOI_DEGREES

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "display_name": self.display_name,
            "place_type": self.place_type,
            "admin_context": self.admin_context,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "bbox": list(self.bbox),
            "bbox_crs": "EPSG:4326",
            "confidence": round(self.confidence, 4),
            "source": self.source,
        }
        if self.country_code:
            payload["country_code"] = self.country_code
        if self.geometry is not None:
            payload["has_boundary_polygon"] = True
            payload["geometry_type"] = self.geometry.get("type")
        if self.is_small_for_an_aoi:
            payload["warning"] = (
                "this resolves to an area under roughly 550 m across -- about "
                "18 pixels of 30 m elevation data. Acquiring for it will "
                "succeed and return almost nothing. Pass an explicit bbox, or "
                "choose the settlement or administrative area containing it."
            )
        if self.extra:
            payload["extra"] = self.extra
        return payload


class GeocoderError(Exception):
    """A geocoder could not be reached, or returned something unusable."""


class Geocoder(ABC):
    """One place-name resolution service."""

    #: Short identifier, used in candidate ``source`` and in status reporting.
    name: str = "geocoder"
    #: Whether this geocoder needs an API key.
    requires_credentials: bool = False
    #: Restrict to these ISO country codes, or empty for worldwide.
    country_scope: tuple[str, ...] = ()

    def is_usable(self) -> bool:
        """Whether this geocoder could be called right now."""
        return True

    def serves_country(self, country_code: str | None) -> bool:
        """Whether this geocoder is appropriate for a country.

        Used for the provider tiering of §4.6.1: UK-specific gazetteers give
        better names within the UK, and nothing at all outside it.
        """
        if not self.country_scope:
            return True
        if not country_code:
            return False
        return country_code.lower() in {code.lower() for code in self.country_scope}

    @abstractmethod
    def resolve(self, place: str, *, limit: int = 10) -> list[PlaceCandidate]:
        """Candidates for ``place``, best first.

        Raises :class:`GeocoderError` on anything that is not a clean answer.
        The resolver turns that into a per-geocoder status rather than a failed
        call, for the same reason REQ-10 does for data providers.
        """


__all__ = ["SMALL_AOI_DEGREES", "Geocoder", "GeocoderError", "PlaceCandidate"]
