"""Terrain export and sea-level modelling, server side (task 5).

A thin front for ``_child/terrain.py``: validate what can be validated without
a QGIS startup, then hand the work across the process boundary. The raster
maths lives in the child, where numpy, GDAL and scipy are.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis_mcp_protocol import Envelope, ErrorCode, ExecutionMode, error
from qgis_mcp_server.headless import HeadlessRunner

TERRAIN_SCRIPT = "terrain.py"
RENDER_SCRIPT = "render.py"
MESH_SCRIPT = "meshexport.py"

#: Heightmap output formats, mirroring the child's own table.
HEIGHTMAP_FORMATS = ("png", "tif", "tif32")

#: ``bathtub`` floods everything below the threshold. ``ocean`` floods only what
#: the sea can reach from the edge of the raster, which is the difference
#: between a coastal inlet and a landlocked basin.
CONNECTIVITY = ("bathtub", "ocean")

NODATA_FILLS = ("min", "interpolate")

#: Raster formats for a map render, and for a layout export.
IMAGE_FORMATS = ("png", "jpg", "tif")
LAYOUT_FORMATS = ("png", "pdf", "svg", "jpg")

#: Mesh-friendly vector formats (task 5.5).
MESH_FORMATS = ("geojson", "obj")

#: Upper bound on layout export resolution. Past this the output is
#: gigabytes for no visible gain, and a typo is far likelier than intent.
MAX_DPI = 2400.0


@dataclass
class TerrainRunner:
    """Runs the terrain operations in a child process."""

    headless: HeadlessRunner

    def _run(self, request: dict[str, Any], *, timeout: float | None) -> Envelope:
        return self.headless.run_child(
            TERRAIN_SCRIPT,
            request,
            timeout=timeout,
            timeout_hint=(
                "Terrain operations read the whole raster into memory; a large "
                "DEM takes proportionally longer."
            ),
        )

    def _check_source(self, source: Path | str) -> tuple[Path | None, Envelope | None]:
        """Resolve a source raster to an absolute path, or explain why not.

        Absolute, for the reason the headless runner's project resolution gives:
        the server validates the path and the child opens it, and a relative
        path can mean different things to the two processes.
        """
        path = Path(source).expanduser()
        if not path.exists():
            return None, error(
                ErrorCode.BAD_REQUEST,
                f"source raster does not exist: {path}",
                mode=ExecutionMode.HEADLESS,
                source=str(path),
            )
        return path.resolve(), None

    # -- 5.2 ---------------------------------------------------------------

    def export_heightmap(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        image_format: str = "png",
        nodata_fill: str = "min",
        allow_geographic: bool = False,
        timeout: float | None = None,
    ) -> Envelope:
        """Export a DEM as a displacement heightmap (REQ-11)."""
        resolved, failure = self._check_source(source)
        if failure is not None:
            return failure

        if image_format not in HEIGHTMAP_FORMATS:
            return error(
                ErrorCode.BAD_REQUEST,
                f"image_format must be one of {HEIGHTMAP_FORMATS}; got "
                f"{image_format!r}",
                mode=ExecutionMode.HEADLESS,
                supported_formats=list(HEIGHTMAP_FORMATS),
            )
        if nodata_fill not in NODATA_FILLS:
            return error(
                ErrorCode.BAD_REQUEST,
                f"nodata_fill must be one of {NODATA_FILLS}; got "
                f"{nodata_fill!r}",
                mode=ExecutionMode.HEADLESS,
            )

        return self._run(
            {
                "operation": "export_heightmap",
                "source": str(resolved),
                "destination": str(Path(destination).expanduser().resolve()),
                "format": image_format,
                "nodata_fill": nodata_fill,
                "allow_geographic": allow_geographic,
            },
            timeout=timeout,
        )

    # -- 5.4 ---------------------------------------------------------------

    def model_sea_level_change(
        self,
        source: Path | str,
        *,
        sea_level_m: float = 0.0,
        connectivity: str = "bathtub",
        output_dir: Path | str | None = None,
        prefix: str = "sea_level",
        depth_raster: bool = False,
        vertical_datum: str | None = None,
        timeout: float | None = None,
    ) -> Envelope:
        """Model a sea level against a DEM, without modifying it (REQ-13)."""
        resolved, failure = self._check_source(source)
        if failure is not None:
            return failure

        if connectivity not in CONNECTIVITY:
            return error(
                ErrorCode.BAD_REQUEST,
                f"connectivity must be one of {CONNECTIVITY}; got "
                f"{connectivity!r}",
                mode=ExecutionMode.HEADLESS,
                supported_connectivity=list(CONNECTIVITY),
            )

        # Outputs go beside the source unless told otherwise: a sea-level run
        # produces three files, and scattering them into the working directory
        # is worse than putting them where the DEM already is.
        destination_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir
            else resolved.parent / f"{resolved.stem}_sea_level"
        )

        return self._run(
            {
                "operation": "model_sea_level_change",
                "source": str(resolved),
                "sea_level_m": float(sea_level_m),
                "connectivity": connectivity,
                "output_dir": str(destination_dir),
                "prefix": prefix,
                "depth_raster": bool(depth_raster),
                "vertical_datum": vertical_datum,
            },
            timeout=timeout,
        )

    # -- shared ------------------------------------------------------------

    def transform_bbox(
        self,
        bbox: tuple[float, float, float, float],
        *,
        source_crs: str = "EPSG:4326",
        target_crs: str,
        timeout: float | None = None,
    ) -> Envelope:
        """Transform a bbox, returning both the bounding and inscribed rectangles."""
        if not bbox or len(tuple(bbox)) != 4:
            return error(
                ErrorCode.BAD_REQUEST,
                f"bbox must be [xmin, ymin, xmax, ymax]; got {bbox!r}",
                mode=ExecutionMode.HEADLESS,
            )
        return self._run(
            {
                "operation": "transform_bbox",
                "bbox": [float(value) for value in bbox],
                "source_crs": source_crs,
                "target_crs": target_crs,
            },
            timeout=timeout,
        )


    # -- 5.3 and 5.6: rendering ------------------------------------------

    def _render(
        self,
        request: dict[str, Any],
        project_path: Path | str | None,
        *,
        timeout: float | None,
    ) -> Envelope:
        resolved, failure = self.headless.resolve_project(project_path)
        if failure is not None:
            return failure
        if resolved is None:
            # Rendering is always of *something in a project*: a layout is a
            # project object, and a layer combination needs layers to resolve
            # names against. Checked here so the common misconfiguration does
            # not cost a QGIS startup.
            return error(
                ErrorCode.BAD_REQUEST,
                "rendering needs a project. Pass project_path, or configure "
                "one with --project.",
                mode=ExecutionMode.HEADLESS,
            )
        return self.headless.run_child(
            RENDER_SCRIPT,
            {**request, "project_path": str(resolved)},
            timeout=timeout,
            timeout_hint=(
                "A render at high DPI over many layers takes proportionally "
                "longer; raise the timeout rather than reducing the DPI."
            ),
        )

    def render_layout(
        self,
        destination: Path | str,
        *,
        layout: str | None = None,
        image_format: str = "png",
        dpi: float = 300.0,
        world_file: bool = True,
        project_path: Path | str | None = None,
        timeout: float | None = None,
    ) -> Envelope:
        """Export a Print Layout. Mode-agnostic under REQ-1b."""
        if image_format not in LAYOUT_FORMATS:
            return error(
                ErrorCode.BAD_REQUEST,
                f"image_format must be one of {LAYOUT_FORMATS}; got "
                f"{image_format!r}",
                mode=ExecutionMode.HEADLESS,
                supported_formats=list(LAYOUT_FORMATS),
            )
        if not 0 < dpi <= MAX_DPI:
            return error(
                ErrorCode.BAD_REQUEST,
                f"dpi must be between 1 and {MAX_DPI:.0f}; got {dpi}",
                mode=ExecutionMode.HEADLESS,
            )
        return self._render(
            {
                "operation": "render_layout",
                "destination": str(Path(destination).expanduser().resolve()),
                "layout": layout,
                "format": image_format,
                "dpi": dpi,
                "world_file": world_file,
            },
            project_path,
            timeout=timeout,
        )

    def render_map(
        self,
        destination: Path | str,
        *,
        layers: list[str] | None = None,
        extent: tuple[float, float, float, float] | None = None,
        extent_crs: str | None = None,
        crs: str | None = None,
        pixel_size: float | None = None,
        width: int | None = None,
        height: int | None = None,
        image_format: str = "png",
        background: str | None = None,
        transparent: bool = False,
        dpi: float | None = None,
        project_path: Path | str | None = None,
        timeout: float | None = None,
    ) -> Envelope:
        """Render a layer combination, reporting the world box it covers."""
        if image_format not in IMAGE_FORMATS:
            return error(
                ErrorCode.BAD_REQUEST,
                f"image_format must be one of {IMAGE_FORMATS}; got "
                f"{image_format!r}",
                mode=ExecutionMode.HEADLESS,
                supported_formats=list(IMAGE_FORMATS),
            )
        return self._render(
            {
                "operation": "render_map",
                "destination": str(Path(destination).expanduser().resolve()),
                "layers": list(layers) if layers else None,
                "extent": [float(value) for value in extent] if extent else None,
                "extent_crs": extent_crs,
                "crs": crs,
                "pixel_size": pixel_size,
                "width": width,
                "height": height,
                "format": image_format,
                "background": background,
                "transparent": transparent,
                "dpi": dpi,
            },
            project_path,
            timeout=timeout,
        )

    # -- 5.5: vector export ----------------------------------------------

    def export_vector_for_mesh(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        layer: str | None = None,
        mesh_format: str = "geojson",
        dem: Path | str | None = None,
        z_scale: float = 1.0,
        local_origin: bool | None = None,
        timeout: float | None = None,
    ) -> Envelope:
        """Export a vector layer for Blender, optionally draped over a DEM."""
        resolved, failure = self._check_source(source)
        if failure is not None:
            return failure
        if mesh_format not in MESH_FORMATS:
            return error(
                ErrorCode.BAD_REQUEST,
                f"mesh_format must be one of {MESH_FORMATS}; got "
                f"{mesh_format!r}",
                mode=ExecutionMode.HEADLESS,
                supported_formats=list(MESH_FORMATS),
            )

        resolved_dem = None
        if dem:
            resolved_dem, failure = self._check_source(dem)
            if failure is not None:
                return failure

        return self.headless.run_child(
            MESH_SCRIPT,
            {
                "operation": "export_vector_for_mesh",
                "source": str(resolved),
                "layer": layer,
                "destination": str(Path(destination).expanduser().resolve()),
                "format": mesh_format,
                "dem": str(resolved_dem) if resolved_dem else None,
                "z_scale": z_scale,
                "local_origin": local_origin,
            },
            timeout=timeout,
        )


__all__ = [
    "CONNECTIVITY",
    "IMAGE_FORMATS",
    "LAYOUT_FORMATS",
    "MESH_FORMATS",
    "MAX_DPI",
    "MESH_SCRIPT",
    "RENDER_SCRIPT",
    "HEIGHTMAP_FORMATS",
    "NODATA_FILLS",
    "TERRAIN_SCRIPT",
    "TerrainRunner",
]
