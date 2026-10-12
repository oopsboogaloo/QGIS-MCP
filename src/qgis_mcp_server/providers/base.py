"""The provider adapter interface (task 4.0, per ``spec/design.md`` §4).

Five v1 providers share almost no interface -- a REST GET, a static catalogue of
zipped downloads, an Overpass query language, and OGC WMS/WFS -- so each
declares what it can do and implements two operations:

* ``search(aoi, types) -> [Dataset]``
* ``fetch(dataset, aoi, dest) -> path``

Adapters inherit from a protocol-family base (``RestApiAdapter``,
``StaticCatalogueAdapter``, ``OverpassAdapter``, ``OgcAdapter``) so each
protocol is written once. Within an existing family a new provider genuinely is
close to a config entry; a provider introducing a new protocol shape needs a
new base class. The claim that was wrong, and is worth not reintroducing, is
that *every* new provider is config-only.

**Licence tier sits in the capability declaration**, alongside coverage and
credentials. The spec puts licence reporting in ``get_dataset_licence_info``
(§4.6) and this does not replace it -- the dedicated tool still answers the
detailed question. But the tier has to be *declared* somewhere for that tool to
read, and declaring it here means REQ-4d's confirmation gate can state the
licence tier before a download rather than after, which is what REQ-4d asks
for. Retrofitting it across five adapters later would be the harder path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qgis_mcp_server.credentials import CredentialSpec, CredentialStatus, CredentialStore


class DataType:
    """What a provider can supply, as the vocabulary ``search`` matches on."""

    ELEVATION = "elevation"
    BUILDINGS = "buildings"
    ROADS = "roads"
    ADMIN_BOUNDARIES = "admin_boundaries"
    PLACE_NAMES = "place_names"
    GEOLOGY = "geology"
    HYDROLOGY = "hydrology"
    LANDCOVER = "landcover"
    BASEMAP = "basemap"
    COASTLINE = "coastline"

    ALL = (
        ELEVATION,
        BUILDINGS,
        ROADS,
        ADMIN_BOUNDARIES,
        PLACE_NAMES,
        GEOLOGY,
        HYDROLOGY,
        LANDCOVER,
        BASEMAP,
        COASTLINE,
    )


class LicenceTier:
    """How freely a dataset can be used, coarsely enough to act on.

    Deliberately coarse. The purpose is to let REQ-4d's confirmation say
    something true and useful before a download -- "this is ODbL, your output
    will carry a share-alike obligation" -- not to be legal advice.
    ``get_dataset_licence_info`` carries the specifics.
    """

    #: No restrictions worth stating. Natural Earth.
    PUBLIC_DOMAIN = "public_domain"
    #: Free to use, attribution required. OGL, CC-BY.
    OPEN_ATTRIBUTION = "open_attribution"
    #: Free to use, attribution *and* share-alike. ODbL.
    OPEN_SHARE_ALIKE = "open_share_alike"
    #: Free for non-commercial use; commercial use needs a different agreement.
    FREE_NON_COMMERCIAL = "free_non_commercial"
    #: Varies by product within the same provider -- the caller must check per
    #: dataset rather than trusting a provider-level answer.
    MIXED = "mixed"

    #: Tiers that may not cover paid client work without further checking.
    NEEDS_COMMERCIAL_CHECK = (FREE_NON_COMMERCIAL, OPEN_SHARE_ALIKE, MIXED)


@dataclass(frozen=True)
class LicenceDeclaration:
    """A provider's licence position, and what it obliges a user to do."""

    tier: str
    summary: str
    #: What must appear in a published output. Empty when nothing need be.
    attribution: str = ""
    #: Set when the tier alone understates a restriction that bites.
    commercial_note: str = ""
    url: str = ""

    @property
    def needs_commercial_check(self) -> bool:
        return self.tier in LicenceTier.NEEDS_COMMERCIAL_CHECK

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tier": self.tier, "summary": self.summary}
        if self.attribution:
            payload["attribution"] = self.attribution
        if self.commercial_note:
            payload["commercial_note"] = self.commercial_note
        if self.url:
            payload["url"] = self.url
        payload["needs_commercial_check"] = self.needs_commercial_check
        return payload


@dataclass(frozen=True)
class ProviderCapability:
    """Everything known about a provider without making a network call.

    Coverage is declared so the dispatcher can skip a provider before any
    traffic -- no querying OS Data Hub for a bounding box in Ontario -- and so
    the server can answer "who could serve geology here" with no requests at
    all.
    """

    name: str
    long_name: str
    protocol_family: str
    data_types: tuple[str, ...]
    licence: LicenceDeclaration
    #: AOI coverage as a 4326 bbox, or ``None`` for global.
    coverage: tuple[float, float, float, float] | None = None
    coverage_description: str = "global"
    credential: CredentialSpec | None = None
    #: Stated separately from ``credential`` because a provider can need a key
    #: for some products and none for others.
    requires_credentials: bool = False
    notes: str = ""

    def covers(self, aoi: tuple[float, float, float, float]) -> bool:
        """Whether this provider could serve ``aoi``, declared in EPSG:4326.

        Intersection, not containment: a provider covering half the requested
        area still has something to offer, and ruling it out would hide real
        results. Whether partial coverage is useful is the caller's call.
        """
        if self.coverage is None:
            return True
        min_lon, min_lat, max_lon, max_lat = aoi
        c_min_lon, c_min_lat, c_max_lon, c_max_lat = self.coverage
        return not (
            max_lon < c_min_lon
            or min_lon > c_max_lon
            or max_lat < c_min_lat
            or min_lat > c_max_lat
        )

    def serves(self, data_types: tuple[str, ...] | list[str] | None) -> bool:
        """Whether this provider supplies any of ``data_types``."""
        if not data_types:
            return True
        return any(wanted in self.data_types for wanted in data_types)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "long_name": self.long_name,
            "protocol_family": self.protocol_family,
            "data_types": list(self.data_types),
            "coverage": list(self.coverage) if self.coverage else None,
            "coverage_description": self.coverage_description,
            "requires_credentials": self.requires_credentials,
            "licence": self.licence.to_dict(),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Dataset:
    """One offering from a provider, as ``search`` returns it."""

    provider: str
    identifier: str
    title: str
    data_type: str
    licence: LicenceDeclaration
    #: Native CRS as an authority code, when the provider states one.
    crs: str | None = None
    #: Ground resolution in metres for raster products.
    resolution_m: float | None = None
    #: Best estimate of download size, for REQ-4d's confirmation.
    estimated_size_bytes: int | None = None
    format: str | None = None
    attribution: str = ""
    #: Tie-break among datasets of equal resolution, lower first.
    #:
    #: First-class rather than buried in ``extra`` because the dispatcher sorts
    #: on it: the leading entry in a menu is what gets taken, and an adapter
    #: that has a reason to rank one product over another of the same
    #: resolution needs that reason to survive the merge across providers.
    preference: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "identifier": self.identifier,
            "title": self.title,
            "data_type": self.data_type,
            "licence": self.licence.to_dict(),
        }
        for key in ("crs", "resolution_m", "estimated_size_bytes", "format"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.attribution:
            payload["attribution"] = self.attribution
        if self.extra:
            payload["extra"] = self.extra
        return payload


class ProviderAdapter(ABC):
    """One data provider, behind a single interface.

    ``search`` and ``fetch`` arrive per adapter in tasks 4.4 and 4.8. The
    capability declaration is available now, and is what
    ``check_api_credentials`` and the dispatcher's coverage pre-filtering read.
    """

    #: Declared by each subclass.
    capability: ProviderCapability

    def __init__(self, credentials: CredentialStore | None = None) -> None:
        self.credentials = credentials or CredentialStore()

    @property
    def name(self) -> str:
        return self.capability.name

    def credential_status(self) -> CredentialStatus | None:
        """This provider's credential status, or ``None`` if it needs none."""
        if self.capability.credential is None:
            return None
        return self.credentials.status(self.capability.credential)

    def is_usable(self) -> bool:
        """Whether this adapter could make a request right now.

        REQ-10 makes this a filter rather than a gate: an unusable adapter is
        reported alongside the successful ones with its status attached, never
        allowed to fail the whole menu.
        """
        if not self.capability.requires_credentials:
            return True
        status = self.credential_status()
        return bool(status and status.present)

    @abstractmethod
    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """Datasets this provider offers for ``aoi``."""

    @abstractmethod
    def fetch(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        destination: Path,
    ) -> Path:
        """Download ``dataset`` for ``aoi`` to ``destination``."""


class RestApiAdapter(ProviderAdapter, ABC):
    """Providers whose search and fetch are parameterised HTTP GETs."""

    protocol_family = "rest"


class StaticCatalogueAdapter(ProviderAdapter, ABC):
    """Providers with no query API: a fixed catalogue of whole-file downloads.

    "Search" is filtering a static manifest, and because there is no
    server-side bbox filtering, files are fetched whole and clipped locally.
    """

    protocol_family = "static_catalogue"


class OverpassAdapter(ProviderAdapter, ABC):
    """Providers queried through Overpass QL.

    ``design.md`` §7 flags this as the acquisition layer's most likely source
    of real work beyond the interface: large AOIs time out or are rejected, so
    this family needs tiling and result merging.
    """

    protocol_family = "overpass"


class OgcAdapter(ProviderAdapter, ABC):
    """Providers exposing OGC WMS/WFS -- GetCapabilities then GetFeature."""

    protocol_family = "ogc"


__all__ = [
    "Dataset",
    "DataType",
    "LicenceDeclaration",
    "LicenceTier",
    "OgcAdapter",
    "OverpassAdapter",
    "ProviderAdapter",
    "ProviderCapability",
    "RestApiAdapter",
    "StaticCatalogueAdapter",
]
