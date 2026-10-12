"""Place-name resolution (task 4.1).

``resolve_place`` exists because ``search_data_sources`` accepts a place string
as well as a bounding box, and REQ-9 governs what happens when that string is
ambiguous: return the candidates, never guess.

**What is implemented, and what is deferred.** ``tasks.md`` 4.1 names three
geocoders -- Nominatim as the no-key global default, with ONS Open Geography and
OS Names for UK places. Nominatim is implemented and delivers the whole REQ-9
contract on its own, including the real boundary polygons the narrative spec
§4.6.1 singles it out for.

The two UK gazetteers are deferred rather than dropped, for a reason worth
recording: both return coordinates in EPSG:27700, and converting those to the
4326 the AOI convention requires needs a PROJ transform -- which means crossing
the process boundary into PyQGIS. That is a different shape of adapter from
Nominatim's single stdlib GET, and it belongs with the layer-registration work
in task 4.7 that already owns transforms. The tier mechanism is in place
(:class:`PlaceResolver` orders country-scoped geocoders ahead of global ones),
so adding them is adding an adapter rather than reworking the resolver.

Nothing here imports PyQGIS.
"""

from qgis_mcp_server.geocoding.base import Geocoder, GeocoderError, PlaceCandidate
from qgis_mcp_server.geocoding.cache import ResolutionCache, cache_key
from qgis_mcp_server.geocoding.nominatim import NominatimGeocoder
from qgis_mcp_server.geocoding.resolver import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    GeocoderStatus,
    PlaceResolution,
    PlaceResolver,
    Resolution,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "Geocoder",
    "GeocoderError",
    "GeocoderStatus",
    "NominatimGeocoder",
    "PlaceCandidate",
    "PlaceResolution",
    "PlaceResolver",
    "Resolution",
    "ResolutionCache",
    "cache_key",
]
