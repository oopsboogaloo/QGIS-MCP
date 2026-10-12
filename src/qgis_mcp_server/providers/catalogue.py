"""The v1 provider capability declarations (task 4.0).

Declarations only. ``search`` and ``fetch`` land per adapter in tasks 4.4 and
4.8; what is here is everything knowable without a network call, which is what
``check_api_credentials`` reads and what the dispatcher's coverage pre-filtering
needs.

Coverage bboxes for the UK providers use the geographic bounds of EPSG:27700
from the PROJ database, rather than a hand-drawn rectangle -- the same source
``crs_policy`` uses for its tier-1 test, so the two cannot disagree about where
the UK is.

**Quotas and commercial terms are recorded where they change decisions.**
OpenTopography's free key allows 50 calls per 24 hours for non-academic use,
which is why task 4.7 keeps live fetching to an opt-in integration test rather
than the default pass -- a live fetch in every test run would exhaust a day's
allowance in one sitting. And its free key does not cover commercial
integration, which matters directly for the commission work in §2's use case 1.
"""

from __future__ import annotations

from qgis_mcp_server.credentials import CredentialSpec
from qgis_mcp_server.crs_policy import UK_BOUNDS
from qgis_mcp_server.providers.base import (
    DataType,
    LicenceDeclaration,
    LicenceTier,
    ProviderCapability,
)

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

OS_DATA_HUB_CREDENTIAL = CredentialSpec(
    provider="os_data_hub",
    config_key="os_data_hub",
    env_var="QGIS_MCP_OS_DATA_HUB_KEY",
    registration_url="https://osdatahub.os.uk/",
    notes=(
        "Sign up, then API Dashboard -> My Projects -> Create a new project -> "
        "Add an API to this project. The key is per project. Enable at least "
        "OS Names (UK place lookup) and OS Features (buildings and streets); "
        "add OS Maps for raster basemap tiles. Free allowance covers a set "
        "monthly value of premium transactions; OS OpenData products are OGL."
    ),
)

OPENTOPOGRAPHY_CREDENTIAL = CredentialSpec(
    provider="opentopography",
    config_key="opentopography",
    env_var="QGIS_MCP_OPENTOPOGRAPHY_KEY",
    registration_url="https://portal.opentopography.org/newUser",
    notes=(
        "Create an account, then MyOpenTopo Dashboard -> Get an API Key -> "
        "Request API key. Issued immediately. The free non-academic allowance "
        "is 50 Global DEM calls per 24 hours (200 for academics), and a single "
        "request is capped at 450,000 km2 for the 30 m datasets. Commercial "
        "integration requires a separate Enterprise key."
    ),
)

ALL_CREDENTIALS = (OS_DATA_HUB_CREDENTIAL, OPENTOPOGRAPHY_CREDENTIAL)


# ---------------------------------------------------------------------------
# Licences
# ---------------------------------------------------------------------------

OGL_LICENCE = LicenceDeclaration(
    tier=LicenceTier.OPEN_ATTRIBUTION,
    summary="UK Open Government Licence: free to use and adapt, with attribution.",
    attribution="Contains OS data (c) Crown copyright and database right",
    url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
)

OS_DATA_HUB_LICENCE = LicenceDeclaration(
    tier=LicenceTier.MIXED,
    summary=(
        "OS OpenData products are OGL; premium products carry OS Data Hub "
        "commercial terms and draw on the account's transaction allowance."
    ),
    attribution="Contains OS data (c) Crown copyright and database right",
    commercial_note=(
        "Which tier a product falls under varies per product, so check the "
        "specific dataset before using it in paid client work rather than "
        "relying on this provider-level answer."
    ),
    url="https://osdatahub.os.uk/legal",
)

OPENTOPOGRAPHY_LICENCE = LicenceDeclaration(
    tier=LicenceTier.MIXED,
    summary=(
        "Per-dataset: Copernicus and SRTM derivatives are generally open with "
        "attribution, but terms vary by source dataset."
    ),
    attribution="Sourced via OpenTopography; cite the underlying DEM dataset",
    commercial_note=(
        "The free API key does not cover commercial integration -- that needs "
        "a separate Enterprise key on request. Confirm before using fetched "
        "elevation in a paid deliverable."
    ),
    url="https://opentopography.org/usageterms",
)

ODBL_LICENCE = LicenceDeclaration(
    tier=LicenceTier.OPEN_SHARE_ALIKE,
    summary=(
        "Open Database Licence: free to use and adapt, with attribution, and "
        "derived databases must be shared under the same terms."
    ),
    attribution="(c) OpenStreetMap contributors",
    commercial_note=(
        "Commercial use is permitted. The obligation is share-alike: a "
        "*database* derived from OSM must be offered under ODbL. Produced maps "
        "and images are generally a Produced Work and need only attribution."
    ),
    url="https://www.openstreetmap.org/copyright",
)

PUBLIC_DOMAIN_LICENCE = LicenceDeclaration(
    tier=LicenceTier.PUBLIC_DOMAIN,
    summary="Public domain. No restrictions, no attribution required.",
    url="https://www.naturalearthdata.com/about/terms-of-use/",
)

BGS_LICENCE = LicenceDeclaration(
    tier=LicenceTier.MIXED,
    summary=(
        "BGS digital data: the 1:625,000-scale products are available under "
        "OGL; more detailed scales are licensed commercially."
    ),
    attribution="Contains British Geological Survey materials (c) UKRI",
    commercial_note=(
        "The free tier stops at 1:625,000. Anything more detailed needs a BGS "
        "licence agreement."
    ),
    url="https://www.bgs.ac.uk/geological-data/licensing/",
)


# ---------------------------------------------------------------------------
# Provider declarations
# ---------------------------------------------------------------------------

OS_DATA_HUB = ProviderCapability(
    name="os_data_hub",
    long_name="Ordnance Survey Data Hub",
    protocol_family="rest",
    data_types=(
        DataType.ELEVATION,
        DataType.BUILDINGS,
        DataType.ROADS,
        DataType.ADMIN_BOUNDARIES,
        DataType.PLACE_NAMES,
        DataType.BASEMAP,
    ),
    licence=OS_DATA_HUB_LICENCE,
    coverage=UK_BOUNDS,
    coverage_description="United Kingdom",
    credential=OS_DATA_HUB_CREDENTIAL,
    requires_credentials=True,
    notes=(
        "OS Terrain 50 (OGL) and OS Terrain 5 (premium) for elevation; OS "
        "Features for buildings and streets; OS Names for place lookup. "
        "Already in EPSG:27700, which is why UK work needs no DEM resampling."
    ),
)

OPENTOPOGRAPHY = ProviderCapability(
    name="opentopography",
    long_name="OpenTopography Global Datasets",
    protocol_family="rest",
    data_types=(DataType.ELEVATION,),
    licence=OPENTOPOGRAPHY_LICENCE,
    coverage=None,
    coverage_description="global, between roughly 84 degrees N and 80 degrees S",
    credential=OPENTOPOGRAPHY_CREDENTIAL,
    requires_credentials=True,
    notes=(
        "Primary elevation source for the terrain pipeline: Copernicus GLO-30, "
        "SRTM GL1/GL3, ALOS World 3D, NASADEM. Served in EPSG:4326, so a "
        "reprojection is unavoidable -- which is exactly why REQ-12 prefers "
        "leaving the higher-resolution national DEM unresampled instead. "
        "Free non-academic allowance: 50 calls per 24 hours."
    ),
)

NATURAL_EARTH = ProviderCapability(
    name="natural_earth",
    long_name="Natural Earth",
    protocol_family="static_catalogue",
    data_types=(
        DataType.ADMIN_BOUNDARIES,
        DataType.COASTLINE,
        DataType.HYDROLOGY,
        DataType.ROADS,
        DataType.PLACE_NAMES,
    ),
    licence=PUBLIC_DOMAIN_LICENCE,
    coverage=None,
    coverage_description="global, at 1:10m, 1:50m and 1:110m scales",
    requires_credentials=False,
    notes=(
        "No query API: a fixed catalogue of zipped downloads by scale and "
        "theme. There is no server-side bbox filtering, so files are fetched "
        "whole and clipped locally -- which makes clip_to_aoi a dependency "
        "rather than a convenience here."
    ),
)

OPENSTREETMAP = ProviderCapability(
    name="openstreetmap",
    long_name="OpenStreetMap via Overpass API",
    protocol_family="overpass",
    data_types=(
        DataType.BUILDINGS,
        DataType.ROADS,
        DataType.PLACE_NAMES,
        DataType.HYDROLOGY,
        DataType.LANDCOVER,
    ),
    licence=ODBL_LICENCE,
    coverage=None,
    coverage_description="global",
    requires_credentials=False,
    notes=(
        "Needs Overpass QL generated per data type, and Overpass JSON "
        "converted to features. Large AOIs time out or are rejected and need "
        "tiling with result merging (design.md section 7). Overpass "
        "rate-limits, so concurrency must be bounded per provider."
    ),
)

BGS = ProviderCapability(
    name="bgs",
    long_name="British Geological Survey",
    protocol_family="ogc",
    data_types=(DataType.GEOLOGY,),
    licence=BGS_LICENCE,
    coverage=UK_BOUNDS,
    coverage_description="United Kingdom",
    requires_credentials=False,
    notes=(
        "OGC WMS/WFS: GetCapabilities to discover, GetFeature to fetch. CRS "
        "varies by service, so the CRS policy matters more here than for the "
        "providers that serve a single consistent projection."
    ),
)

#: Every v1 provider. Order is stable so responses are reproducible.
ALL_PROVIDERS = (
    OS_DATA_HUB,
    OPENTOPOGRAPHY,
    NATURAL_EARTH,
    OPENSTREETMAP,
    BGS,
)

PROVIDERS_BY_NAME = {provider.name: provider for provider in ALL_PROVIDERS}


def providers_for(
    aoi: tuple[float, float, float, float] | None = None,
    data_types: tuple[str, ...] | list[str] | None = None,
) -> tuple[ProviderCapability, ...]:
    """Providers that could serve ``aoi`` for ``data_types``, with no network calls.

    This is the coverage pre-filtering of ``design.md`` §4, and it answers "who
    could serve geology here" entirely from declarations -- no traffic, and no
    credentials needed to find out.
    """
    selected = []
    for provider in ALL_PROVIDERS:
        if aoi is not None and not provider.covers(aoi):
            continue
        if not provider.serves(data_types):
            continue
        selected.append(provider)
    return tuple(selected)


__all__ = [
    "ALL_CREDENTIALS",
    "ALL_PROVIDERS",
    "BGS",
    "NATURAL_EARTH",
    "OPENSTREETMAP",
    "OPENTOPOGRAPHY",
    "OPENTOPOGRAPHY_CREDENTIAL",
    "OS_DATA_HUB",
    "OS_DATA_HUB_CREDENTIAL",
    "PROVIDERS_BY_NAME",
    "providers_for",
]
