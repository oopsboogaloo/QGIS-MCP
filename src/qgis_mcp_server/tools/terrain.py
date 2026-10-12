"""Terrain and Blender-export tools (task 5).

``fetch_elevation_data`` (5.1), ``export_heightmap`` (5.2) and
``model_sea_level_change`` (5.4). All mode-agnostic under REQ-1b.

``fetch_elevation_data`` is a convenience wrapper, and ``design.md`` §7 names
the risk in it: "it wraps search_data_sources + download_and_register_layer +
clip_to_aoi for the most common case. The convenience is justified, but it is a
second route to the same outcome and the two can drift -- hence REQ-4d naming it
explicitly so the confirmation gate cannot be bypassed through the wrapper." So
it goes through the same plan-and-confirm path as the tool it wraps, rather than
reimplementing it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qgis_mcp_protocol import ErrorCode, error, ok
from qgis_mcp_server import modes
from qgis_mcp_server.acquisition import _human_size
from qgis_mcp_server.clipping import ClipRunner
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.geocoding import Resolution
from qgis_mcp_server.providers.base import DataType
from qgis_mcp_server.terrain import CONNECTIVITY, HEIGHTMAP_FORMATS, NODATA_FILLS
from qgis_mcp_server.tools.result import tool_result

_HEIGHTMAP_DESCRIPTION = """\
Export a DEM as a 16-bit displacement heightmap for Blender.

Returns the numbers you need to set up the displacement, which a heightmap
image cannot carry on its own:

  source_elevation_min_m / max_m  the real range the image's 0..65535 maps to
  extent_width_m / height_m       the ground the image covers
  aspect_ratio                    width / height
  displacement_ratio              strength for a plane one Blender unit wide

Set the plane's X size to 1, its Y size to 1/aspect_ratio, and the displacement
strength to displacement_ratio. Without these the vertical exaggeration is set
by eye and is not reproducible between exports.

Always 16 bits or more. 8-bit would give 256 steps, which across a 300 m range
is 1.2 m per step and reads as visible stair-stepping on a rendered hillside.

Nodata is excluded from the range, so terrain is scaled to the real elevations
rather than to a -9999 sentinel — the latter looks completely normal in the
file's metadata and renders as a flat plane.

A geographic-CRS DEM is REFUSED, not silently exported: its pixels are degrees,
so a ratio derived from them stretches terrain east-west by roughly 62% at UK
latitudes. Reproject first — download_and_register_layer does it on acquisition.
"""

_SEA_LEVEL_DESCRIPTION = """\
Model a sea level against a DEM. Thresholding, never modification.

The source DEM is opened read-only and is not changed. Outputs are new files: a
land/water mask raster, a polygonised water layer, and optionally a depth raster.

connectivity is the parameter that matters:
  bathtub (default)  floods every cell below the level, connected or not
  ocean              floods only what the sea can actually reach from the edge

The difference is a landlocked basin below sea level. Bathtub floods it; ocean
does not. With ocean the response also reports how many cells were excluded for
being landlocked, so the two answers are comparable.

Pass vertical_datum if you know it — get_layer_summary reports it for layers
acquired through this server, and fetch_elevation_data returns it. Without it
you get a warning and the result anyway: sea level is modelled in single-metre
increments, so a datum offset of a metre or two is a large fraction of the
signal, and ellipsoidal heights would be wrong by roughly 50 m in the UK.
"""

_FETCH_ELEVATION_DESCRIPTION = """\
Get elevation data for an area, in one call: search, download, reproject, clip.

The common case, wrapping search_data_sources, download_and_register_layer and
clip_to_aoi. It carries the SAME confirmation gate as the download it wraps —
calling it without confirm returns the source, format, licence tier and
approximate size and downloads nothing. A convenience wrapper is not a way
around the gate.

Give a place name or a bbox as [min_lon, min_lat, max_lon, max_lat] in
EPSG:4326. Omit dataset to take the best available for the area, which is
normally Copernicus GLO-30.

The result is clipped to a clean rectangle in the target CRS. That matters for
a heightmap: reprojecting a lat/lon box gives a curved quadrilateral, so the
bounding rectangle has nodata corners, and those would distort the elevation
range export_heightmap derives its displacement ratio from.

Returns the registered layer, its vertical datum, and the clipped raster ready
to pass to export_heightmap.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the terrain tools on ``server``."""
    clipper = ClipRunner(context.processing)

    @server.tool(name="export_heightmap", description=_HEIGHTMAP_DESCRIPTION)
    def export_heightmap(
        source: str,
        destination: str,
        image_format: str = "png",
        nodata_fill: str = "min",
        allow_geographic: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Export a DEM as a displacement heightmap (REQ-11).

        Args:
            source: DEM raster path. Must be in a projected CRS.
            destination: Output path.
            image_format: ``png`` (16-bit, Blender's preference), ``tif``
                (16-bit) or ``tif32`` (float metres, lossless).
            nodata_fill: ``min`` fills gaps at the lowest real elevation;
                ``interpolate`` fills across them, which suits small holes and
                invents terrain across large ones.
            allow_geographic: Override the geographic-CRS refusal. Only do this
                if you understand the distortion it produces.
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("export_heightmap")
        envelope = context.terrain.export_heightmap(
            source,
            destination,
            image_format=image_format,
            nodata_fill=nodata_fill,
            allow_geographic=allow_geographic,
            timeout=timeout_seconds,
        )
        return tool_result(envelope, decision)

    @server.tool(
        name="model_sea_level_change", description=_SEA_LEVEL_DESCRIPTION
    )
    def model_sea_level_change(
        source: str,
        sea_level_m: float = 0.0,
        connectivity: str = "bathtub",
        output_dir: str | None = None,
        prefix: str = "sea_level",
        depth_raster: bool = False,
        vertical_datum: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Model a sea level against a DEM (REQ-13).

        Args:
            source: DEM raster path. Not modified.
            sea_level_m: Threshold in the DEM's own vertical units.
            connectivity: ``bathtub`` or ``ocean``.
            output_dir: Where to write outputs. Defaults to beside the DEM.
            prefix: Filename prefix for the outputs.
            depth_raster: Also write water depth where flooded.
            vertical_datum: The DEM's vertical datum, if known.
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("model_sea_level_change")
        envelope = context.terrain.model_sea_level_change(
            source,
            sea_level_m=sea_level_m,
            connectivity=connectivity,
            output_dir=output_dir,
            prefix=prefix,
            depth_raster=depth_raster,
            vertical_datum=vertical_datum,
            timeout=timeout_seconds,
        )
        return tool_result(envelope, decision)

    @server.tool(
        name="fetch_elevation_data", description=_FETCH_ELEVATION_DESCRIPTION
    )
    def fetch_elevation_data(
        place: str | None = None,
        bbox: list[float] | None = None,
        dataset: str | None = None,
        provider: str = "opentopography",
        target_crs: str | None = None,
        project_path: str | None = None,
        clip: bool = True,
        register_layer: bool = True,
        confirm: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Search, download, reproject and clip elevation for an area (REQ-4d).

        Args:
            place: Place name. Mutually exclusive with bbox.
            bbox: ``[min_lon, min_lat, max_lon, max_lat]`` in EPSG:4326.
            dataset: Specific dataset identifier. Omit for the best available.
            provider: Elevation provider.
            target_crs: Required when the AOI spans several UTM zones.
            project_path: Project to register into.
            clip: Clip the reprojected raster to a clean rectangle.
            register_layer: Whether to add the layer to a project.
            confirm: Required to download anything (REQ-4d).
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("fetch_elevation_data")

        if (place is None) == (bbox is None):
            return tool_result(
                error(
                    ErrorCode.BAD_REQUEST,
                    "fetch_elevation_data needs exactly one of place or bbox.",
                ),
                decision,
            )

        # --- AOI (REQ-9) ---------------------------------------------------
        if place is not None:
            resolution = context.places.resolve(place)
            if resolution.state != Resolution.RESOLVED:
                payload = resolution.to_dict()
                detail = {k: v for k, v in payload.items() if k != "message"}
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
        else:
            try:
                aoi = _validated_aoi(bbox)
            except ValueError as exc:
                return tool_result(
                    error(ErrorCode.BAD_REQUEST, str(exc), bbox=list(bbox or [])),
                    decision,
                )

        # --- pick the dataset ----------------------------------------------
        if dataset is None:
            outcome = context.providers.search(aoi, (DataType.ELEVATION,))
            candidates = outcome.grouped().get(DataType.ELEVATION, [])
            candidates = [
                item for item in candidates if item.provider == provider
            ]
            if not candidates:
                return tool_result(
                    error(
                        ErrorCode.BAD_REQUEST,
                        f"{provider} offers no elevation dataset for this AOI.",
                        aoi=list(aoi),
                        provider_status=[
                            status.to_dict() for status in outcome.statuses
                        ],
                    ),
                    decision,
                )
            # search() already ranks by resolution then by the preference that
            # keeps ellipsoidal products from leading, so the first is the one
            # it is safe to take without reading the notes.
            chosen = candidates[0]
        else:
            chosen, failure = context.acquisition.find_dataset(
                provider, dataset, aoi
            )
            if failure is not None:
                return tool_result(failure, decision)

        plan, failure = context.acquisition.plan(
            chosen,
            aoi,
            target_crs=target_crs,
            project_path=project_path,
            register=register_layer,
        )
        if failure is not None:
            return tool_result(failure, decision)

        # --- REQ-4d: the same gate, not a way around it --------------------
        if not confirm:
            return tool_result(
                error(
                    ErrorCode.CONFIRMATION_REQUIRED,
                    f"this would download {plan.dataset.title} "
                    f"({plan.dataset.identifier}) from {plan.dataset.provider}, "
                    f"about {_human_size(plan.dataset.estimated_size_bytes)}, "
                    f"licensed {plan.dataset.licence.tier}. Review the details "
                    f"and call again with confirm=true.",
                    confirmation_required=True,
                    plan=plan.to_dict(),
                    wraps="download_and_register_layer",
                ),
                decision,
            )

        envelope = context.acquisition.execute(
            plan, register=register_layer, timeout=timeout_seconds
        )
        if not envelope.ok:
            return tool_result(envelope, decision)

        result = dict(envelope.result)
        result["aoi"] = list(aoi)

        # --- clip to a clean rectangle in the target CRS -------------------
        if clip:
            clipped, note = _clip_to_target_aoi(
                context, clipper, plan, result, aoi, timeout_seconds
            )
            if clipped is not None:
                result["clipped_path"] = str(clipped)
                result["heightmap_source"] = str(clipped)
            if note:
                result.setdefault("warnings", []).append(note)
        if "heightmap_source" not in result:
            result["heightmap_source"] = result["layer_path"]

        return tool_result(ok(result), decision)

    return None


def _clip_to_target_aoi(context, clipper, plan, result, aoi, timeout):
    """Clip the reprojected raster to the inscribed AOI rectangle.

    The *inscribed* rectangle rather than the bounding one, because the output
    is meant for ``export_heightmap``: reprojecting a lat/lon box produces a
    curved quadrilateral, so its bounding rectangle carries nodata corners, and
    those would be filled and counted when the displacement ratio is derived.
    A clean rectangle costs a thin sliver of ground and keeps the elevation
    range honest.
    """
    transformed = context.terrain.transform_bbox(
        aoi, source_crs="EPSG:4326", target_crs=plan.target_crs, timeout=timeout
    )
    if not transformed.ok:
        return None, (
            f"could not transform the AOI into {plan.target_crs}, so the "
            f"raster was left unclipped: "
            f"{(transformed.error or {}).get('message')}"
        )

    inner = transformed.result.get("inner")
    if not inner:
        return None, (
            "the transformed AOI is too distorted for a clean rectangle, so "
            "the raster was left unclipped; expect nodata at its edges."
        )

    # Inset by one pixel before clipping.
    #
    # The inscribed rectangle touches the edge of the reprojected data, and a
    # clip snaps to the raster's own pixel grid -- so a boundary falling
    # mid-pixel pulls in a partial row of the nodata that reprojection left in
    # the corners. One pixel of margin costs a sliver of ground and is what
    # makes the result actually free of nodata, which is the entire reason for
    # clipping before a heightmap export.
    pixel = plan.dataset.resolution_m or 0.0
    if pixel > 0:
        inset = [
            inner[0] + pixel,
            inner[1] + pixel,
            inner[2] - pixel,
            inner[3] - pixel,
        ]
        if inset[0] < inset[2] and inset[1] < inset[3]:
            inner = inset

    destination = Path(result["layer_path"]).with_name(
        Path(result["layer_path"]).stem + "_clipped.tif"
    )
    envelope = clipper.clip(
        result["layer_path"],
        destination,
        bbox=tuple(inner),
        bbox_crs=plan.target_crs,
        data_kind="raster",
        confirm=True,
        timeout=timeout,
    )
    if not envelope.ok:
        return None, (
            f"downloaded and reprojected successfully but the clip failed, so "
            f"the unclipped raster is what was produced: "
            f"{(envelope.error or {}).get('message')}"
        )
    return destination, None


def _validated_aoi(bbox: list[float] | None) -> tuple[float, float, float, float]:
    """Check an AOI bbox given as ``[min_lon, min_lat, max_lon, max_lat]``."""
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
            f"max_lat={max_lat}) is empty or reversed."
        )
    return (min_lon, min_lat, max_lon, max_lat)


__all__ = ["register"]
