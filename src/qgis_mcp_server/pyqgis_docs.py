"""PyQGIS API documentation, indexed from the local install (task 7).

REQ-6 governs the contract: an identifier with no exact match returns the
nearest namespace or partial match **with suggested siblings**, never a bare
"not found". ``tasks.md`` adds that the index is "pinned to the local QGIS
version", and ``design.md`` §7 explains why that is a constraint rather than a
nicety: "PyQGIS tracks the C++ API and both move each release. The doc index is
pinned to the local install; cross-version calls are a known limitation."

**The index comes from the type stubs, not from importing PyQGIS.** QGIS ships
``_core.pyi``, ``_gui.pyi``, ``_analysis.pyi`` and ``_3d_p.pyi`` -- about 3 MB
of ordinary text whose signatures are exactly what the local build exposes. So
the index is built by parsing them in this process, which means search costs no
QGIS startup at all and the whole of REQ-6 is answerable offline.

That choice also sidesteps a real obstacle: ``inspect.signature`` raises on SIP
methods, so importing the bindings would yield a list of names and no
signatures. The runtime does hold the prose documentation, in ``__doc__``, and
``get_pyqgis_class_docs`` fetches that across the process boundary when asked
for -- but only then, and the result is cached.

Nothing here imports PyQGIS.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qgis_mcp_protocol import Envelope, ErrorCode, ExecutionMode, error, ok
from qgis_mcp_server.qgis_install import QgisNotFound

#: Stub file to the module whose classes it declares.
STUB_MODULES = {
    "_core.pyi": "qgis.core",
    "_gui.pyi": "qgis.gui",
    "_analysis.pyi": "qgis.analysis",
    "_3d_p.pyi": "qgis._3d",
}

#: Bumped when the stored index shape changes, so a stale cache is rebuilt
#: rather than misread.
INDEX_FORMAT_VERSION = 1

DEFAULT_RESULT_LIMIT = 15

_CLASS_RE = re.compile(r"^(?P<indent>\s*)class\s+(?P<name>\w+)\s*(?:\((?P<bases>[^)]*)\))?\s*:")
_DEF_RE = re.compile(
    r"^(?P<indent>\s*)def\s+(?P<name>\w+)\s*\((?P<args>.*)\)\s*->\s*(?P<returns>.+?)\s*:\s*\.\.\.\s*$"
)
_ATTR_RE = re.compile(
    r"^(?P<indent>\s*)(?P<name>\w+)\s*=\s*\.\.\.\s*#\s*type:\s*(?P<type>.+?)\s*$"
)
_CLASSVAR_RE = re.compile(
    r"^(?P<indent>\s*)(?P<name>\w+)\s*:\s*typing\.ClassVar\[(?P<type>.+?)\]\s*$"
)

#: ``class_map.yaml`` lines look like ``Qgis.devVersion: src/core/qgis.h#L96``.
_CLASS_MAP_RE = re.compile(r"^(?P<symbol>[\w.]+)\s*:\s*(?P<location>\S+)\s*$")


class DocsUnavailable(Exception):
    """The stubs that the index is built from could not be read."""


@dataclass(frozen=True)
class Member:
    """One member of a class: a method, attribute, signal or nested class."""

    name: str
    kind: str
    #: One entry per overload. SIP exposes overloads as separate stub lines,
    #: and collapsing them would hide the one a caller actually needs.
    signatures: tuple[str, ...] = ()
    type_hint: str = ""
    source_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.signatures:
            payload["signatures"] = list(self.signatures)
        if self.type_hint:
            payload["type"] = self.type_hint
        if self.source_reference:
            payload["source_reference"] = self.source_reference
        return payload


@dataclass
class ClassEntry:
    """One PyQGIS class."""

    name: str
    module: str
    bases: tuple[str, ...] = ()
    members: dict[str, Member] = field(default_factory=dict)
    source_reference: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.module}.{self.name}"

    def method_names(self) -> list[str]:
        return sorted(
            name for name, member in self.members.items() if member.kind == "method"
        )

    def to_dict(self, *, include_members: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "module": self.module,
            "bases": list(self.bases),
            "member_count": len(self.members),
        }
        if self.source_reference:
            payload["source_reference"] = self.source_reference
        if include_members:
            payload["members"] = [
                member.to_dict() for _, member in sorted(self.members.items())
            ]
        return payload

    def summary(self) -> dict[str, Any]:
        return self.to_dict(include_members=False)


@dataclass
class DocIndex:
    """The parsed API surface of one QGIS install."""

    qgis_version: str
    classes: dict[str, ClassEntry] = field(default_factory=dict)
    #: Where the index came from, so a stale cache can be spotted.
    stub_files: tuple[str, ...] = ()

    # -- lookup ------------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self.classes

    def resolve(self, identifier: str) -> tuple[ClassEntry | None, Member | None]:
        """Resolve ``Class`` or ``Class.member``, as far as each part exists.

        Returns what was found rather than all-or-nothing: a class that exists
        with a member that does not is exactly REQ-6's case, and the caller
        needs the class in order to offer its siblings.
        """
        name = (identifier or "").strip()
        if not name:
            return None, None

        # A fully qualified name is accepted and reduced: a caller who read
        # `qgis.core.QgsVectorLayer` in an import line should not have to know
        # the index is keyed on the bare class name.
        for prefix in ("qgis.core.", "qgis.gui.", "qgis.analysis.", "qgis._3d.", "qgis."):
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break

        if name in self.classes:
            return self.classes[name], None

        if "." in name:
            head, _, tail = name.rpartition(".")
            # Nested classes are indexed under their dotted name, so try the
            # whole thing as a class before splitting off a member.
            if head in self.classes:
                entry = self.classes[head]
                return entry, entry.members.get(tail)
        return None, None

    # -- REQ-6 -------------------------------------------------------------

    def search(
        self, query: str, *, limit: int = DEFAULT_RESULT_LIMIT
    ) -> dict[str, Any]:
        """Look up an identifier, never failing bare (REQ-6).

        Four outcomes, and which one applies is always stated:

        * the identifier is a class, or a member of one -- returned directly;
        * the class exists but the member does not -- the nearest member by
          spelling, plus its siblings, which is REQ-6's "nearest namespace
          match with suggested siblings" verbatim;
        * the class does not exist -- the nearest class names;
        * nothing resembles it -- free-text matches over names.
        """
        raw = (query or "").strip()
        if not raw:
            return {
                "query": raw,
                "match": "none",
                "message": "no identifier was given",
                "classes": [],
            }

        entry, member = self.resolve(raw)

        if entry is not None and member is not None:
            return {
                "query": raw,
                "match": "exact_member",
                "class": entry.summary(),
                "member": member.to_dict(),
            }

        if entry is not None and member is None:
            wanted = raw.rpartition(".")[2]
            if wanted and wanted != entry.name and wanted not in entry.members:
                # REQ-6's central case: the namespace is right, the member is
                # not. Offering the class's own members is what turns a dead
                # end into one more round-trip.
                near = difflib.get_close_matches(
                    wanted, list(entry.members), n=5, cutoff=0.5
                )
                siblings = [
                    entry.members[name].to_dict()
                    for name in (near or entry.method_names()[:limit])
                ]
                return {
                    "query": raw,
                    "match": "namespace_only",
                    "message": (
                        f"{entry.name} exists but has no member named "
                        f"{wanted!r}. "
                        + (
                            f"Closest: {', '.join(near)}."
                            if near
                            else f"Its first {len(siblings)} methods are listed."
                        )
                    ),
                    "class": entry.summary(),
                    "nearest_members": near,
                    "siblings": siblings,
                }
            return {
                "query": raw,
                "match": "exact_class",
                "class": entry.to_dict(),
            }

        # No class resolved. Offer near spellings, then free text.
        head = raw.rpartition(".")[0] or raw
        candidates = difflib.get_close_matches(
            head, list(self.classes), n=limit, cutoff=0.6
        )
        if candidates:
            return {
                "query": raw,
                "match": "nearest_classes",
                "message": (
                    f"no class named {head!r}. Closest: "
                    f"{', '.join(candidates[:5])}."
                ),
                "classes": [self.classes[name].summary() for name in candidates],
            }

        hits = self.free_text(raw, limit=limit)
        if hits:
            return {
                "query": raw,
                "match": "text",
                "message": (
                    f"nothing is named {raw!r}; these names contain it."
                ),
                "classes": [entry.summary() for entry, _ in hits],
                "members": [
                    {"class": entry.name, **member.to_dict()}
                    for entry, member in hits
                    if member is not None
                ],
            }

        return {
            "query": raw,
            "match": "none",
            "message": (
                f"nothing in this QGIS build's API matches {raw!r}. The index "
                f"covers {len(self.classes)} classes from "
                f"{', '.join(sorted(set(STUB_MODULES.values())))}. Note it is "
                f"pinned to QGIS {self.qgis_version}, so a class added in a "
                f"later release will not be here."
            ),
            "classes": [],
        }

    def free_text(
        self, query: str, *, limit: int = DEFAULT_RESULT_LIMIT
    ) -> list[tuple[ClassEntry, Member | None]]:
        """Substring matches over class and member names, best-looking first."""
        needle = query.strip().lower()
        if len(needle) < 3:
            return []

        class_hits: list[tuple[int, str, ClassEntry]] = []
        member_hits: list[tuple[int, str, ClassEntry, Member]] = []

        for entry in self.classes.values():
            lowered = entry.name.lower()
            if needle == lowered:
                class_hits.append((0, entry.name, entry))
            elif lowered.startswith(needle):
                class_hits.append((1, entry.name, entry))
            elif needle in lowered:
                class_hits.append((2, entry.name, entry))

        if len(class_hits) < limit:
            for entry in self.classes.values():
                for name, member in entry.members.items():
                    lowered = name.lower()
                    if needle == lowered:
                        member_hits.append((0, f"{entry.name}.{name}", entry, member))
                    elif needle in lowered:
                        member_hits.append((3, f"{entry.name}.{name}", entry, member))
                if len(member_hits) > limit * 4:
                    break

        class_hits.sort(key=lambda row: (row[0], len(row[1]), row[1]))
        member_hits.sort(key=lambda row: (row[0], len(row[1]), row[1]))

        combined: list[tuple[ClassEntry, Member | None]] = [
            (entry, None) for _, _, entry in class_hits[:limit]
        ]
        for _, _, entry, member in member_hits:
            if len(combined) >= limit:
                break
            combined.append((entry, member))
        return combined

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": INDEX_FORMAT_VERSION,
            "qgis_version": self.qgis_version,
            "stub_files": list(self.stub_files),
            "classes": {
                name: {
                    "module": entry.module,
                    "bases": list(entry.bases),
                    "source_reference": entry.source_reference,
                    "members": [
                        [
                            member.name,
                            member.kind,
                            list(member.signatures),
                            member.type_hint,
                            member.source_reference,
                        ]
                        for member in entry.members.values()
                    ],
                }
                for name, entry in self.classes.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DocIndex:
        if payload.get("format_version") != INDEX_FORMAT_VERSION:
            raise DocsUnavailable(
                f"cached index format {payload.get('format_version')!r} is not "
                f"supported (this build reads {INDEX_FORMAT_VERSION})"
            )
        index = cls(
            qgis_version=payload.get("qgis_version", "unknown"),
            stub_files=tuple(payload.get("stub_files") or ()),
        )
        for name, raw in (payload.get("classes") or {}).items():
            entry = ClassEntry(
                name=name,
                module=raw.get("module", ""),
                bases=tuple(raw.get("bases") or ()),
                source_reference=raw.get("source_reference", ""),
            )
            for member_name, kind, signatures, type_hint, reference in raw.get(
                "members", []
            ):
                entry.members[member_name] = Member(
                    name=member_name,
                    kind=kind,
                    signatures=tuple(signatures),
                    type_hint=type_hint,
                    source_reference=reference,
                )
            index.classes[name] = entry
        return index


# ---------------------------------------------------------------------------
# Building the index
# ---------------------------------------------------------------------------


def _logical_lines(text: str):
    """Yield stub lines, joining any that continue across a newline.

    The stubs keep a ``def`` on one line almost always, but a long overload
    list occasionally wraps. Joining on unbalanced brackets costs nothing and
    means a wrapped signature is parsed rather than silently skipped.
    """
    buffer = ""
    depth = 0
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        buffer = stripped if not buffer else buffer + " " + stripped.strip()
        depth += stripped.count("(") - stripped.count(")")
        depth += stripped.count("[") - stripped.count("]")
        if depth <= 0:
            yield buffer
            buffer = ""
            depth = 0


def parse_class_map(path: Path) -> dict[str, str]:
    """Read a ``class_map.yaml`` into symbol to source location.

    QGIS writes some entries with the class name doubled --
    ``QgsVectorLayer.QgsVectorLayer.handledFeatures`` -- so a repeated leading
    segment is collapsed. Without that, nothing would ever match.
    """
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _CLASS_MAP_RE.match(line.strip())
        if not match:
            continue
        symbol = match.group("symbol")
        parts = symbol.split(".")
        if len(parts) >= 2 and parts[0] == parts[1]:
            parts = parts[1:]
        mapping.setdefault(".".join(parts), match.group("location"))
    return mapping


def parse_stub(text: str, module: str, locations: dict[str, str]) -> dict[str, ClassEntry]:
    """Parse one ``.pyi`` stub into class entries.

    Indentation decides structure, so a stack of open classes is kept and
    popped whenever a line appears at or above an enclosing class's indent.
    Nested classes are keyed by their dotted name -- ``QgsVectorLayer`` and
    ``QgsVectorLayer.LayerOptions`` are both looked up the same way.
    """
    classes: dict[str, ClassEntry] = {}
    stack: list[tuple[int, str]] = []

    for line in _logical_lines(text):
        indent = len(line) - len(line.lstrip())

        class_match = _CLASS_RE.match(line)
        if class_match:
            while stack and indent <= stack[-1][0]:
                stack.pop()
            name = class_match.group("name")
            qualified = f"{stack[-1][1]}.{name}" if stack else name
            bases = tuple(
                base.strip()
                for base in (class_match.group("bases") or "").split(",")
                if base.strip() and not base.strip().startswith("PyQt")
            )
            classes[qualified] = ClassEntry(
                name=qualified,
                module=module,
                bases=bases,
                source_reference=locations.get(qualified, ""),
            )
            if stack:
                parent = classes.get(stack[-1][1])
                if parent is not None:
                    parent.members[name] = Member(name=name, kind="class")
            stack.append((indent, qualified))
            continue

        if not stack:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            continue
        entry = classes[stack[-1][1]]

        def_match = _DEF_RE.match(line)
        if def_match:
            name = def_match.group("name")
            signature = (
                f"{name}({def_match.group('args').strip()}) -> "
                f"{def_match.group('returns').strip()}"
            )
            existing = entry.members.get(name)
            # Overloads arrive as separate lines for the same name; each is a
            # distinct way to call the method and all of them are kept.
            signatures = (
                existing.signatures + (signature,)
                if existing is not None and existing.kind == "method"
                else (signature,)
            )
            entry.members[name] = Member(
                name=name,
                kind="method",
                signatures=signatures,
                source_reference=locations.get(f"{entry.name}.{name}", ""),
            )
            continue

        attr_match = _ATTR_RE.match(line)
        if attr_match:
            name = attr_match.group("name")
            entry.members[name] = Member(
                name=name,
                kind="attribute",
                type_hint=attr_match.group("type").strip(),
                source_reference=locations.get(f"{entry.name}.{name}", ""),
            )
            continue

        classvar_match = _CLASSVAR_RE.match(line)
        if classvar_match:
            name = classvar_match.group("name")
            hint = classvar_match.group("type").strip()
            entry.members[name] = Member(
                name=name,
                # Qt signals are connected rather than called, so they are
                # labelled as such instead of filed with the attributes.
                kind="signal" if "pyqtSignal" in hint else "attribute",
                type_hint=hint,
                source_reference=locations.get(f"{entry.name}.{name}", ""),
            )

    return classes


def build_index(bindings_dir: Path, *, qgis_version: str = "unknown") -> DocIndex:
    """Parse every stub in a QGIS install's bindings directory."""
    bindings_dir = Path(bindings_dir)
    index = DocIndex(qgis_version=qgis_version)
    found: list[str] = []

    for filename, module in STUB_MODULES.items():
        stub = bindings_dir / filename
        if not stub.exists():
            continue
        short = module.rpartition(".")[2]
        locations = parse_class_map(bindings_dir / short / "class_map.yaml")
        text = stub.read_text(encoding="utf-8", errors="replace")
        for name, entry in parse_stub(text, module, locations).items():
            # First module to declare a name wins, and the order in
            # STUB_MODULES puts core first -- which is what a caller asking
            # about a name that exists in both almost always means.
            index.classes.setdefault(name, entry)
        found.append(filename)

    if not index.classes:
        raise DocsUnavailable(
            f"no PyQGIS type stubs found in {bindings_dir}. Expected at least "
            f"one of {', '.join(STUB_MODULES)}. Without them the API index "
            f"cannot be built, so documentation lookup is unavailable for this "
            f"install."
        )
    index.stub_files = tuple(found)
    return index


# ---------------------------------------------------------------------------
# The server-side runner
# ---------------------------------------------------------------------------

APIDOCS_SCRIPT = "apidocs.py"


@dataclass
class DocsRunner:
    """Answers documentation queries, building the index on first use.

    **The index is held in memory and never written to disk.** Parsing 3 MB of
    stubs takes under a fifth of a second, which is less than it would cost to
    read a 5 MB cache back -- and a cache would introduce a staleness bug for
    no gain, since the stubs it was built from are the pinned-to-this-install
    source in the first place.

    Prose documentation is cached per class, because *that* costs a QGIS
    startup and a caller reading through one class's API will ask repeatedly.
    """

    config: Any
    headless: Any
    _index: DocIndex | None = None
    _prose: dict[str, dict[str, Any]] = field(default_factory=dict)

    def index(self) -> DocIndex:
        """The API index, parsed from this install's stubs."""
        if self._index is None:
            install = self.config.install
            version = ".".join(
                str(part) for part in (install.version_hint or ())
            ) or "unknown"
            self._index = build_index(
                install.bindings_dir / "qgis", qgis_version=version
            )
        return self._index

    def search(self, query: str, *, limit: int = DEFAULT_RESULT_LIMIT) -> Envelope:
        """Look up an identifier (REQ-6). No QGIS startup."""
        try:
            index = self.index()
        except (DocsUnavailable, QgisNotFound) as exc:
            return error(
                ErrorCode.BAD_REQUEST,
                f"documentation lookup is unavailable: {exc}",
                mode=ExecutionMode.HEADLESS,
            )

        result = index.search(query, limit=max(1, limit))
        result["qgis_version"] = index.qgis_version
        result["indexed_class_count"] = len(index.classes)
        # Stated on every response, not just failures. design.md section 7
        # calls cross-version drift a known limitation, and a caller acting on
        # a signature needs to know which build it came from.
        result["index_note"] = (
            f"signatures come from the type stubs of the QGIS "
            f"{index.qgis_version} install in use, so they describe this build "
            f"exactly and may differ from another release."
        )
        return ok(result)

    def class_docs(
        self,
        class_name: str,
        *,
        members: list[str] | None = None,
        include_summaries: bool = True,
        timeout: float | None = None,
    ) -> Envelope:
        """Full documentation for one class: signatures plus prose."""
        try:
            index = self.index()
        except (DocsUnavailable, QgisNotFound) as exc:
            return error(
                ErrorCode.BAD_REQUEST,
                f"documentation lookup is unavailable: {exc}",
                mode=ExecutionMode.HEADLESS,
            )

        entry, _ = index.resolve(class_name)
        if entry is None:
            # REQ-6's contract applies here too: an unrecognised class name
            # comes back with the nearest ones rather than a bare failure.
            found = index.search(class_name, limit=DEFAULT_RESULT_LIMIT)
            return error(
                ErrorCode.BAD_REQUEST,
                found.get("message")
                or f"no class named {class_name!r} in this QGIS build's API.",
                mode=ExecutionMode.HEADLESS,
                **{
                    key: value
                    for key, value in found.items()
                    if key not in ("message", "query")
                },
            )

        result: dict[str, Any] = {
            "class": entry.to_dict(),
            "qgis_version": index.qgis_version,
        }

        requested = [str(name) for name in (members or [])]
        cache_key = f"{entry.name}|{','.join(sorted(requested))}|{include_summaries}"
        prose = self._prose.get(cache_key)

        if prose is None:
            envelope = self.headless.run_child(
                APIDOCS_SCRIPT,
                {
                    "operation": "class_docs",
                    "class": entry.name,
                    "members": requested or None,
                    "include_all_summaries": include_summaries,
                },
                timeout=timeout,
            )
            if envelope.ok:
                prose = envelope.result
                self._prose[cache_key] = prose
            else:
                # The signatures are already in hand, so a prose failure
                # degrades the answer rather than losing it. A name that is in
                # the stubs but not importable -- a few SIP internals are --
                # should still return its API surface.
                result["prose_unavailable"] = envelope.error
                result["note"] = (
                    "signatures are from the type stubs; the explanatory text "
                    "could not be read from the running bindings."
                )
                return ok(result)

        result["documentation"] = prose.get("documentation", "")
        result["summary"] = prose.get("summary", "")
        result["module"] = prose.get("module")
        if prose.get("members"):
            result["member_documentation"] = prose["members"]
        if prose.get("member_summaries"):
            result["member_summaries"] = prose["member_summaries"]
        if prose.get("truncated_members"):
            result["truncated_members"] = prose["truncated_members"]
        return ok(result)


__all__ = [
    "APIDOCS_SCRIPT",
    "DEFAULT_RESULT_LIMIT",
    "DocsRunner",
    "INDEX_FORMAT_VERSION",
    "STUB_MODULES",
    "ClassEntry",
    "DocIndex",
    "DocsUnavailable",
    "Member",
    "build_index",
    "parse_class_map",
    "parse_stub",
]
