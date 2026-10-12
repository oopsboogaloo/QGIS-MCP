"""The Processing layer: the ``qgis_process`` invocation path and the catalogue.

``spec/design.md`` §3.2 prefers ``qgis_process --json`` wherever an operation is
a Processing algorithm, over hand-bootstrapping ``QgsApplication``: it is a
supported CLI contract with stable JSON output. Since much of the acquisition
and terrain surface is thin wrappers over Processing, this covers more of the
tool surface than it first appears.

**Parameters travel as JSON on stdin.** ``qgis_process`` accepts either
``--PARAM=VALUE`` arguments or, with ``-`` in place of the parameter list, a
JSON object on stdin. The JSON form is used here for the same reasons the
headless runner takes code as data: no escaping rules to get wrong for values
containing ``=``, spaces or quotes; a layer URI like
``file.gpkg|layername=x`` survives intact; ordered list values are just JSON
lists rather than a repeated-flag convention; and ``project_path`` and
``ellipsoid`` ride along in the same object.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qgis_mcp_protocol import Envelope, ErrorCode, error, ok
from qgis_mcp_server.config import Config
from qgis_mcp_server.qgis_install import QgisNotFound

#: ``provider:name``. Checked before launching anything, because a malformed id
#: cannot match and is not worth a QGIS startup to discover.
ALGORITHM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+:[A-Za-z0-9_.\-]+$")

#: Default cap on returned suggestions and search hits.
DEFAULT_RESULT_LIMIT = 15

#: Why a provider the spec discusses might not be registered.
#:
#: This is *not* the hardcoded provider list §6.4 forbids -- what is registered
#: is always read from the running install. This only supplies the explanation
#: for an absence already observed, for the two providers the spec calls out by
#: name. An absent provider not listed here is still reported, just without the
#: editorial.
PROVIDER_ABSENCE_NOTES: dict[str, str] = {
    "grass": (
        "GRASS algorithms are built in but require GRASS itself to be "
        "installed and the grassprovider plugin to be enabled."
    ),
    "grass7": (
        "The grass7: prefix was retired; current QGIS registers GRASS "
        "algorithms under grass:."
    ),
    "saga": (
        "SAGA stopped being a core Processing provider in QGIS 3.30 and now "
        "needs a third-party plugin. Plugin installation is out of scope for "
        "this server (spec/requirements.md section 9), so saga: algorithms "
        "cannot be made available from here."
    ),
    "sagang": (
        "The SAGA Next Gen provider is a third-party plugin. Plugin "
        "installation is out of scope for this server."
    ),
}

#: Project file suffixes. Writing one is "overwrite a project file" under
#: REQ-4, which requires confirmation regardless of whether it already exists.
PROJECT_SUFFIXES = {".qgz", ".qgs"}


@dataclass(frozen=True)
class AlgorithmSummary:
    """One registered algorithm, as ``qgis_process list`` describes it."""

    id: str
    provider: str
    name: str
    group: str = ""
    short_description: str = ""
    tags: tuple[str, ...] = ()
    deprecated: bool = False
    has_known_issues: bool = False

    @property
    def bare_name(self) -> str:
        """The id with its provider prefix removed."""
        _, _, bare = self.id.partition(":")
        return bare

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "group": self.group,
            "short_description": self.short_description,
        }
        if self.tags:
            payload["tags"] = list(self.tags)
        # Reported only when true: a flag on every entry reads as noise, and
        # these two are warnings rather than attributes.
        if self.deprecated:
            payload["deprecated"] = True
        if self.has_known_issues:
            payload["has_known_issues"] = True
        return payload


@dataclass(frozen=True)
class ProviderSummary:
    """One Processing provider, as actually registered."""

    name: str
    long_name: str
    is_active: bool
    algorithm_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "long_name": self.long_name,
            "is_active": self.is_active,
            "algorithm_count": self.algorithm_count,
        }


@dataclass
class Catalogue:
    """Everything registered in the running QGIS.

    Built from ``qgis_process --json list``, which is the authority §6.4 calls
    for: what is *actually registered*, not what the version is supposed to
    ship. On a given install ``grass:`` may be absent and ``saga:`` almost
    certainly is.
    """

    providers: dict[str, ProviderSummary] = field(default_factory=dict)
    algorithms: dict[str, AlgorithmSummary] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def __contains__(self, algorithm_id: str) -> bool:
        return algorithm_id in self.algorithms

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Catalogue:
        providers: dict[str, ProviderSummary] = {}
        algorithms: dict[str, AlgorithmSummary] = {}

        for provider_name, provider in (payload.get("providers") or {}).items():
            entries = provider.get("algorithms") or {}
            providers[provider_name] = ProviderSummary(
                name=provider_name,
                long_name=provider.get("long_name") or provider_name,
                is_active=bool(provider.get("is_active", True)),
                algorithm_count=len(entries),
            )
            for algorithm_id, details in entries.items():
                algorithms[algorithm_id] = AlgorithmSummary(
                    id=algorithm_id,
                    provider=provider_name,
                    name=details.get("name") or algorithm_id,
                    group=details.get("group") or "",
                    short_description=details.get("short_description") or "",
                    tags=tuple(details.get("tags") or ()),
                    deprecated=bool(details.get("deprecated", False)),
                    has_known_issues=bool(details.get("has_known_issues", False)),
                )

        versions = {
            key: value
            for key, value in payload.items()
            if key.endswith("_version") and isinstance(value, str)
        }
        return cls(providers=providers, algorithms=algorithms, versions=versions)

    # -- discovery ---------------------------------------------------------

    def suggest(self, algorithm_id: str, *, limit: int = DEFAULT_RESULT_LIMIT) -> list[AlgorithmSummary]:
        """Fuzzy matches for an unrecognised id (REQ-7).

        Ranked in three bands, because they are not equally likely to be what
        was meant:

        1. **Same bare name under a different provider.** This is the case
           REQ-7's rationale names outright -- ``native:buffer`` versus
           ``qgis:buffer`` differ across QGIS versions -- and it is almost
           always the answer, so it ranks above everything.
        2. **Close spellings** of the bare name, by edit distance.
        3. **Substring hits** on the bare name or the display name, which
           catches a half-remembered id like ``native:zonalstats``.
        """
        query = (algorithm_id or "").strip()
        if not query:
            return []
        _, _, bare = query.partition(":") if ":" in query else ("", "", query)
        bare = bare.lower()

        ranked: list[tuple[int, float, AlgorithmSummary]] = []
        seen: set[str] = set()

        def consider(algorithm: AlgorithmSummary, band: int, score: float) -> None:
            if algorithm.id in seen:
                return
            seen.add(algorithm.id)
            ranked.append((band, score, algorithm))

        for algorithm in self.algorithms.values():
            if algorithm.bare_name.lower() == bare:
                consider(algorithm, 0, 0.0)

        candidates = {
            algorithm.bare_name.lower(): algorithm
            for algorithm in self.algorithms.values()
            if algorithm.id not in seen
        }
        for match in difflib.get_close_matches(bare, candidates, n=limit, cutoff=0.6):
            ratio = difflib.SequenceMatcher(None, bare, match).ratio()
            consider(candidates[match], 1, -ratio)

        if len(bare) >= 3:
            for algorithm in self.algorithms.values():
                haystack = f"{algorithm.bare_name} {algorithm.name}".lower()
                if bare in haystack:
                    consider(algorithm, 2, float(len(algorithm.bare_name)))

        ranked.sort(key=lambda row: (row[0], row[1], row[2].id))
        return [algorithm for _, _, algorithm in ranked[:limit]]

    def search(
        self,
        query: str = "",
        *,
        provider: str | None = None,
        include_deprecated: bool = False,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> tuple[list[AlgorithmSummary], int]:
        """Search by id, name, group or tag.

        Returns the capped hits and the total that matched, so a caller can see
        that it is looking at a window rather than the whole answer.
        """
        needle = (query or "").strip().lower()
        hits: list[tuple[int, str, AlgorithmSummary]] = []

        for algorithm in self.algorithms.values():
            if provider and algorithm.provider != provider:
                continue
            if algorithm.deprecated and not include_deprecated:
                continue
            if not needle:
                hits.append((3, algorithm.id, algorithm))
                continue

            if needle == algorithm.id.lower() or needle == algorithm.bare_name.lower():
                band = 0
            elif needle in algorithm.bare_name.lower() or needle in algorithm.name.lower():
                band = 1
            elif needle in algorithm.group.lower() or any(
                needle in tag.lower() for tag in algorithm.tags
            ):
                band = 2
            elif needle in algorithm.short_description.lower():
                band = 3
            else:
                continue
            hits.append((band, algorithm.id, algorithm))

        hits.sort(key=lambda row: (row[0], row[1]))
        return [algorithm for _, _, algorithm in hits[:limit]], len(hits)

    def absence_report(self, *requested: str) -> dict[str, Any]:
        """Report registered providers, and explain any requested absences.

        The task-3 note requires absence to be reported plainly "rather than
        implying the algorithms exist". Silence about a missing provider is
        exactly that implication.
        """
        registered = sorted(self.providers)
        report: dict[str, Any] = {
            "registered_providers": registered,
            "provider_details": [
                self.providers[name].to_dict() for name in registered
            ],
        }
        missing = {}
        for name in requested:
            key = (name or "").strip().lower()
            if not key or key in self.providers:
                continue
            missing[key] = PROVIDER_ABSENCE_NOTES.get(
                key, f"no provider named {key!r} is registered in this QGIS install."
            )
        if missing:
            report["unavailable_providers"] = missing
        return report


@dataclass
class ProcessingRunner:
    """Invokes ``qgis_process`` and caches the catalogue.

    The catalogue costs a QGIS startup, so it is fetched once per server and
    reused. ``refresh=True`` exists for the case where a provider was enabled
    while the server was running.
    """

    config: Config
    _catalogue: Catalogue | None = None

    # -- invocation --------------------------------------------------------

    def _invoke(
        self,
        args: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        install = self.config.install
        return subprocess.run(
            [str(install.qgis_process_exe), *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout if timeout is not None else self.config.headless_timeout,
            env=install.child_env(),
            check=False,
        )

    # -- catalogue ---------------------------------------------------------

    def catalogue(self, *, refresh: bool = False, timeout: float | None = None) -> Catalogue:
        """The registered providers and algorithms.

        Raises ``QgisNotFound`` when the install is unusable, and
        ``ProcessingUnavailable`` when ``qgis_process`` runs but says nothing
        usable.
        """
        if self._catalogue is not None and not refresh:
            return self._catalogue

        completed = self._invoke(["--json", "list"], timeout=timeout)
        payload = _parse_json_payload(completed, what="qgis_process list")
        self._catalogue = Catalogue.from_payload(payload)
        return self._catalogue

    def describe(self, algorithm_id: str, *, timeout: float | None = None) -> Envelope:
        """Full parameter and output definitions for one algorithm.

        An unrecognised id gets REQ-7 treatment here too. Describing and
        running fail the same way for the same reason, and a caller that
        reached for ``help`` after a wrong id should not have to ask twice to
        find out what it should have typed.
        """
        invalid = _reject_malformed_id(algorithm_id)
        if invalid is not None:
            return invalid

        try:
            catalogue = self.catalogue(timeout=timeout)
        except (QgisNotFound, ProcessingUnavailable) as exc:
            return error(ErrorCode.QGIS_NOT_FOUND, str(exc))

        if algorithm_id not in catalogue:
            return _algorithm_not_found(algorithm_id, catalogue)

        completed = self._invoke(["--json", "help", algorithm_id], timeout=timeout)
        try:
            payload = _parse_json_payload(
                completed, what=f"qgis_process help {algorithm_id}"
            )
        except ProcessingUnavailable as exc:
            return error(ErrorCode.CHILD_FAILED, str(exc), algorithm_id=algorithm_id)

        return ok(
            {
                "algorithm": payload.get("algorithm_details", {}),
                "parameters": payload.get("parameters", {}),
                "outputs": payload.get("outputs", {}),
            }
        )

    # -- running -----------------------------------------------------------

    def run(
        self,
        algorithm_id: str,
        inputs: dict[str, Any] | None = None,
        *,
        project_path: Path | str | None = None,
        ellipsoid: str | None = None,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Envelope:
        """Run one Processing algorithm and return its structured results."""
        invalid = _reject_malformed_id(algorithm_id)
        if invalid is not None:
            return invalid

        resolved_inputs = dict(inputs or {})

        try:
            catalogue = self.catalogue(timeout=timeout)
        except (QgisNotFound, ProcessingUnavailable) as exc:
            return error(ErrorCode.QGIS_NOT_FOUND, str(exc))

        # REQ-7, checked against the catalogue before launching: a wrong id is
        # the common mistake, and resolving it should not cost a QGIS startup.
        if algorithm_id not in catalogue:
            return _algorithm_not_found(algorithm_id, catalogue)

        unsafe = _check_destinations(resolved_inputs, confirm=confirm)
        if unsafe is not None:
            return unsafe

        payload: dict[str, Any] = {"inputs": resolved_inputs}
        resolved_project = project_path or self.config.project_path
        if resolved_project is not None:
            resolved_project = Path(resolved_project).expanduser()
            if not resolved_project.exists():
                return error(
                    ErrorCode.BAD_REQUEST,
                    f"project file does not exist: {resolved_project}",
                    project_path=str(resolved_project),
                )
            payload["project_path"] = str(resolved_project)
        if ellipsoid:
            payload["ellipsoid"] = ellipsoid

        try:
            completed = self._invoke(
                ["run", algorithm_id, "-"],
                stdin=json.dumps(payload),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            effective = timeout if timeout is not None else self.config.headless_timeout
            return error(
                ErrorCode.TIMEOUT,
                f"{algorithm_id} exceeded {effective:.0f}s and was terminated. "
                f"Processing over a large raster can legitimately take minutes "
                f"-- raise timeout_seconds rather than reducing the input.",
                algorithm_id=algorithm_id,
                timeout_seconds=effective,
            )

        try:
            result_payload = _parse_json_payload(
                completed, what=f"qgis_process run {algorithm_id}"
            )
        except ProcessingUnavailable as exc:
            # The algorithm ran and failed on its own terms -- bad parameter
            # values, unreadable input. qgis_process reports these on stderr
            # with no JSON, so the captured text *is* the diagnosis and is
            # passed through rather than summarised.
            return error(
                ErrorCode.ALGORITHM_FAILED,
                f"{algorithm_id} failed: {exc}",
                algorithm_id=algorithm_id,
                exit_code=completed.returncode,
                stderr=(completed.stderr or "").strip()[-4000:] or None,
            )

        results = result_payload.get("results") or {}
        return ok(
            {
                "algorithm_id": algorithm_id,
                "results": results,
                "outputs": _describe_outputs(results),
                "inputs": result_payload.get("inputs") or resolved_inputs,
            },
            log=_collapse_log(result_payload.get("log")),
        )


class ProcessingUnavailable(Exception):
    """``qgis_process`` ran but produced nothing usable."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json_payload(
    completed: subprocess.CompletedProcess[str], *, what: str
) -> dict[str, Any]:
    """Pull the JSON document out of a ``qgis_process`` invocation.

    Located by its first ``{`` rather than by parsing the whole stream: QGIS
    writes provider and plugin chatter ahead of the document on some installs,
    and the JSON itself is always the remainder once it starts.
    """
    stdout = completed.stdout or ""
    start = stdout.find("{")
    if start == -1:
        detail = (completed.stderr or "").strip() or stdout.strip() or "(no output)"
        raise ProcessingUnavailable(
            f"{what} produced no JSON (exit {completed.returncode}): {detail[:2000]}"
        )
    try:
        payload = json.loads(stdout[start:])
    except json.JSONDecodeError as exc:
        raise ProcessingUnavailable(
            f"{what} produced unparseable JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProcessingUnavailable(
            f"{what} returned {type(payload).__name__}, expected a JSON object"
        )
    return payload


def _reject_malformed_id(algorithm_id: str) -> Envelope | None:
    """Reject an id that cannot possibly match, without launching QGIS."""
    if not algorithm_id or not algorithm_id.strip():
        return error(
            ErrorCode.BAD_REQUEST,
            "algorithm_id is required, in provider:name form (e.g. native:buffer).",
        )
    if not ALGORITHM_ID_PATTERN.match(algorithm_id.strip()):
        return error(
            ErrorCode.BAD_REQUEST,
            f"{algorithm_id!r} is not a well-formed algorithm id. Expected "
            f"provider:name, e.g. native:buffer or gdal:cliprasterbymasklayer. "
            f"Use search_processing_algorithms to find one.",
            algorithm_id=algorithm_id,
        )
    return None


def _algorithm_not_found(algorithm_id: str, catalogue: Catalogue) -> Envelope:
    """REQ-7: never fail bare on an unrecognised id.

    The requirements' recurring contract (§0) is to return the candidates
    rather than guess, on the grounds that an extra round-trip is consistently
    cheaper than a confidently wrong result the caller cannot detect. Here the
    candidates are fuzzy matches, plus -- when the id named a provider that is
    not registered at all -- the reason it is missing, which is a different and
    more useful answer than a list of near-spellings.
    """
    provider, _, _ = algorithm_id.partition(":")
    suggestions = catalogue.suggest(algorithm_id)
    detail: dict[str, Any] = {
        "algorithm_id": algorithm_id,
        "suggestions": [algorithm.to_dict() for algorithm in suggestions],
    }

    provider_key = provider.strip().lower()
    if provider_key and provider_key not in catalogue.providers:
        detail["provider_unavailable"] = {
            "provider": provider_key,
            "reason": PROVIDER_ABSENCE_NOTES.get(
                provider_key,
                f"no provider named {provider_key!r} is registered in this "
                f"QGIS install.",
            ),
            "registered_providers": sorted(catalogue.providers),
        }
        message = (
            f"No algorithm {algorithm_id!r}: its provider {provider_key!r} is "
            f"not registered in this QGIS install."
        )
    elif suggestions:
        shown = ", ".join(algorithm.id for algorithm in suggestions[:5])
        message = (
            f"No algorithm {algorithm_id!r} is registered. Closest matches: "
            f"{shown}."
        )
    else:
        message = (
            f"No algorithm {algorithm_id!r} is registered, and nothing similar "
            f"was found. Use search_processing_algorithms to browse what is "
            f"available."
        )

    return error(ErrorCode.ALGORITHM_NOT_FOUND, message, **detail)


def _check_destinations(inputs: dict[str, Any], *, confirm: bool) -> Envelope | None:
    """Guard destinations that already exist, or that are project files (REQ-4).

    REQ-4 enumerates deleting a layer, overwriting a project file and modifying
    features. A Processing run can do the second outright, and an output path
    pointing at an existing file is the same kind of irreversible local change
    the requirement exists to prevent -- QGIS has no undo across a scripted
    session. So both are gated.

    An output path that does not exist yet is not gated: creating a new file is
    the ordinary case, and gating it would make the tool unusable.
    """
    overwrites: list[dict[str, str]] = []
    for key, value in inputs.items():
        if not isinstance(value, str) or not value.strip():
            continue
        # Layer URIs carry provider options after a pipe; only the path can be
        # a destination. Memory and temporary outputs have no path at all.
        candidate = value.split("|", 1)[0].strip()
        if not candidate or candidate.startswith(("memory:", "TEMPORARY_OUTPUT")):
            continue
        try:
            path = Path(candidate).expanduser()
        except (OSError, ValueError):
            continue

        if path.suffix.lower() in PROJECT_SUFFIXES:
            overwrites.append(
                {
                    "parameter": key,
                    "path": str(path),
                    "reason": "writing a QGIS project file",
                }
            )
        elif key.upper().startswith("OUTPUT") and path.exists():
            overwrites.append(
                {
                    "parameter": key,
                    "path": str(path),
                    "reason": "an existing file would be overwritten",
                }
            )

    if not overwrites or confirm:
        return None

    listed = "; ".join(f"{row['parameter']} -> {row['path']} ({row['reason']})" for row in overwrites)
    return error(
        ErrorCode.CONFIRMATION_REQUIRED,
        f"this call would overwrite existing data and needs confirm=true: "
        f"{listed}. QGIS has no undo across a scripted session, so this is not "
        f"reversible. Pass a different destination, or confirm=true if the "
        f"overwrite is intended.",
        would_overwrite=overwrites,
    )


def _describe_outputs(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Summarise the result map, saying which outputs reached disk.

    A Processing result is a map of parameter name to value, and the values are
    a mixture: destination paths, scalar statistics, and layer ids. Reporting
    whether a path actually exists catches the case where an algorithm reports
    success but wrote nothing -- which happens with an empty input and is
    otherwise invisible until something downstream fails to open it.
    """
    described: list[dict[str, Any]] = []
    for key, value in sorted(results.items()):
        entry: dict[str, Any] = {"parameter": key, "value": value}
        if isinstance(value, str) and value.strip():
            candidate = value.split("|", 1)[0].strip()
            try:
                path = Path(candidate)
            except (OSError, ValueError):
                described.append(entry)
                continue
            if path.suffix and len(candidate) < 4096:
                exists = path.exists()
                entry["is_path"] = True
                entry["exists"] = exists
                if exists:
                    entry["size_bytes"] = path.stat().st_size
        described.append(entry)
    return described


def _collapse_log(log: Any) -> Any:
    """Keep a Processing log only when it says something.

    ``qgis_process`` returns ``"log": {}`` on a clean run. Carrying an empty
    object into every response trains the reader to ignore the field, which is
    the opposite of what it is for.
    """
    if not log:
        return None
    return log


__all__ = [
    "ALGORITHM_ID_PATTERN",
    "DEFAULT_RESULT_LIMIT",
    "PROVIDER_ABSENCE_NOTES",
    "AlgorithmSummary",
    "Catalogue",
    "ProcessingRunner",
    "ProcessingUnavailable",
    "ProviderSummary",
]
