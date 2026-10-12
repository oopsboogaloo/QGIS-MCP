"""What the tool modules are handed when they register.

Tools need the resolved configuration and the execution paths, and they need
them without reaching for module-level globals -- the tests construct a context
per case, pointing at a temp project or a deliberately broken QGIS root, and
global state would leak between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qgis_mcp_server.acquisition import AcquisitionRunner
from qgis_mcp_server.config import Config
from qgis_mcp_server.credentials import CredentialStore
from qgis_mcp_server.headless import HeadlessRunner
from qgis_mcp_server.geocoding import (
    NominatimGeocoder,
    PlaceResolver,
    ResolutionCache,
)
from qgis_mcp_server.processing import ProcessingRunner
from qgis_mcp_server.providers.dispatcher import ProviderDispatcher
from qgis_mcp_server.providers.opentopography import OpenTopographyAdapter
from qgis_mcp_server.terrain import TerrainRunner


@dataclass
class ServerContext:
    """Everything the registered tools share for the life of one server."""

    config: Config
    headless: HeadlessRunner = field(init=False)
    #: Shared rather than per-call, because it caches the algorithm catalogue
    #: and that costs a QGIS startup to build.
    processing: ProcessingRunner = field(init=False)
    credentials: CredentialStore = field(init=False)
    #: Shared so the Nominatim rate limiter and the cache are process-wide
    #: rather than per call -- which is what keeps usage inside policy.
    places: PlaceResolver = field(init=False)
    #: Data provider adapters, behind the REQ-10 dispatcher.
    #:
    #: Only the providers with an implemented adapter appear here. The five
    #: declared in providers/catalogue.py are the v1 target; the gap between
    #: the two is reported by search_data_sources rather than left to look
    #: like a provider that had nothing to offer.
    providers: ProviderDispatcher = field(init=False)
    #: Download, reproject and register (task 4.7).
    acquisition: AcquisitionRunner = field(init=False)
    #: Heightmap export and sea-level modelling (task 5).
    terrain: TerrainRunner = field(init=False)

    def __post_init__(self) -> None:
        self.headless = HeadlessRunner(self.config)
        self.processing = ProcessingRunner(self.config)
        self.credentials = self.config.credential_store
        self.places = PlaceResolver(
            geocoders=(
                NominatimGeocoder(
                    cache=ResolutionCache(self.config.cache_dir / "geocoder")
                ),
            )
        )
        self.providers = ProviderDispatcher(
            adapters=(OpenTopographyAdapter(self.credentials),)
        )
        self.terrain = TerrainRunner(headless=self.headless)
        self.acquisition = AcquisitionRunner(
            config=self.config,
            processing=self.processing,
            headless=self.headless,
            providers=self.providers,
        )

    def close(self) -> None:
        self.headless.close()

    def __enter__(self) -> ServerContext:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


__all__ = ["ServerContext"]
