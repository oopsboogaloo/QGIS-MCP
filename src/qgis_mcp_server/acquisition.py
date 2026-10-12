"""Downloading and registering an acquired layer (task 4.7).

``tasks.md`` puts this last in task 4 for a reason: "the only tool needing real
network calls, and the confirmation gate (REQ-4d) must be correctly wired before
it is safe to use unattended."

Four requirements meet here, and each one changes the shape of the flow:

* **REQ-4d** -- confirmation stating source, format, licence tier and
  approximate size, *before* fetching. Rather than making the caller guess what
  to confirm, an unconfirmed call returns the full disclosure and refuses; the
  caller re-calls with ``confirm=True``.
* **REQ-12** -- reproject on registration to one projected CRS derived from the
  AOI, **retain the unmodified original on disk**, and report the chosen CRS and
  the tier that selected it. A tier-3 AOI must not be resolved automatically, so
  the download refuses until the caller names a target.
* **REQ-13** -- record the DEM's vertical datum as layer metadata. A GeoTIFF has
  nowhere to put it, so it survives only in the project.
* **REQ-1b** -- report which mode served the call, because it is the only way to
  know whether the layer landed in an open canvas or in a project file on disk.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qgis_mcp_protocol import Envelope, ErrorCode, error, ok
from qgis_mcp_server.config import Config
from qgis_mcp_server.crs_policy import (
    AoiError,
    RasterInput,
    select_target_crs,
)
from qgis_mcp_server.headless import HeadlessRunner
from qgis_mcp_server.processing import ProcessingRunner
from qgis_mcp_server.providers.base import Dataset, LicenceTier
from qgis_mcp_server.providers.dispatcher import ProviderDispatcher

REGISTER_SCRIPT = "register.py"

#: Reprojection resampling. **Not** the algorithm's own default.
#:
#: ``gdal:warpreproject`` defaults ``RESAMPLING`` to 0, Nearest Neighbour,
#: which preserves exact pixel values and is correct for categorical rasters.
#: For a continuous elevation surface it is wrong: it produces blocky terraces
#: that look like terrain and are not, which is the same class of
#: silent-plausible-output failure REQ-11 guards against for bit depth. Bilinear
#: is the standard choice for continuous surfaces, so it is set explicitly here
#: rather than inherited.
RESAMPLING_BILINEAR = 1
RESAMPLING_NEAREST = 0

#: Nodata value stamped on a reprojected raster.
#:
#: Far below any real terrestrial elevation (the Dead Sea shore is about
#: -430 m), so it cannot be mistaken for data, and it matches the sentinel
#: the synthetic fixtures use.
NODATA_SENTINEL = -9999.0


@dataclass
class AcquisitionPlan:
    """What a download would do, as REQ-4d requires it stated beforehand."""

    dataset: Dataset
    aoi: tuple[float, float, float, float]
    target_crs: str
    crs_tier: int
    crs_reason: str
    original_path: Path
    reprojected_path: Path
    will_reproject: bool
    project_path: Path | None
    will_modify_project: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            # The four things REQ-4d requires the confirmation to state.
            "source": {
                "provider": self.dataset.provider,
                "dataset": self.dataset.identifier,
                "title": self.dataset.title,
            },
            "format": self.dataset.format,
            "licence": self.dataset.licence.to_dict(),
            "estimated_size_bytes": self.dataset.estimated_size_bytes,
            "estimated_size_human": _human_size(self.dataset.estimated_size_bytes),
            # And what it would change on disk, which REQ-4 cares about.
            "aoi": list(self.aoi),
            "aoi_crs": "EPSG:4326",
            "target_crs": self.target_crs,
            "crs_tier": self.crs_tier,
            "crs_reason": self.crs_reason,
            "will_reproject": self.will_reproject,
            "original_path": str(self.original_path),
            "reprojected_path": str(self.reprojected_path),
            "project_path": str(self.project_path) if self.project_path else None,
            "will_modify_project": self.will_modify_project,
            "vertical_datum": self.dataset.extra.get("vertical_datum"),
            "warnings": list(self.warnings),
        }


@dataclass
class AcquisitionRunner:
    """Fetches a dataset, reprojects it, and registers it in a project."""

    config: Config
    processing: ProcessingRunner
    headless: HeadlessRunner
    providers: ProviderDispatcher

    # -- paths -------------------------------------------------------------

    @property
    def download_dir(self) -> Path:
        """Where downloads land.

        Under the per-user cache rather than beside the project, on the same
        reasoning REQ-4b gives for backups: a downloaded tile is about an area,
        not about one project, and two projects over the same ground should not
        fetch it twice.
        """
        return self.config.cache_dir / "downloads"

    def paths_for(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        target_crs: str,
    ) -> tuple[Path, Path]:
        """The original and reprojected paths for one acquisition.

        The AOI is hashed into the name so that two different areas of the same
        dataset do not overwrite each other -- REQ-12 requires the original to
        be *retained*, and a name that collides would break that on the second
        download rather than the first.
        """
        digest = hashlib.sha256(
            f"{dataset.provider}|{dataset.identifier}|"
            f"{','.join(f'{value:.6f}' for value in aoi)}".encode("utf-8")
        ).hexdigest()[:10]
        folder = self.download_dir / dataset.provider
        stem = f"{dataset.identifier}_{digest}"
        suffix = ".tif"
        crs_tag = target_crs.replace(":", "").replace("+", "").replace(" ", "")[:24]
        return (
            folder / f"{stem}_original{suffix}",
            folder / f"{stem}_{crs_tag}{suffix}",
        )

    # -- planning ----------------------------------------------------------

    def find_dataset(
        self,
        provider: str,
        identifier: str,
        aoi: tuple[float, float, float, float],
    ) -> tuple[Dataset | None, Envelope | None]:
        """Locate a dataset offered for ``aoi``, or explain why not.

        Resolved through the adapter's own ``search`` rather than trusting a
        caller-supplied description, so licence tier and size come from the
        provider rather than from whatever was passed in -- REQ-4d's
        disclosure is only worth anything if the server states it.
        """
        adapter = next(
            (item for item in self.providers.adapters if item.name == provider), None
        )
        if adapter is None:
            known = sorted(item.name for item in self.providers.adapters)
            return None, error(
                ErrorCode.BAD_REQUEST,
                f"no adapter for provider {provider!r} is implemented",
                requested_provider=provider,
                implemented_providers=known,
            )

        try:
            offered = adapter.search(aoi)
        except Exception as exc:  # noqa: BLE001 - becomes a structured failure
            return None, error(
                ErrorCode.BAD_REQUEST,
                f"{provider} could not list datasets for this AOI: {exc}",
                provider=provider,
            )

        match = next(
            (item for item in offered if item.identifier == identifier), None
        )
        if match is not None:
            return match, None

        # Not offered. Distinguishing "does not exist" from "excluded for this
        # AOI" matters: the first needs a different identifier, the second a
        # different AOI, and only one of those is the caller's mistake.
        reporter = getattr(adapter, "unavailable_for", None)
        excluded = reporter(aoi) if reporter else []
        reason = next(
            (
                entry
                for entry in excluded
                if entry.get("identifier") == identifier
            ),
            None,
        )
        if reason is not None:
            return None, error(
                ErrorCode.BAD_REQUEST,
                f"{identifier} exists but cannot serve this AOI: "
                f"{reason['reason']}",
                provider=provider,
                dataset=identifier,
                **{k: v for k, v in reason.items() if k != "identifier"},
            )
        return None, error(
            ErrorCode.BAD_REQUEST,
            f"{provider} does not offer a dataset called {identifier!r} for "
            f"this AOI.",
            provider=provider,
            dataset=identifier,
            available=[item.identifier for item in offered],
        )

    def plan(
        self,
        dataset: Dataset,
        aoi: tuple[float, float, float, float],
        *,
        target_crs: str | None = None,
        project_path: Path | str | None = None,
        register: bool = True,
    ) -> tuple[AcquisitionPlan | None, Envelope | None]:
        """Work out what a download would do, without doing any of it."""
        resolved_inputs = ()
        if dataset.resolution_m and dataset.crs:
            resolved_inputs = (
                RasterInput(
                    name=dataset.identifier,
                    crs=dataset.crs,
                    resolution_m=dataset.resolution_m,
                ),
            )

        try:
            selection = select_target_crs(
                aoi, inputs=resolved_inputs, prefer_crs=target_crs
            )
        except AoiError as exc:
            return None, error(ErrorCode.BAD_REQUEST, str(exc), aoi=list(aoi))

        if selection.requires_choice:
            # REQ-12 forbids the server choosing here, and that applies with
            # most force at download time: once three layers have arrived in a
            # projection nobody chose, the cost of the wrong answer is a
            # re-download rather than a question.
            return None, error(
                ErrorCode.BAD_REQUEST,
                f"this AOI spans {len(selection.utm_zones)} UTM zones, so no "
                f"single projected CRS serves it and the choice is "
                f"cartographic rather than technical. Pick a candidate and "
                f"pass it as target_crs. {selection.reason}",
                aoi=list(aoi),
                **selection.to_dict(),
            )

        chosen = selection.crs
        assert chosen is not None  # requires_choice was False
        original_path, reprojected_path = self.paths_for(dataset, aoi, chosen)

        resolved_project = (
            Path(project_path).expanduser()
            if project_path
            else self.config.project_path
        )

        warnings: list[str] = []

        datum = dataset.extra.get("vertical_datum")
        if not datum or datum == "unknown":
            # REQ-13: warn on an unknown datum and proceed.
            warnings.append(
                "this dataset's vertical datum is unknown, so elevations "
                "cannot be compared with another DEM or used for sea-level "
                "modelling without checking it first (REQ-13)."
            )
        elif "ellipsoid" in str(datum).lower():
            warnings.append(
                f"this dataset uses {datum} heights, not orthometric ones. In "
                f"the UK they sit roughly 50 m above EGM96 values, so do not "
                f"mix it with another product or use it for sea-level "
                f"modelling without converting first."
            )

        if dataset.licence.needs_commercial_check:
            warnings.append(
                f"licence tier is {dataset.licence.tier}: "
                f"{dataset.licence.commercial_note or dataset.licence.summary}"
            )
        if dataset.licence.attribution:
            warnings.append(
                f"published output must carry: {dataset.licence.attribution}"
            )

        will_reproject = dataset.crs != chosen
        if not will_reproject:
            warnings.append(
                f"the dataset is already in {chosen}, so no resampling is "
                f"needed and the registered layer is the original file."
            )

        will_modify_project = bool(register and resolved_project)
        if register and not resolved_project:
            warnings.append(
                "no project is configured, so the layer will be downloaded and "
                "reprojected but not registered. Pass project_path, or set one "
                "with --project."
            )

        plan = AcquisitionPlan(
            dataset=dataset,
            aoi=aoi,
            target_crs=chosen,
            crs_tier=selection.tier,
            crs_reason=selection.reason,
            original_path=original_path,
            reprojected_path=reprojected_path,
            will_reproject=will_reproject,
            project_path=resolved_project,
            will_modify_project=will_modify_project,
            warnings=warnings,
        )
        return plan, None

    # -- execution ---------------------------------------------------------

    def execute(
        self,
        plan: AcquisitionPlan,
        *,
        register: bool = True,
        resampling: int = RESAMPLING_BILINEAR,
        layer_name: str | None = None,
        timeout: float | None = None,
    ) -> Envelope:
        """Carry out a confirmed plan."""
        adapter = next(
            (
                item
                for item in self.providers.adapters
                if item.name == plan.dataset.provider
            ),
            None,
        )
        if adapter is None:
            return error(
                ErrorCode.BAD_REQUEST,
                f"no adapter for provider {plan.dataset.provider!r}",
            )

        plan.original_path.parent.mkdir(parents=True, exist_ok=True)

        # --- fetch, unless the original is already here -------------------
        reused = plan.original_path.exists() and plan.original_path.stat().st_size > 0
        if not reused:
            try:
                adapter.fetch(plan.dataset, plan.aoi, plan.original_path)
            except Exception as exc:  # noqa: BLE001 - provider-side failure
                return error(
                    ErrorCode.BAD_REQUEST,
                    f"could not download {plan.dataset.identifier} from "
                    f"{plan.dataset.provider}: {exc}",
                    provider=plan.dataset.provider,
                    dataset=plan.dataset.identifier,
                )

        steps: list[dict[str, Any]] = [
            {
                "step": "download",
                "path": str(plan.original_path),
                "bytes": plan.original_path.stat().st_size,
                # REQ-12 requires the original retained. Saying it was reused
                # rather than re-fetched also explains a suspiciously fast call
                # and, more practically, a daily allowance that did not move.
                "reused_existing": reused,
            }
        ]

        # --- reproject (REQ-12), original untouched -----------------------
        layer_path = plan.original_path
        if plan.will_reproject:
            envelope = self.processing.run(
                "gdal:warpreproject",
                {
                    "INPUT": str(plan.original_path),
                    "SOURCE_CRS": plan.dataset.crs,
                    "TARGET_CRS": plan.target_crs,
                    "RESAMPLING": resampling,
                    # Reprojecting a lat/lon box into a projected CRS leaves
                    # the corners of the output rectangle uncovered, and
                    # without an explicit nodata GDAL fills them with 0 --
                    # which for a DEM is indistinguishable from real elevation
                    # at sea level. It then enters the elevation range, pulls
                    # the minimum to 0, and distorts the displacement ratio
                    # REQ-11 derives from it. Declaring a sentinel keeps the
                    # fill out of every later statistic.
                    "NODATA": NODATA_SENTINEL,
                    "OUTPUT": str(plan.reprojected_path),
                },
                # The destination is ours, derived from the AOI hash, and the
                # caller has already confirmed the acquisition.
                confirm=True,
                timeout=timeout,
            )
            if not envelope.ok:
                return error(
                    ErrorCode.ALGORITHM_FAILED,
                    f"downloaded {plan.dataset.identifier} but could not "
                    f"reproject it to {plan.target_crs}. The original is kept "
                    f"at {plan.original_path}.",
                    original_path=str(plan.original_path),
                    reprojection_error=envelope.error,
                )
            layer_path = plan.reprojected_path
            steps.append(
                {
                    "step": "reproject",
                    "from_crs": plan.dataset.crs,
                    "to_crs": plan.target_crs,
                    "resampling": (
                        "bilinear" if resampling == RESAMPLING_BILINEAR else str(resampling)
                    ),
                    "nodata": NODATA_SENTINEL,
                    "path": str(plan.reprojected_path),
                    "original_retained": str(plan.original_path),
                }
            )

        result: dict[str, Any] = {
            "dataset": plan.dataset.to_dict(),
            "aoi": list(plan.aoi),
            "aoi_crs": "EPSG:4326",
            "target_crs": plan.target_crs,
            "crs_tier": plan.crs_tier,
            "crs_reason": plan.crs_reason,
            "original_path": str(plan.original_path),
            "layer_path": str(layer_path),
            "reprojected": plan.will_reproject,
            "vertical_datum": plan.dataset.extra.get("vertical_datum"),
            "steps": steps,
            "warnings": list(plan.warnings),
        }

        # --- register (REQ-13 provenance) ---------------------------------
        if not register:
            result["registered"] = False
            result["registration_note"] = "register=false, so no project was touched"
            return ok(result)
        if plan.project_path is None:
            result["registered"] = False
            result["registration_note"] = (
                "no project is configured, so the files were produced but not "
                "registered. Pass project_path to register them."
            )
            return ok(result)

        registration = self.headless.run_child(
            REGISTER_SCRIPT,
            {
                "layer_path": str(layer_path),
                "layer_name": layer_name or _default_layer_name(plan),
                "project_path": str(plan.project_path),
                "provenance": {
                    "vertical_datum": plan.dataset.extra.get("vertical_datum"),
                    "provider": plan.dataset.provider,
                    "dataset_identifier": plan.dataset.identifier,
                    "dataset_title": plan.dataset.title,
                    "licence_tier": plan.dataset.licence.tier,
                    "licence_summary": plan.dataset.licence.summary,
                    "attribution": plan.dataset.licence.attribution,
                    "original_path": str(plan.original_path),
                    "source_crs": plan.dataset.crs,
                    "target_crs": plan.target_crs,
                    "crs_tier": plan.crs_tier,
                    "aoi": list(plan.aoi),
                    "acquired_resolution_m": plan.dataset.resolution_m,
                    "resampled": plan.will_reproject,
                },
            },
            timeout=timeout,
        )
        if not registration.ok:
            # The files are on disk and useful, so this is reported as a
            # partial success rather than a failure -- discarding a completed
            # download because the project write failed would waste one of a
            # small daily allowance.
            result["registered"] = False
            result["registration_error"] = registration.error
            result["registration_note"] = (
                "the data was downloaded and reprojected successfully but "
                "could not be registered; the files above are usable as they "
                "are."
            )
            return ok(result)

        result["registered"] = True
        result["registration"] = registration.result
        steps.append(
            {
                "step": "register",
                "project_path": registration.result.get("project_path"),
                "layer_id": registration.result.get("layer_id"),
                "layer_name": registration.result.get("layer_name"),
            }
        )
        return ok(result)


def _default_layer_name(plan: AcquisitionPlan) -> str:
    """A layer name that says what the layer is without being a path."""
    return f"{plan.dataset.identifier} ({plan.dataset.provider})"


def _human_size(size: int | None) -> str | None:
    """Size in units a person reads, for REQ-4d's "approximate size".

    The decision a confirmation informs is between a few megabytes and a few
    gigabytes, and an exact byte count conveys that worse than a rounded one.
    """
    if size is None:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return None


__all__ = [
    "NODATA_SENTINEL",
    "RESAMPLING_BILINEAR",
    "RESAMPLING_NEAREST",
    "REGISTER_SCRIPT",
    "AcquisitionPlan",
    "AcquisitionRunner",
]
