"""The provider adapter layer (task 4.0, ``spec/design.md`` section 4).

``base`` holds the interface and the protocol-family bases; ``catalogue`` holds
the v1 declarations. Adapter implementations of ``search`` and ``fetch`` arrive
per provider in tasks 4.4 and 4.8 -- what exists now is everything knowable
without a network call, which is enough for credential reporting and for the
dispatcher's coverage pre-filtering.
"""

from qgis_mcp_server.providers.base import (
    Dataset,
    DataType,
    LicenceDeclaration,
    LicenceTier,
    OgcAdapter,
    OverpassAdapter,
    ProviderAdapter,
    ProviderCapability,
    RestApiAdapter,
    StaticCatalogueAdapter,
)
from qgis_mcp_server.providers.catalogue import (
    ALL_CREDENTIALS,
    ALL_PROVIDERS,
    PROVIDERS_BY_NAME,
    providers_for,
)

__all__ = [
    "ALL_CREDENTIALS",
    "ALL_PROVIDERS",
    "PROVIDERS_BY_NAME",
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
    "providers_for",
]
