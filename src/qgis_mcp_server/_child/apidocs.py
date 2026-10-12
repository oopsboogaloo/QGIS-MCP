"""Reads PyQGIS prose documentation out of the running bindings (task 7).

The type stubs give signatures; they carry no prose. The *runtime* has it, in
``__doc__``, as reStructuredText generated from the C++ Doxygen comments. So
this is the one part of documentation lookup that needs the bindings imported,
and therefore a child process.

It exists separately from the index for a reason worth stating: searching --
and the whole of REQ-6, including the nearest-match-with-siblings contract --
is answered from the stubs in the server process with no QGIS startup at all.
Only a request for the actual explanatory text pays the two seconds.

Usage: ``python apidocs.py <request.json>``
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from qgis_mcp_protocol import ErrorCode, ExecutionMode, encode, error, ok

OPERATIONS = ("class_docs",)

#: Modules searched for a class name, in order. Core first, because a name
#: present in two modules is nearly always wanted from core.
MODULES = ("qgis.core", "qgis.gui", "qgis.analysis")

#: Cap on how much prose one member can contribute. Some PyQGIS docstrings run
#: to several screens of overload descriptions, and a response built from 200
#: of them unabridged would be unreadable and would crowd out everything else
#: a caller is holding.
MAX_MEMBER_DOC_CHARS = 4000


def _emit(envelope) -> None:
    sys.stdout.write("\n" + encode(envelope) + "\n")
    sys.stdout.flush()


def _fail(code: str, message: str, **detail):
    _emit(error(code, message, mode=ExecutionMode.HEADLESS, **detail))


def _first_paragraph(text: str) -> str:
    """The first prose paragraph of a docstring, skipping signature lines.

    SIP puts the signature on the opening line (and one line per overload), so
    the first *blank-line-delimited* block is often signatures rather than
    description. This walks past any line that looks like a call signature to
    find the sentence a reader actually wants.
    """
    if not text:
        return ""
    lines = text.strip().splitlines()
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        # A signature line: `name(args) -> type`. Skipped while looking for
        # prose, since the stubs already provide signatures more reliably.
        if "(" in stripped and ")" in stripped and "->" in stripped:
            if collected:
                break
            continue
        collected.append(stripped)
    return " ".join(collected)


def _resolve_class(name: str):
    """Find a class by name across the PyQGIS modules.

    Dotted names resolve through nested attributes, so
    ``QgsVectorLayer.LayerOptions`` works as well as ``QgsVectorLayer``.
    """
    import importlib

    parts = name.split(".")
    # A fully qualified name is accepted: a caller copying from an import line
    # should not have to strip the module themselves.
    if len(parts) > 1 and parts[0] == "qgis":
        module_name = ".".join(parts[:2])
        parts = parts[2:]
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            return None, None
        modules = [module]
    else:
        modules = []
        for module_name in MODULES:
            try:
                modules.append(importlib.import_module(module_name))
            except ImportError:
                continue

    for module in modules:
        target = module
        for part in parts:
            target = getattr(target, part, None)
            if target is None:
                break
        if target is not None:
            return target, getattr(module, "__name__", None)
    return None, None


def op_class_docs(request: dict) -> dict:
    name = request.get("class")
    wanted = request.get("members")
    include_all = bool(request.get("include_all_summaries", True))

    if not name:
        raise _Refusal(ErrorCode.BAD_REQUEST, "class is required")

    target, module_name = _resolve_class(str(name))
    if target is None:
        raise _Refusal(
            ErrorCode.BAD_REQUEST,
            f"{name!r} could not be resolved in {', '.join(MODULES)}. It may "
            f"exist in the type stubs but not be importable, which happens for "
            f"a few SIP-internal names.",
            searched_modules=list(MODULES),
        )

    class_doc = target.__doc__ or ""
    result = {
        "class": str(name),
        "module": module_name,
        "documentation": class_doc.strip(),
        "summary": _first_paragraph(class_doc),
        "members": {},
        "member_summaries": {},
        "truncated_members": [],
    }

    if wanted:
        for member_name in wanted:
            attribute = getattr(target, str(member_name), None)
            if attribute is None:
                # Reported rather than omitted: a silently missing member
                # reads as an undocumented one.
                result["members"][str(member_name)] = None
                continue
            doc = (getattr(attribute, "__doc__", None) or "").strip()
            if len(doc) > MAX_MEMBER_DOC_CHARS:
                doc = doc[:MAX_MEMBER_DOC_CHARS] + "\n... (truncated)"
                result["truncated_members"].append(str(member_name))
            result["members"][str(member_name)] = doc

    if include_all:
        for member_name in dir(target):
            if member_name.startswith("_"):
                continue
            attribute = getattr(target, member_name, None)
            doc = getattr(attribute, "__doc__", None) or ""
            summary = _first_paragraph(doc)
            if summary:
                result["member_summaries"][member_name] = summary[:300]

    return result


class _Refusal(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


HANDLERS = {"class_docs": op_class_docs}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _fail(ErrorCode.BAD_REQUEST, "apidocs expects one argument, a request file")
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _fail(ErrorCode.BAD_REQUEST, f"could not read request file: {exc}")
        return 2

    operation = request.get("operation")
    if operation not in HANDLERS:
        _fail(
            ErrorCode.BAD_REQUEST,
            f"unknown operation {operation!r}",
            supported_operations=list(OPERATIONS),
        )
        return 2

    from qgis.core import QgsApplication

    prefix_path = request.get("prefix_path") or os.environ.get("QGIS_PREFIX_PATH")
    if prefix_path:
        QgsApplication.setPrefixPath(str(prefix_path), True)
    # Documentation lookup reads docstrings off imported classes and touches no
    # providers, so the registry is never initialised -- which keeps this the
    # cheapest child call in the server.
    app = QgsApplication([], False)

    try:
        result = HANDLERS[operation](request)
    except _Refusal as refusal:
        _fail(refusal.code, str(refusal), operation=operation, **refusal.detail)
        return 1
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        _fail(
            ErrorCode.CHILD_FAILED,
            f"{type(exc).__name__}: {exc}",
            operation=operation,
            traceback=traceback.format_exc(),
        )
        return 1
    finally:
        del app

    _emit(ok(result, mode=ExecutionMode.HEADLESS, operation=operation))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
