"""Processing algorithm tools (task 3).

``run_processing_algorithm`` and ``search_processing_algorithms``. Both are
mode-agnostic under REQ-1b, so both report which path served the call.

The narrative spec (§4.4) specifies ``search_processing_algorithms`` as
"list/search available Processing algorithm IDs *and their parameter schemas*",
which is why there is no separate describe tool: a search that narrows to one
algorithm returns that algorithm's full parameter and output definitions.
Returning full schemas for every hit would be unreadable, so the schema arrives
when the query has actually identified something.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_protocol import ErrorCode, error, ok
from qgis_mcp_server import modes
from qgis_mcp_server.context import ServerContext
from qgis_mcp_server.processing import DEFAULT_RESULT_LIMIT, ProcessingUnavailable
from qgis_mcp_server.qgis_install import QgisNotFound

_RUN_DESCRIPTION = """\
Run a QGIS Processing algorithm by id, with a parameter dictionary.

Processing is QGIS's main batteries-included analysis surface: buffers, clips,
zonal statistics, raster maths, reprojection, and the GDAL algorithms. Prefer
this over writing raw PyQGIS for anything an algorithm already does.

Pass parameters exactly as the algorithm defines them, e.g.
{"INPUT": "data.gpkg|layername=roads", "DISTANCE": 25, "OUTPUT": "out.gpkg"}.
Use search_processing_algorithms with an algorithm_id to get the parameter
schema, including enum option numbers. A wrong or slightly-wrong id returns
close matches rather than a bare failure.

OUTPUT HANDLING depends on which mode served the call, and the response says
which. In a live session, output layers are added to the open project. Headless,
outputs are written to the paths you give and are NOT registered in any
project -- so give every destination parameter an explicit path, and read the
returned `outputs` list to confirm each file was actually written.

Needs confirm=true if a destination would overwrite an existing file, or write
a .qgz/.qgs project file. There is no undo across a scripted session.
"""

_SEARCH_DESCRIPTION = """\
Find Processing algorithms, and get their parameter schemas.

Search by free text against algorithm ids, names, groups and tags -- e.g.
"buffer", "zonal", "clip raster", "hillshade". Pass algorithm_id instead to get
one algorithm's full parameter and output definitions, including the integer
option numbers for enum parameters, which are not guessable.

Reports the providers ACTUALLY registered in the running QGIS, not the set the
version nominally ships. Two absences are common and are reported plainly
rather than left implied: grass: algorithms need GRASS installed and its
provider plugin enabled, and saga: algorithms need a third-party plugin that
this server cannot install. If a provider is not listed, its algorithms are
not available here.
"""


def register(server: Any, context: ServerContext) -> None:
    """Register the Processing tools on ``server``."""

    @server.tool(name="run_processing_algorithm", description=_RUN_DESCRIPTION)
    def run_processing_algorithm(
        algorithm_id: str,
        parameters: dict[str, Any] | None = None,
        project_path: str | None = None,
        ellipsoid: str | None = None,
        confirm: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run one Processing algorithm.

        Args:
            algorithm_id: Provider-qualified id, e.g. ``native:buffer``.
            parameters: Algorithm parameters, keyed as the algorithm defines
                them. Destination parameters take output paths.
            project_path: Project to load during execution. Defaults to the
                configured project.
            ellipsoid: Ellipsoid for distance and area calculations, when the
                algorithm needs one.
            confirm: Required when a destination would overwrite existing data
                or write a project file (REQ-4).
            timeout_seconds: Override the configured timeout. Raise it for
                large rasters rather than shrinking the input.
        """
        decision = modes.resolve("run_processing_algorithm")
        envelope = context.processing.run(
            algorithm_id,
            parameters,
            project_path=project_path,
            ellipsoid=ellipsoid,
            confirm=confirm,
            timeout=timeout_seconds,
        )
        return _as_tool_result(envelope, decision)

    @server.tool(
        name="search_processing_algorithms", description=_SEARCH_DESCRIPTION
    )
    def search_processing_algorithms(
        query: str = "",
        algorithm_id: str | None = None,
        provider: str | None = None,
        include_deprecated: bool = False,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> dict[str, Any]:
        """Search algorithms, or describe one.

        Args:
            query: Free text matched against ids, names, groups and tags.
            algorithm_id: Exact id to describe in full. Takes precedence over
                ``query``.
            provider: Restrict to one provider, e.g. ``native`` or ``gdal``.
            include_deprecated: Include algorithms QGIS marks deprecated.
            limit: Maximum hits to return.
        """
        decision = modes.resolve("search_processing_algorithms")

        if algorithm_id:
            envelope = context.processing.describe(algorithm_id)
            if envelope.ok:
                # The provider report travels with every successful search, so
                # that "what is available here" never requires a second call.
                envelope.result["providers"] = _provider_report(context, provider)
            return _as_tool_result(envelope, decision)

        try:
            catalogue = context.processing.catalogue()
        except (QgisNotFound, ProcessingUnavailable) as exc:
            return _as_tool_result(
                modes.unavailable("search_processing_algorithms", str(exc)), decision
            )

        if provider and provider not in catalogue.providers:
            # An unregistered provider is reported as such rather than as zero
            # results: "no hits" implies the algorithms exist but did not match.
            return _as_tool_result(
                error(
                    ErrorCode.BAD_REQUEST,
                    f"provider {provider!r} is not registered in this QGIS "
                    f"install, so none of its algorithms are available here.",
                    requested_provider=provider,
                    **catalogue.absence_report(provider),
                ),
                decision,
            )

        hits, total = catalogue.search(
            query,
            provider=provider,
            include_deprecated=include_deprecated,
            limit=max(1, limit),
        )

        result: dict[str, Any] = {
            "query": query,
            "matched": total,
            "returned": len(hits),
            "algorithms": [algorithm.to_dict() for algorithm in hits],
            "providers": _provider_report(context, provider, catalogue=catalogue),
        }
        if total > len(hits):
            # Say so rather than letting a window read as the whole answer.
            result["truncated"] = True
            result["note"] = (
                f"{total} algorithms matched; showing {len(hits)}. Narrow the "
                f"query or raise limit to see the rest."
            )
        if len(hits) == 1:
            # One hit identifies an algorithm, so return its schema now and
            # save the caller a round-trip they would certainly make.
            detail = context.processing.describe(hits[0].id)
            if detail.ok:
                result["parameters"] = detail.result.get("parameters", {})
                result["outputs"] = detail.result.get("outputs", {})

        return _as_tool_result(ok(result), decision)

    return None


def _provider_report(
    context: ServerContext,
    requested_provider: str | None,
    *,
    catalogue: Any = None,
) -> dict[str, Any]:
    """Registered providers, plus explanations for notable absences.

    ``grass`` and ``saga`` are probed for by default, not because a hardcoded
    list is wanted -- §6.4 forbids exactly that -- but because they are the two
    the spec calls out, they are absent on most installs, and an agent that
    assumes otherwise will write calls that cannot work. What is *registered*
    is always read from the running QGIS.
    """
    try:
        resolved = catalogue if catalogue is not None else context.processing.catalogue()
    except (QgisNotFound, ProcessingUnavailable) as exc:
        return {"error": str(exc)}

    probe = ["grass", "saga"]
    if requested_provider:
        probe.append(requested_provider)
    return resolved.absence_report(*probe)


def _as_tool_result(envelope: Any, decision: modes.ModeDecision) -> dict[str, Any]:
    """Flatten an envelope, attaching the REQ-1b serving mode.

    The mode comes from the dispatcher's decision rather than from the
    envelope: these tools are mode-agnostic, so which path ran is a fact about
    the dispatch, and ``mode_reason`` records why -- which is what makes the
    field answer "so why is my layer not on screen?".
    """
    payload: dict[str, Any] = {
        "ok": envelope.ok,
        "mode": decision.mode,
        "mode_reason": decision.reason,
    }
    if envelope.ok:
        if isinstance(envelope.result, dict):
            payload.update(envelope.result)
        else:
            payload["result"] = envelope.result
        for key, value in envelope.meta.items():
            if value is not None:
                payload[key] = value
    else:
        payload["error"] = envelope.error or {}
    return payload


__all__ = ["register"]
