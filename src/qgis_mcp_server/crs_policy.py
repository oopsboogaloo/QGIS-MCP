"""Target CRS selection for an area of interest (REQ-12, task 4.3).

``spec/design.md`` §5 explains why this exists at all. Mixed CRS is fine for
display -- QGIS reprojects on the fly -- and it bites in three places:
distance/area analysis, combining rasters, and export to Blender. The first two
are recoverable. The third fails *silently*: at 52°N one degree of longitude
spans about 68.5 km against about 111.2 km for latitude, so a 4326 DEM treated
as a square grid renders terrain stretched east-west by roughly 62%. The output
looks like plausible terrain. It is simply not the shape of that place.

So the target is derived per area of interest, in three tiers:

===== ====================================== ==============================
Tier  Condition                              Target
===== ====================================== ==============================
1     AOI within the UK                      EPSG:27700
2     AOI within a single UTM zone           that UTM zone
3     AOI spans multiple zones               candidates, server does not pick
===== ====================================== ==============================

Tier 3 returns candidates rather than choosing because, per REQ-12, the
equal-area versus conformal decision at continental scale is a cartographic
judgement with a visible effect on the finished map -- not a technical default
the server is entitled to make on someone's behalf.

**Pure Python, no PyQGIS and no PROJ database.** AOI geometry travels as 4326
(§5), tiers 1 and 2 are decidable from a bounding box with arithmetic, and
tier 3 only has to *list* candidates. That keeps the whole policy unit-testable
without starting QGIS. The one thing it cannot do is look up an arbitrary
EPSG code's area of use -- see ``prefer_crs`` on :func:`select_target_crs`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Geographic bounds of EPSG:27700 (OSGB36 / British National Grid), read from
#: the PROJ database shipped with QGIS 4.2 rather than estimated. Verified
#: against ``QgsCoordinateReferenceSystem("EPSG:27700").bounds()`` by a test, so
#: a PROJ update that moves them is reported rather than silently diverging
#: from what this module believes.
UK_BOUNDS = (-9.01, 49.75, 2.01, 61.01)
UK_CRS = "EPSG:27700"
UK_CRS_DESCRIPTION = "OSGB36 / British National Grid"

#: Area of use shared by EPSG:3035 and EPSG:3034, likewise from PROJ.
EUROPE_BOUNDS = (-35.58, 24.6, 44.83, 84.73)

#: The Europe-wide pair offered for a continental European AOI: one equal-area,
#: one conformal. Standard, well-understood codes beat a bespoke PROJ string
#: when one happens to fit.
EUROPE_EQUAL_AREA = ("EPSG:3035", "ETRS89-extended / LAEA Europe")
EUROPE_CONFORMAL = ("EPSG:3034", "ETRS89-extended / LCC Europe")

#: UTM covers to 84°N and 80°S; beyond that the zones do not apply.
UTM_MAX_LATITUDE = 84.0
UTM_MIN_LATITUDE = -80.0

_EQUAL_AREA_TRADE_OFF = (
    "preserves area, so measured areas and area-weighted statistics are "
    "correct; distorts shape and angles increasingly far from the centre"
)
_CONFORMAL_TRADE_OFF = (
    "preserves local shape and angles, so coastlines and buildings look right; "
    "distorts area, increasingly far from the standard parallels"
)
_ZONAL_TRADE_OFF = (
    "accurate within its own zone and the usual choice for local work, but the "
    "AOI extends beyond it -- everything outside the zone is progressively "
    "stretched"
)


class AoiError(ValueError):
    """The area of interest is malformed or outside what the policy can serve."""


@dataclass(frozen=True)
class RasterInput:
    """One raster being brought into a project, for the resampling refinement."""

    name: str
    crs: str
    resolution_m: float


@dataclass(frozen=True)
class CrsCandidate:
    """One target a caller could choose, with its trade-off stated."""

    crs: str
    description: str
    kind: str
    trade_off: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "crs": self.crs,
            "description": self.description,
            "kind": self.kind,
            "trade_off": self.trade_off,
        }


@dataclass(frozen=True)
class CrsSelection:
    """The chosen target, or the candidates when choosing is not the server's call."""

    tier: int
    reason: str
    crs: str | None = None
    description: str | None = None
    candidates: tuple[CrsCandidate, ...] = ()
    utm_zones: tuple[int, ...] = ()
    resampling: dict[str, Any] | None = None
    #: Set when the AOI is a single point or a degenerate line. The policy still
    #: answers, because a zero-area AOI has a perfectly good target CRS.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def requires_choice(self) -> bool:
        """True when the caller must pick, per REQ-12's tier 3."""
        return self.crs is None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tier": self.tier,
            "reason": self.reason,
            "requires_choice": self.requires_choice,
        }
        if self.crs is not None:
            payload["crs"] = self.crs
            payload["description"] = self.description
        if self.candidates:
            payload["candidates"] = [c.to_dict() for c in self.candidates]
        if self.utm_zones:
            payload["utm_zones"] = list(self.utm_zones)
        if self.resampling is not None:
            payload["resampling"] = self.resampling
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


def utm_zone(longitude: float) -> int:
    """UTM zone number containing ``longitude``. 1 to 60."""
    zone = int(math.floor((longitude + 180.0) / 6.0)) + 1
    # lon == 180 lands on zone 61, which does not exist; it belongs to 60.
    return min(max(zone, 1), 60)


def utm_epsg(zone: int, *, northern: bool) -> str:
    """EPSG code for a WGS 84 UTM zone."""
    return f"EPSG:{(32600 if northern else 32700) + zone}"


def _validate(aoi: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Check an AOI bounding box in EPSG:4326, as ``(min_lon, min_lat, max_lon, max_lat)``."""
    if aoi is None or len(tuple(aoi)) != 4:
        raise AoiError(
            "area of interest must be a 4-tuple (min_lon, min_lat, max_lon, "
            "max_lat) in EPSG:4326"
        )
    min_lon, min_lat, max_lon, max_lat = (float(value) for value in aoi)

    for name, value in (
        ("min_lon", min_lon),
        ("max_lon", max_lon),
    ):
        if not -180.0 <= value <= 180.0:
            raise AoiError(f"{name}={value} is outside the valid range -180..180")
    for name, value in (("min_lat", min_lat), ("max_lat", max_lat)):
        if not -90.0 <= value <= 90.0:
            raise AoiError(f"{name}={value} is outside the valid range -90..90")

    if min_lon > max_lon:
        # Rather than silently normalise: a reversed bbox is as likely to be an
        # antimeridian crossing as a typo, and those need opposite handling.
        raise AoiError(
            f"min_lon={min_lon} is greater than max_lon={max_lon}. If this AOI "
            f"crosses the antimeridian, split it into two and select a CRS for "
            f"each -- no single projected CRS serves both sides well."
        )
    if min_lat > max_lat:
        raise AoiError(f"min_lat={min_lat} is greater than max_lat={max_lat}")

    return min_lon, min_lat, max_lon, max_lat


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    """Whether ``outer`` fully contains ``inner``."""
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _zone_span(min_lon: float, max_lon: float) -> tuple[int, ...]:
    """The UTM zones an east-west span touches.

    A bbox whose eastern edge lies exactly on a zone boundary does not actually
    enter the next zone, so the boundary case is excluded rather than rounded
    into an extra zone -- otherwise an AOI drawn deliberately to one zone's
    edge would be pushed into tier 3 and asked to choose for no reason.
    """
    low = utm_zone(min_lon)
    high = utm_zone(max_lon)
    if high > low and math.isclose((max_lon + 180.0) % 6.0, 0.0, abs_tol=1e-9):
        high -= 1
    return tuple(range(low, high + 1))


def _hemisphere(min_lat: float, max_lat: float) -> str | None:
    """``"north"``, ``"south"``, or ``None`` when the AOI straddles the equator."""
    if min_lat >= 0.0:
        return "north"
    if max_lat <= 0.0:
        return "south"
    return None


def _centred_projections(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> list[CrsCandidate]:
    """An equal-area and a conformal projection centred on the AOI.

    REQ-12 asks tier 3 for "an equal-area or conformal projection centred on
    the AOI", which no fixed EPSG code provides for an arbitrary area -- so
    these are PROJ strings. The conformal one puts its standard parallels at
    one sixth and five sixths of the latitude range, the usual construction for
    minimising distortion across a Lambert Conformal Conic's extent.
    """
    centre_lon = (min_lon + max_lon) / 2.0
    centre_lat = (min_lat + max_lat) / 2.0
    span = max_lat - min_lat
    parallel_1 = min_lat + span / 6.0
    parallel_2 = max_lat - span / 6.0

    laea = (
        f"+proj=laea +lat_0={centre_lat:.4f} +lon_0={centre_lon:.4f} "
        f"+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    lcc = (
        f"+proj=lcc +lat_1={parallel_1:.4f} +lat_2={parallel_2:.4f} "
        f"+lat_0={centre_lat:.4f} +lon_0={centre_lon:.4f} "
        f"+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    return [
        CrsCandidate(
            crs=laea,
            description=(
                f"Lambert Azimuthal Equal Area centred on "
                f"{centre_lat:.4f}, {centre_lon:.4f}"
            ),
            kind="equal_area",
            trade_off=_EQUAL_AREA_TRADE_OFF,
        ),
        CrsCandidate(
            crs=lcc,
            description=(
                f"Lambert Conformal Conic centred on "
                f"{centre_lat:.4f}, {centre_lon:.4f}, standard parallels "
                f"{parallel_1:.4f} and {parallel_2:.4f}"
            ),
            kind="conformal",
            trade_off=_CONFORMAL_TRADE_OFF,
        ),
    ]


def _resampling_report(
    target: str | None, inputs: tuple[RasterInput, ...]
) -> dict[str, Any] | None:
    """Which raster gets resampled, and whether it is the one that matters.

    ``design.md`` §5 calls this "the refinement that matters more than the
    tiers": something always gets resampled, and it should be the 30 m global
    DEM, not the 5 m national one. This reports the outcome so a caller can
    see it; for UK work it falls out for free, since OS Terrain 5 is already in
    EPSG:27700.
    """
    if not inputs:
        return None
    finest = min(inputs, key=lambda item: item.resolution_m)
    will_resample = target is None or finest.crs != target
    report: dict[str, Any] = {
        "highest_resolution_input": finest.name,
        "its_crs": finest.crs,
        "resolution_m": finest.resolution_m,
        "will_be_resampled": will_resample,
    }
    if will_resample and target is not None:
        report["note"] = (
            f"{finest.name} is the highest-resolution input at "
            f"{finest.resolution_m} m and is in {finest.crs}, so reprojecting "
            f"to {target} will resample it. If another candidate CRS matches "
            f"{finest.crs} and covers the AOI, prefer that instead."
        )
    elif not will_resample:
        report["note"] = (
            f"{finest.name} is already in {target}, so the highest-resolution "
            f"input is not resampled."
        )
    return report


def select_target_crs(
    aoi: tuple[float, float, float, float],
    *,
    inputs: tuple[RasterInput, ...] | list[RasterInput] = (),
    prefer_crs: str | None = None,
) -> CrsSelection:
    """Derive the target CRS for ``aoi``, given in EPSG:4326.

    Args:
        aoi: ``(min_lon, min_lat, max_lon, max_lat)``.
        inputs: Rasters being brought in, used to report which one gets
            resampled.
        prefer_crs: A projected CRS to adopt if the tier would otherwise pick
            something else.

            This is the escape hatch for §5's highest-resolution-raster
            refinement in its general form. Deciding it properly needs to know
            whether a candidate CRS's area of use covers the AOI, which means a
            PROJ lookup -- and this module is deliberately free of one so that
            the tier logic stays pure and fast to test. The caller that *does*
            have PyQGIS available (layer registration, task 4.7) resolves
            coverage there and passes the answer in. For UK work it is never
            needed: tier 1 already lands on EPSG:27700.

    Raises:
        AoiError: the AOI is malformed, reversed, or crosses the antimeridian.
    """
    min_lon, min_lat, max_lon, max_lat = _validate(aoi)
    resolved_inputs = tuple(inputs)
    notes: list[str] = []

    if min_lon == max_lon or min_lat == max_lat:
        notes.append(
            "the AOI has zero width or height; a target CRS is still "
            "well-defined, but check the AOI is what you intended"
        )

    # --- Tier 1: the UK -------------------------------------------------
    if _contains(UK_BOUNDS, (min_lon, min_lat, max_lon, max_lat)):
        target = prefer_crs or UK_CRS
        return CrsSelection(
            tier=1,
            crs=target,
            description=UK_CRS_DESCRIPTION if target == UK_CRS else None,
            reason=(
                f"the AOI lies within the United Kingdom, so the national grid "
                f"{UK_CRS} applies"
                + ("" if target == UK_CRS else f"; overridden to {target} by the caller")
            ),
            resampling=_resampling_report(target, resolved_inputs),
            notes=tuple(notes),
        )

    # --- Tier 2: a single UTM zone --------------------------------------
    zones = _zone_span(min_lon, max_lon)
    hemisphere = _hemisphere(min_lat, max_lat)
    beyond_utm = max_lat > UTM_MAX_LATITUDE or min_lat < UTM_MIN_LATITUDE

    if len(zones) == 1 and hemisphere is not None and not beyond_utm:
        zone = zones[0]
        default = utm_epsg(zone, northern=hemisphere == "north")
        target = prefer_crs or default
        return CrsSelection(
            tier=2,
            crs=target,
            description=(
                f"WGS 84 / UTM zone {zone}{'N' if hemisphere == 'north' else 'S'}"
                if target == default
                else None
            ),
            reason=(
                f"the AOI lies within a single UTM zone ({zone}"
                f"{'N' if hemisphere == 'north' else 'S'}), so that zone applies"
                + ("" if target == default else f"; overridden to {target} by the caller")
            ),
            utm_zones=zones,
            resampling=_resampling_report(target, resolved_inputs),
            notes=tuple(notes),
        )

    # --- Tier 3: the server does not choose -----------------------------
    #
    # But the *caller* may, and that is the point of the parameter. REQ-12
    # forbids the server selecting a projection automatically at this scale; it
    # does not forbid honouring a decision that has been made. Without this,
    # passing a candidate back would be refused the same way as not choosing at
    # all, leaving the question unanswerable.
    candidates: list[CrsCandidate] = []

    if not beyond_utm and hemisphere is not None:
        northern = hemisphere == "north"
        for zone in zones:
            candidates.append(
                CrsCandidate(
                    crs=utm_epsg(zone, northern=northern),
                    description=(
                        f"WGS 84 / UTM zone {zone}{'N' if northern else 'S'}"
                    ),
                    kind="zonal",
                    trade_off=_ZONAL_TRADE_OFF,
                )
            )

    if _contains(EUROPE_BOUNDS, (min_lon, min_lat, max_lon, max_lat)):
        candidates.append(
            CrsCandidate(
                crs=EUROPE_EQUAL_AREA[0],
                description=EUROPE_EQUAL_AREA[1],
                kind="equal_area",
                trade_off=_EQUAL_AREA_TRADE_OFF,
            )
        )
        candidates.append(
            CrsCandidate(
                crs=EUROPE_CONFORMAL[0],
                description=EUROPE_CONFORMAL[1],
                kind="conformal",
                trade_off=_CONFORMAL_TRADE_OFF,
            )
        )

    candidates.extend(_centred_projections(min_lon, min_lat, max_lon, max_lat))

    if beyond_utm:
        notes.append(
            "the AOI reaches beyond UTM's 84°N/80°S limit, so no UTM zone "
            "serves it; a polar stereographic projection may suit better than "
            "any candidate listed here"
        )
    if hemisphere is None:
        notes.append(
            "the AOI straddles the equator, so northern and southern UTM zones "
            "both apply in part and neither serves the whole area"
        )

    if prefer_crs:
        return CrsSelection(
            tier=3,
            crs=prefer_crs,
            reason=(
                f"the AOI spans {len(zones)} UTM zones, so the server would not "
                f"choose a projection -- {prefer_crs} was supplied by the caller"
            ),
            # Kept so the response still shows what the alternatives were, and
            # what was given up by choosing this one.
            candidates=tuple(candidates),
            utm_zones=zones,
            resampling=_resampling_report(prefer_crs, resolved_inputs),
            notes=tuple(notes),
        )

    return CrsSelection(
        tier=3,
        crs=None,
        reason=(
            f"the AOI spans {len(zones)} UTM zones, so no single zone serves it. "
            f"Choosing between an equal-area and a conformal projection at this "
            f"scale changes the finished map visibly, so the decision is yours "
            f"-- pick a candidate and pass it as the target."
        ),
        candidates=tuple(candidates),
        utm_zones=zones,
        resampling=_resampling_report(None, resolved_inputs),
        notes=tuple(notes),
    )


__all__ = [
    "EUROPE_BOUNDS",
    "UK_BOUNDS",
    "UK_CRS",
    "UTM_MAX_LATITUDE",
    "UTM_MIN_LATITUDE",
    "AoiError",
    "CrsCandidate",
    "CrsSelection",
    "RasterInput",
    "select_target_crs",
    "utm_epsg",
    "utm_zone",
]
