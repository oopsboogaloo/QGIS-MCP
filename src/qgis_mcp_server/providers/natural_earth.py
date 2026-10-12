"""Natural Earth, a static catalogue (task 4.8).

The simplest adapter in the project, and interesting for what it does *not*
have: no query API, no credentials, no server-side filtering. "Search" is
filtering a declared manifest, and a fetch pulls a whole zipped shapefile of
the entire world however small the AOI.

That last point is the one worth being honest about. Asking Natural Earth for
the coastline around Sudbury downloads every coastline on Earth, because there
is no other way to ask. The AOI is used for two things only: choosing a
sensible scale, and warning when the data is too coarse to mean anything at
the size being mapped.

**Scale is the real decision here.** Natural Earth publishes at 1:10,000,000,
1:50,000,000 and 1:110,000,000. Even the most detailed is a world atlas scale:
on a town-sized AOI its "coastline" is a handful of vertices and its roads are
motorways only. The adapter therefore recommends a scale from the AOI's span
and says plainly when the answer is "none of these" --- which for a UK town is
usually the truthful answer.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_server.providers.base import Dataset, DataType, StaticCatalogueAdapter
from qgis_mcp_server.providers.catalogue import NATURAL_EARTH, PUBLIC_DOMAIN_LICENCE

#: The CDN Natural Earth itself links to.
#:
#: Not ``naturalearthdata.com/http//...``, which is the redirect the website
#: uses and which answered HTTP 500 when checked.
BASE_URL = "https://naciscdn.org/naturalearth"

USER_AGENT = "qgis-mcp/0.1 (QGIS MCP server; https://github.com/oopsboogaloo/QGIS-MCP)"

DEFAULT_TIMEOUT = 180.0

#: Published scales, most detailed first. The number is the denominator in
#: millions, which is what makes them comparable with an AOI span.
SCALES = ("10m", "50m", "110m")

SCALE_DENOMINATOR = {"10m": 10_000_000, "50m": 50_000_000, "110m": 110_000_000}

#: Degrees of AOI span below which even 1:10m is too coarse to be useful.
#:
#: Roughly a 50 km box. Natural Earth's most detailed line work has vertices
#: hundreds of metres apart, so below this it draws a coastline that visibly
#: misses the coast. Stated as a warning rather than a refusal: context
#: linework at the wrong scale is a legitimate thing to want behind a map.
TOO_DETAILED_BELOW_DEGREES = 0.5


class NaturalEarthError(Exception):
    """A Natural Earth download failed."""


@dataclass(frozen=True)
class NaturalEarthTheme:
    """One Natural Earth product, across the scales it is published at."""

    data_type: str
    slug: str
    category: str  # "cultural" or "physical"
    title: str
    scales: tuple[str, ...] = SCALES
    #: Approximate zipped size at 1:10m, in bytes. Approximate on purpose: the
    #: exact figure comes free in the Content-Length header at fetch time, and
    #: a hardcoded table would drift silently as Natural Earth republishes.
    approx_bytes_10m: int = 2_000_000
    notes: str = ""

    def identifier(self, scale: str) -> str:
        return f"ne_{scale}_{self.slug}"

    def url(self, scale: str) -> str:
        return f"{BASE_URL}/{scale}/{self.category}/ne_{scale}_{self.slug}.zip"


THEMES = (
    NaturalEarthTheme(
        data_type=DataType.ADMIN_BOUNDARIES,
        slug="admin_0_countries",
        category="cultural",
        title="Country boundaries",
        approx_bytes_10m=4_900_000,
        notes="Sovereign states with names, ISO codes and population.",
    ),
    NaturalEarthTheme(
        data_type=DataType.ADMIN_BOUNDARIES,
        slug="admin_1_states_provinces",
        category="cultural",
        title="States and provinces",
        approx_bytes_10m=7_000_000,
        notes="First-level subdivisions: counties, states, regions.",
    ),
    NaturalEarthTheme(
        data_type=DataType.COASTLINE,
        slug="coastline",
        category="physical",
        title="Coastline",
        approx_bytes_10m=1_200_000,
        notes="A single global coastline layer, useful as a backdrop.",
    ),
    NaturalEarthTheme(
        data_type=DataType.HYDROLOGY,
        slug="rivers_lake_centerlines",
        category="physical",
        title="Rivers and lake centre lines",
        approx_bytes_10m=2_100_000,
    ),
    NaturalEarthTheme(
        data_type=DataType.HYDROLOGY,
        slug="lakes",
        category="physical",
        title="Lakes and reservoirs",
        approx_bytes_10m=900_000,
    ),
    NaturalEarthTheme(
        data_type=DataType.ROADS,
        slug="roads",
        category="cultural",
        title="Major roads",
        # Published at 1:10m only; there is no 50m or 110m roads layer, and
        # requesting one returns a 404 rather than a coarser file.
        scales=("10m",),
        approx_bytes_10m=9_100_000,
        notes=(
            "Motorways and trunk routes only, worldwide. For anything below "
            "national scale, OpenStreetMap is the right source instead."
        ),
    ),
    NaturalEarthTheme(
        data_type=DataType.PLACE_NAMES,
        slug="populated_places",
        category="cultural",
        title="Populated places",
        approx_bytes_10m=2_800_000,
        notes="Towns and cities as points, with population and capital status.",
    ),
)


def recommend_scale(aoi: tuple[float, float, float, float]) -> str:
    """The most appropriate published scale for an AOI's span.

    Natural Earth's scales are about how much of the world is in view, so the
    choice follows the AOI's larger dimension: a continent wants the generalised
    version, a country the detailed one. Taking the most detailed always would
    mean downloading nine megabytes of world roads to draw a backdrop behind
    Europe.
    """
    min_lon, min_lat, max_lon, max_lat = aoi
    span = max(abs(max_lon - min_lon), abs(max_lat - min_lat))
    if span > 60:
        return "110m"
    if span > 15:
        return "50m"
    return "10m"


class NaturalEarthAdapter(StaticCatalogueAdapter):
    """Natural Earth's fixed catalogue of whole-world downloads."""

    capability = NATURAL_EARTH

    def __init__(
        self,
        credentials=None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(credentials)
        self.base_url = base_url
        self.timeout = timeout

    def search(
        self,
        aoi: tuple[float, float, float, float],
        data_types: tuple[str, ...] | None = None,
    ) -> list[Dataset]:
        """The catalogue, filtered. No network call and no credentials."""
        min_lon, min_lat, max_lon, max_lat = aoi
        span = max(abs(max_lon - min_lon), abs(max_lat - min_lat))
        scale = recommend_scale(aoi)

        datasets = []
        for theme in THEMES:
            if data_types and theme.data_type not in data_types:
                continue
            chosen = scale if scale in theme.scales else theme.scales[0]
            extra: dict[str, Any] = {
                "scale": chosen,
                "scale_denominator": SCALE_DENOMINATOR[chosen],
                "available_scales": list(theme.scales),
                "whole_world": True,
                "clip_required": (
                    "there is no server-side bbox filtering: this downloads the "
                    "whole world and must be clipped locally with clip_to_aoi"
                ),
                "url": theme.url(chosen),
            }
            if theme.notes:
                extra["notes"] = theme.notes
            if chosen != scale:
                extra["scale_note"] = (
                    f"{theme.title} is published at {', '.join(theme.scales)} "
                    f"only, so {chosen} was used rather than the {scale} this "
                    f"AOI suggests"
                )
            if span < TOO_DETAILED_BELOW_DEGREES:
                extra["too_coarse_warning"] = (
                    f"this AOI spans about {span:.2f} degrees and the most "
                    f"detailed Natural Earth scale is 1:{SCALE_DENOMINATOR['10m']:,}. "
                    f"At this size the linework will visibly miss the features "
                    f"it names. Use OpenStreetMap for detail at town scale; "
                    f"Natural Earth is context behind a small-scale map."
                )

            datasets.append(
                Dataset(
                    provider=self.name,
                    identifier=theme.identifier(chosen),
                    title=f"{theme.title} ({chosen})",
                    data_type=theme.data_type,
                    licence=PUBLIC_DOMAIN_LICENCE,
                    crs="EPSG:4326",
                    estimated_size_bytes=_scaled_size(theme, chosen),
                    format="SHP-ZIP",
                    attribution="",  # public domain: none required
                    # Ranked below a provider with real local data. Natural
                    # Earth will happily answer for anywhere on Earth, and
                    # without this it would outrank OS or OSM on a UK AOI
                    # simply by being listed.
                    preference=5,
                    extra=extra,
                )
            )
        return datasets

    def fetch(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        destination: Path,
    ) -> Path:
        """Download the zipped shapefile named by ``dataset``.

        The AOI is ignored, which is not an oversight: there is nothing to pass
        it to. Clipping happens afterwards, locally.
        """
        url = (dataset.extra or {}).get("url")
        if not url:
            theme, scale = _theme_for(dataset.identifier)
            url = theme.url(scale)

        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise NaturalEarthError(
                f"Natural Earth refused {url} (HTTP {exc.code}). Not every "
                f"theme is published at every scale --- roads, for one, exists "
                f"only at 10m."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NaturalEarthError(f"could not reach Natural Earth: {exc}") from exc

        # A zip starts "PK". Checking is cheap and the alternative is writing a
        # CDN error page into a file named .zip, which fails later and
        # somewhere else.
        if not body.startswith(b"PK"):
            raise NaturalEarthError(
                f"{url} did not return a zip archive ({len(body)} bytes "
                f"beginning {body[:16]!r}). Refusing to write it."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return destination


def _scaled_size(theme: NaturalEarthTheme, scale: str) -> int:
    """Approximate download size at a scale, from the 1:10m figure.

    The generalised scales are far smaller, and roughly by the ratio of their
    denominators. Approximate and labelled as such --- the exact number arrives
    in the Content-Length header, free, at fetch time.
    """
    ratio = SCALE_DENOMINATOR["10m"] / SCALE_DENOMINATOR[scale]
    return max(20_000, int(theme.approx_bytes_10m * ratio))


def _theme_for(identifier: str) -> tuple[NaturalEarthTheme, str]:
    for theme in THEMES:
        for scale in theme.scales:
            if theme.identifier(scale) == identifier:
                return theme, scale
    known = ", ".join(
        sorted(theme.identifier(scale) for theme in THEMES for scale in theme.scales)
    )
    raise NaturalEarthError(
        f"{identifier!r} is not a Natural Earth product. Known: {known}"
    )


__all__ = [
    "BASE_URL",
    "SCALES",
    "SCALE_DENOMINATOR",
    "THEMES",
    "TOO_DETAILED_BELOW_DEGREES",
    "NaturalEarthAdapter",
    "NaturalEarthError",
    "NaturalEarthTheme",
    "recommend_scale",
]
