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

#: Heightmap output formats, mirroring the child's own table.
HEIGHTMAP_FORMATS = ("png", "tif", "tif32")

#: ``bathtub`` floods everything below the threshold. ``ocean`` floods only what
#: the sea can reach from the edge of the raster, which is the difference
#: between a coastal inlet and a landlocked basin.
CONNECTIVITY = ("bathtub", "ocean")

NODATA_FILLS = ("min", "interpolate")


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


__all__ = [
    "CONNECTIVITY",
    "HEIGHTMAP_FORMATS",
    "NODATA_FILLS",
    "TERRAIN_SCRIPT",
    "TerrainRunner",
]
