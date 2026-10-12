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
from qgis_mcp_server.providers.base import DataType, LicenceTier
from qgis_mcp_server.providers.catalogue import (
    ALL_CREDENTIALS,
    ALL_PROVIDERS,
    PROVIDERS_BY_NAME,
)
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


_SEARCH_DESCRIPTION = """\
Find data available for an area, grouped by type, across every provider.

Give either a place name or a bbox as [min_lon, min_lat, max_lon, max_lat] in
EPSG:4326. A place name is resolved first, and an ambiguous one comes back as
ambiguous_place with candidates rather than a guess.

Returns one combined menu grouped by data type, not one response per type. It
also reports, for every provider, whether it answered — and if not, why:
out of coverage, no API key, failed, or timed out. A provider missing a key is
NOT a blocker; the rest of the menu is still returned, so there is no need to
check credentials before calling this.

Read dataset_exclusions before concluding something is unavailable. A large AOI
commonly excludes the 30 m elevation products on per-request area limits while
leaving the 90 m ones, and the remedy — split the AOI — is only obvious once
the reason is stated.

target_crs previews what acquired layers would be reprojected to. If it says
requires_choice, settle that before downloading rather than after.

Nothing is downloaded. Use download_and_register_layer for that, per dataset.
"""

_LICENCE_DESCRIPTION = """\
Report the licence and attribution terms for a provider or a dataset.

Use it before publishing anything built from acquired data, and before using
acquired data in paid work. Commission deliverables can carry redistribution
or attribution obligations that differ per source, and those are not visible
in the data itself.

Returns the licence tier, what attribution must appear in a published output,
and whether the tier needs checking for commercial use. Two cases to know:
OpenStreetMap is share-alike, so a derived *database* must be offered under
ODbL (a produced map or image generally needs only attribution); and
OpenTopography's free API key does not cover commercial integration at all.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the acquisition-layer tools on ``server``."""
    clipper = ClipRunner(context.processing)

    @server.tool(name="search_data_sources", description=_SEARCH_DESCRIPTION)
    def search_data_sources(
        place: str | None = None,
        bbox: list[float] | None = None,
        data_types: list[str] | None = None,
        confidence_threshold: float | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Search every provider for data covering an area (REQ-8, REQ-9, REQ-10).

        Args:
            place: Place name, resolved via resolve_place. Mutually exclusive
                with bbox.
            bbox: ``[min_lon, min_lat, max_lon, max_lat]`` in EPSG:4326.
            data_types: Restrict to these types, e.g. ``["elevation"]``. Omit
                for everything available.
            confidence_threshold: Passed to place resolution.
            timeout_seconds: Bound on the provider fan-out.
        """
        decision = modes.resolve("search_data_sources")

        if (place is None) == (bbox is None):
            return tool_result(
                error(
                    ErrorCode.BAD_REQUEST,
                    "search_data_sources needs exactly one of place or bbox.",
                ),
                decision,
            )

        requested = tuple(data_types) if data_types else None
        if requested:
            unknown = [
                value for value in requested if value not in DataType.ALL
            ]
            if unknown:
                return tool_result(
                    error(
                        ErrorCode.BAD_REQUEST,
                        f"unknown data type(s): {', '.join(unknown)}",
                        requested=list(requested),
                        supported_data_types=list(DataType.ALL),
                    ),
                    decision,
                )

        # --- resolve the AOI ---------------------------------------------
        resolved_place: dict[str, Any] | None = None
        if place is not None:
            # REQ-9: resolve, and proceed only on exactly one strong candidate.
            resolution = context.places.resolve(
                place, threshold=confidence_threshold
            )
            if resolution.state != Resolution.RESOLVED:
                payload = resolution.to_dict()
                detail = {
                    key: value
                    for key, value in payload.items()
                    if key != "message"
                }
                code = {
                    Resolution.AMBIGUOUS: ErrorCode.AMBIGUOUS_PLACE,
                    Resolution.NOT_FOUND: ErrorCode.PLACE_NOT_FOUND,
                    Resolution.UNAVAILABLE: ErrorCode.GEOCODER_UNAVAILABLE,
                }[resolution.state]
                return tool_result(
                    error(code, resolution.message or resolution.state, **detail),
                    decision,
                )
            aoi = resolution.resolved.bbox
            resolved_place = resolution.resolved.to_dict()
        else:
            try:
                aoi = _validated_aoi(bbox)
            except ValueError as exc:
                return tool_result(
                    error(ErrorCode.BAD_REQUEST, str(exc), bbox=list(bbox or [])),
                    decision,
                )

        # --- fan out ------------------------------------------------------
        outcome = context.providers.search(
            aoi, requested, timeout=timeout_seconds
        )
        result = outcome.to_dict()
        result["aoi"] = list(aoi)
        result["aoi_crs"] = "EPSG:4326"
        result["aoi_source"] = "place" if place is not None else "bbox"
        if resolved_place is not None:
            result["resolved_place"] = resolved_place
        if requested:
            result["requested_data_types"] = list(requested)

        # Dataset-level exclusions, which the dispatcher cannot know about:
        # they are a property of one provider's catalogue rather than of the
        # provider. Left out, a large AOI would silently return only coarse
        # products and look like that is all that exists.
        exclusions: list[dict[str, Any]] = []
        for adapter in context.providers.adapters:
            reporter = getattr(adapter, "unavailable_for", None)
            if reporter is None:
                continue
            for entry in reporter(aoi):
                exclusions.append({"provider": adapter.name, **entry})
        if exclusions:
            result["dataset_exclusions"] = exclusions

        # The declared-versus-implemented gap, stated rather than left to look
        # like a provider that simply had nothing for this area.
        implemented = {adapter.name for adapter in context.providers.adapters}
        pending = [
            provider.to_dict() | {"status": "adapter not yet implemented"}
            for provider in ALL_PROVIDERS
            if provider.name not in implemented
        ]
        if pending:
            result["providers_not_yet_implemented"] = [
                {
                    "name": entry["name"],
                    "data_types": entry["data_types"],
                    "coverage_description": entry["coverage_description"],
                    "status": entry["status"],
                }
                for entry in pending
            ]

        # REQ-12 preview: the AOI is already known, so answering now saves a
        # round-trip -- and a tier-3 AOI needs a decision before anything is
        # downloaded, not after three layers have arrived in the wrong CRS.
        try:
            result["target_crs"] = select_target_crs(aoi).to_dict()
        except AoiError as exc:
            result["target_crs"] = {"error": str(exc)}

        return tool_result(ok(result), decision)

    @server.tool(name="get_dataset_licence_info", description=_LICENCE_DESCRIPTION)
    def get_dataset_licence_info(
        provider: str | None = None,
        dataset: str | None = None,
    ) -> dict[str, Any]:
        """Report licence terms for a provider, a dataset, or everything.

        Args:
            provider: Provider name, e.g. ``opentopography``. Omit for all.
            dataset: Dataset identifier, for a per-dataset answer where the
                provider distinguishes them.
        """
        decision = modes.resolve("get_dataset_licence_info")

        if provider is not None and provider not in PROVIDERS_BY_NAME:
            return tool_result(
                error(
                    ErrorCode.BAD_REQUEST,
                    f"unknown provider {provider!r}",
                    known_providers=sorted(PROVIDERS_BY_NAME),
                ),
                decision,
            )

        selected = (
            [PROVIDERS_BY_NAME[provider]] if provider else list(ALL_PROVIDERS)
        )
        entries = []
        for capability in selected:
            entry = {
                "provider": capability.name,
                "long_name": capability.long_name,
                "licence": capability.licence.to_dict(),
            }
            if dataset and capability.name == provider:
                entry["dataset"] = dataset
                # Per-dataset terms are the provider's to state; where the
                # tier is MIXED the provider-level answer is explicitly not
                # sufficient, and saying so beats implying it is.
                if capability.licence.tier == LicenceTier.MIXED:
                    entry["caveat"] = (
                        f"{capability.long_name} licenses products under "
                        f"different terms, so this provider-level answer does "
                        f"not settle {dataset!r}. Check the dataset's own "
                        f"terms at {capability.licence.url or 'the provider'}."
                    )
            entries.append(entry)

        return tool_result(
            ok(
                {
                    "licences": entries,
                    "needs_commercial_check": [
                        capability.name
                        for capability in selected
                        if capability.licence.needs_commercial_check
                    ],
                }
            ),
            decision,
        )

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


def _validated_aoi(bbox: list[float] | None) -> tuple[float, float, float, float]:
    """Check an AOI bbox given as ``[min_lon, min_lat, max_lon, max_lat]``.

    Reversed is refused rather than sorted, for the reason the clipping module
    gives at more length: a reversed box is as likely to be the wrong
    convention as a typo, and silently sorting it hides exactly the mistake
    worth catching.
    """
    values = [float(value) for value in (bbox or [])]
    if len(values) != 4:
        raise ValueError(
            f"bbox must be [min_lon, min_lat, max_lon, max_lat]; got "
            f"{len(values)} value(s)"
        )
    min_lon, min_lat, max_lon, max_lat = values
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise ValueError("longitudes must be within -180..180")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ValueError("latitudes must be within -90..90")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError(
            f"bbox (min_lon={min_lon}, min_lat={min_lat}, max_lon={max_lon}, "
            f"max_lat={max_lat}) is empty or reversed. Expected order is "
            f"[min_lon, min_lat, max_lon, max_lat]."
        )
    return (min_lon, min_lat, max_lon, max_lat)


__all__ = ["register"]
