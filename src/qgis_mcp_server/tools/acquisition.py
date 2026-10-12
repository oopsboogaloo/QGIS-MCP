"""Acquisition-layer tools available so far: clipping, credentials, CRS policy.

``clip_to_aoi`` is task 4.2 and ``check_api_credentials`` is task 4.6; both are
reachable before any provider adapter implements ``search``, which is why they
land first. ``select_target_crs`` exposes the task 4.3 policy so a caller can
settle the CRS question before acquiring anything -- REQ-12's tier 3 requires a
human decision, and finding that out *after* downloading three layers is the
expensive order to discover it in.

All three are mode-agnostic under REQ-1b.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_protocol import ErrorCode, error, ok
from qgis_mcp_server import modes
from qgis_mcp_server.clipping import ClipRunner
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.crs_policy import AoiError, RasterInput, select_target_crs
from qgis_mcp_server.geocoding import Resolution
from qgis_mcp_server.providers.catalogue import ALL_CREDENTIALS, ALL_PROVIDERS
from qgis_mcp_server.tools.result import tool_result

_CLIP_DESCRIPTION = """\
Clip a raster or vector layer to an area of interest.

Give exactly one of bbox or mask:
  - bbox gives a rectangular result with no ragged nodata edge, which is what a
    Blender heightmap wants.
  - mask follows a real boundary polygon, which is what stops a city render
    being fringed with isolated farm tracks running off into the countryside.

IMPORTANT — bbox order is [xmin, ymin, xmax, ymax], the same order GeoJSON,
OGC and GDAL use. Do NOT pass QGIS's own xmin,xmax,ymin,ymax extent order; this
tool converts internally. Getting it wrong elsewhere does not raise an error,
it silently returns a correctly-formatted raster of the wrong place, which is
the reason this tool exists rather than calling the clip algorithms directly.

Always pass bbox_crs when giving a bbox. Without it the numbers are read in the
layer's own CRS — right by luck when they match, silently wrong when they do not.

For a GeoPackage or SQLite source, pass data_kind, since those can hold either
raster or vector and the extension cannot decide.

Needs confirm=true if the destination already exists.
"""

_CREDENTIALS_DESCRIPTION = """\
Report which provider API keys are configured, and how to get the ones missing.

A diagnostic, not a pre-flight check: a provider lacking a key already shows up
flagged in search results, so you do not need to call this before acquiring
data. Reach for it when asked what is set up, or when a provider is reported
unavailable and you want to know why.

Returns presence, which source supplied each key, and its length — never the
key itself, not even partially. For anything missing it returns the
registration URL and the steps, because the server cannot complete a signup.

Also reports each provider's coverage and licence tier, including where a free
tier does not extend to commercial use.
"""

_CRS_DESCRIPTION = """\
Choose the target CRS for an area of interest, before acquiring data (REQ-12).

Acquired layers are reprojected on registration to one projected, metre-based
CRS derived from the AOI. This reports which, and why:

  tier 1 — AOI within the UK            -> EPSG:27700
  tier 2 — AOI within one UTM zone      -> that zone
  tier 3 — AOI spans several zones      -> candidates; YOU must choose

In tier 3 the response has requires_choice=true and no crs. That is deliberate:
at continental scale the equal-area versus conformal decision visibly changes
the finished map, so it is a cartographic judgement rather than a default the
server should make. Present the candidates and their trade-offs and ask.

Pass the AOI as [min_lon, min_lat, max_lon, max_lat] in EPSG:4326.

Why this matters more than it sounds: a geographic-CRS DEM treated as a square
grid renders terrain stretched east-west by roughly 62% at UK latitudes. The
output looks like plausible terrain and is not the shape of that place.
"""


_RESOLVE_PLACE_DESCRIPTION = """\
Turn a place name into an area of interest.

Returns the bounding box as [min_lon, min_lat, max_lon, max_lat] in EPSG:4326,
ready to pass to select_target_crs or an acquisition tool, plus a real boundary
polygon where the geocoder has one — clipping a city's streets to its actual
built-up boundary rather than its bounding rectangle is the difference between
a render of a city and a render of a city plus surrounding farmland.

AMBIGUITY IS AN ERROR, NOT A GUESS. If several places match with comparable
confidence, this returns ambiguous_place with every candidate and its
administrative context, and you must put the choice to the user rather than
taking the first. "Stonehenge" matches Wiltshire, Saskatchewan and Queensland;
"Newport" matches Wales, Rhode Island and more. Picking one silently produces
data for the wrong continent, and nothing downstream can detect it.

A candidate may carry a warning that it is too small to be a useful AOI — a
monument rather than a settlement. Read it before acquiring.

Resolutions are cached locally, so repeating a lookup costs nothing and stays
within the geocoder's usage policy.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the acquisition-layer tools on ``server``."""
    clipper = ClipRunner(context.processing)

    @server.tool(name="resolve_place", description=_RESOLVE_PLACE_DESCRIPTION)
    def resolve_place(
        place: str,
        limit: int = 10,
        country_code: str | None = None,
        confidence_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Resolve a place name to an AOI (REQ-9).

        Args:
            place: Place name. More administrative context resolves better --
                "Newport, Isle of Wight" beats "Newport".
            limit: Maximum candidates to consider.
            country_code: ISO 3166-1 alpha-2 hint, e.g. ``gb``, used to prefer
                a national gazetteer where one is configured.
            confidence_threshold: Override how strong a single match must be to
                resolve without asking. Lower it only deliberately: the whole
                point of the threshold is that guessing is worse than asking.
        """
        decision = modes.resolve("resolve_place")
        resolution = context.places.resolve(
            place,
            limit=limit,
            country_code=country_code,
            threshold=confidence_threshold,
        )
        payload = resolution.to_dict()

        if resolution.state == Resolution.RESOLVED:
            return tool_result(ok(payload), decision)

        # Ambiguity and absence come back as errors rather than as a successful
        # response with a state field, so that a caller cannot treat either as
        # an answer by overlooking a key. The candidates travel in the error,
        # which is the same "return candidates, never fail bare" shape the rest
        # of this server uses (requirements.md section 0).
        code = {
            Resolution.AMBIGUOUS: ErrorCode.AMBIGUOUS_PLACE,
            Resolution.NOT_FOUND: ErrorCode.PLACE_NOT_FOUND,
            Resolution.UNAVAILABLE: ErrorCode.GEOCODER_UNAVAILABLE,
        }[resolution.state]
        # The resolution's own "message" becomes the error message rather than
        # travelling beside it as a duplicate -- and leaving it in the payload
        # would collide with error()'s own parameter.
        detail = {key: value for key, value in payload.items() if key != "message"}
        return tool_result(
            error(code, resolution.message or resolution.state, **detail), decision
        )

    @server.tool(name="clip_to_aoi", description=_CLIP_DESCRIPTION)
    def clip_to_aoi(
        source: str,
        destination: str,
        bbox: list[float] | None = None,
        bbox_crs: str | None = None,
        mask: str | None = None,
        data_kind: str | None = None,
        confirm: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Clip a layer to an AOI.

        Args:
            source: Layer to clip -- a file path, or a provider URI such as
                ``data.gpkg|layername=roads``.
            destination: Output path.
            bbox: ``[xmin, ymin, xmax, ymax]``. Mutually exclusive with mask.
            bbox_crs: CRS the bbox is expressed in, e.g. ``EPSG:27700``.
            mask: Polygon layer to clip against. Mutually exclusive with bbox.
            data_kind: ``raster`` or ``vector``. Inferred from the extension
                when omitted; required for GeoPackage and SQLite sources.
            confirm: Required if the destination already exists (REQ-4).
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("clip_to_aoi")
        envelope = clipper.clip(
            source,
            destination,
            bbox=tuple(bbox) if bbox else None,
            bbox_crs=bbox_crs,
            mask=mask,
            data_kind=data_kind,
            confirm=confirm,
            timeout=timeout_seconds,
        )
        return tool_result(envelope, decision)

    @server.tool(
        name="check_api_credentials", description=_CREDENTIALS_DESCRIPTION
    )
    def check_api_credentials() -> dict[str, Any]:
        """Report configured provider credentials and licence terms."""
        decision = modes.resolve("check_api_credentials")

        statuses = [
            context.credentials.status(spec).to_dict() for spec in ALL_CREDENTIALS
        ]
        missing = [entry["provider"] for entry in statuses if not entry["present"]]

        providers = []
        for provider in ALL_PROVIDERS:
            entry = provider.to_dict()
            if provider.credential is not None:
                status = context.credentials.status(provider.credential)
                entry["credential_present"] = status.present
            else:
                entry["credential_present"] = None
            entry["usable_now"] = (
                not provider.requires_credentials or entry["credential_present"] is True
            )
            providers.append(entry)

        # REQ-10 makes this a diagnostic rather than a gate, and saying so here
        # stops a caller treating a missing key as a blocker: a partial menu
        # annotated "BGS: skipped, no API key" is more useful than an error.
        return tool_result(
            ok(
                {
                    "credentials": statuses,
                    "missing": missing,
                    "all_present": not missing,
                    "providers": providers,
                    "commercial_attention": [
                        {
                            "provider": provider.name,
                            "tier": provider.licence.tier,
                            "note": provider.licence.commercial_note,
                        }
                        for provider in ALL_PROVIDERS
                        if provider.licence.needs_commercial_check
                        and provider.licence.commercial_note
                    ],
                    "note": (
                        "Missing credentials do not block a search: providers "
                        "lacking a key are reported as unavailable alongside "
                        "the results that succeeded (REQ-10)."
                    ),
                }
            ),
            decision,
        )

    @server.tool(name="select_target_crs", description=_CRS_DESCRIPTION)
    def select_target_crs_tool(
        aoi: list[float],
        raster_inputs: list[dict[str, Any]] | None = None,
        prefer_crs: str | None = None,
    ) -> dict[str, Any]:
        """Derive the target CRS for an AOI.

        Args:
            aoi: ``[min_lon, min_lat, max_lon, max_lat]`` in EPSG:4326.
            raster_inputs: Rasters being brought in, each
                ``{"name": ..., "crs": ..., "resolution_m": ...}``. Used to
                report which one gets resampled -- something always does, and
                it should be the 30 m global DEM rather than the 5 m national
                one.
            prefer_crs: Adopt this projected CRS instead of the tier default,
                when you have already confirmed it covers the AOI.
        """
        decision = modes.resolve("select_target_crs")

        parsed: list[RasterInput] = []
        for index, entry in enumerate(raster_inputs or []):
            try:
                parsed.append(
                    RasterInput(
                        name=str(entry["name"]),
                        crs=str(entry["crs"]),
                        resolution_m=float(entry["resolution_m"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                return tool_result(
                    error(
                        ErrorCode.BAD_REQUEST,
                        f"raster_inputs[{index}] must have name, crs and "
                        f"resolution_m: {exc}",
                    ),
                    decision,
                )

        try:
            selection = select_target_crs(
                tuple(aoi), inputs=tuple(parsed), prefer_crs=prefer_crs
            )
        except AoiError as exc:
            return tool_result(
                error(ErrorCode.BAD_REQUEST, str(exc), aoi=list(aoi)), decision
            )

        return tool_result(ok(selection.to_dict()), decision)

    return None


__all__ = ["register"]
