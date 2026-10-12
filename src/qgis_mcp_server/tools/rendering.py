"""Rendering and mesh-export tools (tasks 5.3, 5.5 and 5.6).

``render_map_to_path`` is the one tool REQ-1a pointedly excludes from its scope,
and the reason is worth keeping in view: a Print Layout is a project object, so
rendering one works headless and falls under REQ-1b. Only the *canvas* variant
needs a live session, because a canvas view exists only in the GUI. So this tool
is mode-agnostic, and asking it for the canvas is the one request that gets a
missing-mode error.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_protocol import ErrorCode, ExecutionMode, error
from qgis_mcp_server import modes
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.terrain import (
    IMAGE_FORMATS,
    LAYOUT_FORMATS,
    MESH_FORMATS,
)
from qgis_mcp_server.tools.result import tool_result

_RENDER_DESCRIPTION = """\
Render a project to an image or PDF — a named Print Layout, or a chosen
combination of layers over a chosen extent.

Two modes:
  layout=<name>   export that Print Layout to PNG, PDF, SVG or JPEG at a DPI.
                  Works headless; this is the usual path for a finished map.
  no layout       render layers directly over an extent, which is what you
                  want for a quick look or a texture.

source="canvas" is only available in a live QGIS session, since a canvas view
exists in the GUI and nowhere else. Headless it returns an error naming the
missing mode rather than quietly rendering something else.

For a direct render, pass pixel_size (metres per pixel) OR one of width/height,
not both. QGIS expands the extent to match the image's aspect ratio, so passing
a mismatched width and height silently covers more ground than you asked for —
the response reports extent_was_adjusted and the extent actually rendered.
"""

_TEXTURE_DESCRIPTION = """\
Render layers as a texture image, with the world-space box it covers.

Built for UV-aligning a texture against a heightmap-derived mesh in Blender:
render the same extent you exported the heightmap from, and the two line up.
Geology, an old-map raster overlay, a hillshade, or any combination of styled
layers works as the source.

ALWAYS use the returned `extent`, never the one you requested. QGIS expands an
extent to match the image's aspect ratio, so the two can differ — asking for a
1 km square at 256x200 px really renders 1.28 km by 1 km, and a texture UV-mapped
to the requested box would be out by 140 m on each side with nothing in the
image to show it. Passing pixel_size avoids the expansion entirely.

A world file is written alongside, so the image is georeferenced on disk and can
be loaded straight back into QGIS as a raster.
"""

_MESH_DESCRIPTION = """\
Export a vector layer for Blender, draped over a DEM.

Roads, coastlines, contours and boundaries out to GeoJSON (with Z) or OBJ (as
polylines), with each vertex's elevation sampled from a DEM so the result sits
on the terrain rather than flat at zero. Pass the same DEM you built the
heightmap from and they agree.

OBJ is written in local metres relative to the layer's own corner, because
Blender's single-precision floats lose centimetre accuracy at a 440,000 m
easting and that shows up as jitter along a road. The origin is returned — use
the same one for every layer that must line up.

The layer and the DEM must be in the same projected CRS. Sampling across
different CRSs would read elevations from the wrong places and return
plausible numbers, so it is refused rather than attempted.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the rendering and mesh-export tools on ``server``."""

    @server.tool(name="render_map_to_path", description=_RENDER_DESCRIPTION)
    def render_map_to_path(
        destination: str,
        layout: str | None = None,
        source: str = "layout_or_map",
        layers: list[str] | None = None,
        extent: list[float] | None = None,
        extent_crs: str | None = None,
        crs: str | None = None,
        pixel_size: float | None = None,
        width: int | None = None,
        height: int | None = None,
        image_format: str = "png",
        dpi: float = 300.0,
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Render a layout or a layer combination (REQ-1b).

        Args:
            destination: Output path.
            layout: Print Layout name. Omit to render layers directly.
            source: ``canvas`` to request the live canvas, which needs a live
                session. Anything else renders headless.
            layers: Layer names, first drawn on top. Omit for the project's own
                layer order.
            extent: ``[xmin, ymin, xmax, ymax]``. Omit for the layers' union.
            extent_crs: CRS the extent is given in, if not the render CRS.
            crs: Render CRS. Defaults to the project's.
            pixel_size: Metres per pixel. Preferred over width/height, since it
                cannot produce an aspect mismatch.
            width: Output width in pixels.
            height: Output height in pixels.
            image_format: ``png``, ``pdf``, ``svg`` or ``jpg`` for a layout;
                ``png``, ``jpg`` or ``tif`` for a direct render.
            dpi: Layout export resolution.
            project_path: Project to render. Defaults to the configured one.
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("render_map_to_path")

        if source == "canvas":
            # The one request this tool cannot serve headless. REQ-1a excludes
            # render_map_to_path from its scope precisely because only this
            # variant is live-only, so the error names the mode rather than
            # substituting a layout or a direct render -- which would return a
            # successful-looking image of something the caller did not ask for.
            return tool_result(
                error(
                    ErrorCode.MODE_UNAVAILABLE,
                    "the map canvas exists only in a running QGIS session, and "
                    "the live listener is not available. Render a Print Layout "
                    "by name, or render layers directly over an extent -- both "
                    "work headless.",
                    mode=ExecutionMode.LIVE,
                    missing_mode=ExecutionMode.LIVE,
                ),
                decision,
            )

        if layout is not None:
            envelope = context.terrain.render_layout(
                destination,
                layout=layout,
                image_format=image_format,
                dpi=dpi,
                project_path=project_path,
                timeout=timeout_seconds,
            )
        else:
            if image_format not in IMAGE_FORMATS:
                return tool_result(
                    error(
                        ErrorCode.BAD_REQUEST,
                        f"a direct render supports {IMAGE_FORMATS}; "
                        f"{image_format!r} is a layout format. Pass a layout "
                        f"name to export {image_format}.",
                        supported_formats=list(IMAGE_FORMATS),
                        layout_formats=list(LAYOUT_FORMATS),
                    ),
                    decision,
                )
            envelope = context.terrain.render_map(
                destination,
                layers=layers,
                extent=tuple(extent) if extent else None,
                extent_crs=extent_crs,
                crs=crs,
                pixel_size=pixel_size,
                width=width,
                height=height,
                image_format=image_format,
                dpi=dpi,
                project_path=project_path,
                timeout=timeout_seconds,
            )
        return tool_result(envelope, decision)

    @server.tool(
        name="export_georeferenced_texture", description=_TEXTURE_DESCRIPTION
    )
    def export_georeferenced_texture(
        destination: str,
        layers: list[str] | None = None,
        extent: list[float] | None = None,
        extent_crs: str | None = None,
        crs: str | None = None,
        pixel_size: float | None = None,
        width: int | None = None,
        transparent: bool = False,
        background: str | None = None,
        project_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Render a texture and report the world box it covers.

        Args:
            destination: Output image path.
            layers: Layer names, first drawn on top.
            extent: ``[xmin, ymin, xmax, ymax]``. Match the heightmap's extent.
            extent_crs: CRS the extent is given in, if not the render CRS.
            crs: Render CRS. Use a projected one for anything UV-aligned.
            pixel_size: Metres per pixel. Preferred — it cannot produce an
                aspect mismatch, so the returned extent matches the requested.
            width: Output width in pixels, if resolution matters more than
                ground sampling.
            transparent: Transparent background instead of a solid colour.
            background: Background colour, e.g. ``#ffffff``.
            project_path: Project to render. Defaults to the configured one.
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("export_georeferenced_texture")
        # Deliberately no `height`: offering both is how an aspect mismatch
        # gets introduced, and this tool's entire value is an extent the
        # caller can trust against a mesh.
        envelope = context.terrain.render_map(
            destination,
            layers=layers,
            extent=tuple(extent) if extent else None,
            extent_crs=extent_crs,
            crs=crs,
            pixel_size=pixel_size,
            width=width,
            transparent=transparent,
            background=background,
            project_path=project_path,
            timeout=timeout_seconds,
        )
        if envelope.ok and isinstance(envelope.result, dict):
            envelope.result["uv_alignment_note"] = (
                "map this texture to a plane covering 'extent' in "
                f"{envelope.result.get('extent_crs')}. Use the same extent for "
                "export_heightmap and the two line up exactly."
            )
        return tool_result(envelope, decision)

    @server.tool(
        name="export_vector_layer_for_mesh", description=_MESH_DESCRIPTION
    )
    def export_vector_layer_for_mesh(
        source: str,
        destination: str,
        layer: str | None = None,
        mesh_format: str = "geojson",
        dem: str | None = None,
        z_scale: float = 1.0,
        local_origin: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Export a vector layer for Blender (task 5.5).

        Args:
            source: Vector file path, e.g. a GeoPackage.
            destination: Output path.
            layer: Layer name within the source. Omit for the first.
            mesh_format: ``geojson`` or ``obj``.
            dem: DEM to sample elevations from. Omit for a flat result.
            z_scale: Multiply sampled elevations, for vertical exaggeration.
            local_origin: Force local metre coordinates on or off. OBJ defaults
                to on, GeoJSON to off.
            timeout_seconds: Override the configured timeout.
        """
        decision = modes.resolve("export_vector_layer_for_mesh")
        if mesh_format not in MESH_FORMATS:
            return tool_result(
                error(
                    ErrorCode.BAD_REQUEST,
                    f"mesh_format must be one of {MESH_FORMATS}; got "
                    f"{mesh_format!r}",
                    supported_formats=list(MESH_FORMATS),
                ),
                decision,
            )
        envelope = context.terrain.export_vector_for_mesh(
            source,
            destination,
            layer=layer,
            mesh_format=mesh_format,
            dem=dem,
            z_scale=z_scale,
            local_origin=local_origin,
            timeout=timeout_seconds,
        )
        return tool_result(envelope, decision)

    return None


__all__ = ["register"]
