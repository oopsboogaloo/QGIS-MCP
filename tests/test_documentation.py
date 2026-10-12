"""Task 7: PyQGIS documentation lookup.

`tasks.md` names one test: "an identifier with no exact match returns the
nearest namespace match with siblings, not a bare 'not found'." That is REQ-6,
and it is the central case here — but REQ-6 has four outcomes rather than two,
and each is checked, including that the *successful* ones still succeed.

Most of this suite runs in milliseconds. The index is parsed from the installed
build's type stubs in the server process, so search needs no QGIS startup at
all; only the prose lookup crosses the process boundary.
"""

from __future__ import annotations

import pytest

from conftest import CollectingServer, describe
from qgis_mcp_protocol import ErrorCode, ExecutionMode
from qgis_mcp_server.pyqgis_docs import (
    STUB_MODULES,
    DocIndex,
    build_index,
    parse_class_map,
    parse_stub,
)

#: A class that has existed for many releases and is not going anywhere, so a
#: test built on it is not hostage to an API change.
STABLE_CLASS = "QgsVectorLayer"
STABLE_MEMBER = "featureCount"


@pytest.fixture(scope="module")
def index(config):
    """The parsed API index for the installed QGIS."""
    bindings = config.install.bindings_dir / "qgis"
    if not any((bindings / name).exists() for name in STUB_MODULES):
        pytest.skip(f"no PyQGIS type stubs in {bindings}")
    return build_index(bindings, qgis_version="test")


@pytest.fixture(scope="module")
def docs(context):
    return context.docs


@pytest.fixture(scope="module")
def tools(context):
    from qgis_mcp_server.tools import documentation

    server = CollectingServer()
    documentation.register(server, context)
    return server


# ---------------------------------------------------------------------------
# Parsing the stubs
# ---------------------------------------------------------------------------


def test_index_covers_the_core_api(index):
    """A few thousand classes, from every stub the install ships."""
    assert len(index.classes) > 1000, (
        f"only {len(index.classes)} classes indexed, which suggests the parser "
        f"is dropping most of the stub"
    )
    assert STABLE_CLASS in index, sorted(index.classes)[:10]
    assert "_core.pyi" in index.stub_files, index.stub_files


def test_classes_carry_their_module_and_bases(index):
    entry = index.classes[STABLE_CLASS]
    assert entry.module == "qgis.core", entry.module
    assert "QgsMapLayer" in entry.bases, entry.bases


def test_overloads_are_all_kept(index):
    """PyQGIS overloads heavily, and the wrong one is a runtime TypeError.

    `featureCount` takes either a legend key or nothing. Collapsing the two
    into one signature would hide whichever the caller needs.
    """
    member = index.classes[STABLE_CLASS].members[STABLE_MEMBER]
    assert member.kind == "method"
    assert len(member.signatures) >= 2, (
        f"expected several overloads, got {member.signatures}"
    )
    assert any("legendKey" in signature for signature in member.signatures)
    assert any(
        signature.startswith(f"{STABLE_MEMBER}(self) ->")
        for signature in member.signatures
    ), member.signatures


def test_nested_classes_are_indexed_by_dotted_name(index):
    """`QgsVectorLayer.LayerOptions` is how a caller refers to it."""
    assert "QgsVectorLayer.LayerOptions" in index, "nested class missing"
    nested = index.classes["QgsVectorLayer.LayerOptions"]
    assert "loadDefaultStyle" in nested.members, sorted(nested.members)[:8]
    # And the parent records it as a member, so browsing finds it.
    assert index.classes[STABLE_CLASS].members["LayerOptions"].kind == "class"


def test_signals_are_distinguished_from_attributes(index):
    """A Qt signal is connected, not read, so the kind has to say so."""
    entry = index.classes[STABLE_CLASS]
    signal = entry.members.get("geometryChanged")
    assert signal is not None, sorted(entry.members)[:10]
    assert signal.kind == "signal", signal
    assert "pyqtSignal" in signal.type_hint, signal.type_hint


def test_attributes_carry_their_type(index):
    nested = index.classes["QgsVectorLayer.LayerOptions"]
    assert nested.members["loadDefaultStyle"].kind == "attribute"
    assert nested.members["loadDefaultStyle"].type_hint == "bool"


def test_source_references_come_from_the_class_map(index):
    """QGIS ships a symbol-to-source map, which is free provenance."""
    entry = index.classes[STABLE_CLASS]
    assert entry.source_reference.startswith("src/core/"), entry.source_reference
    assert "#L" in entry.source_reference, entry.source_reference


def test_class_map_collapses_the_doubled_prefix():
    """QGIS writes some entries with the class name twice.

    `QgsVectorLayer.QgsVectorLayer.handledFeatures`. Without collapsing the
    repeat, nothing would ever match and every source reference would be empty.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "class_map.yaml"
        path.write_text(
            "QgsFoo.QgsFoo.bar: src/core/foo.h#L10\n"
            "QgsBaz: src/core/baz.h#L20\n",
            encoding="utf-8",
        )
        mapping = parse_class_map(path)
    assert mapping["QgsFoo.bar"] == "src/core/foo.h#L10", mapping
    assert mapping["QgsBaz"] == "src/core/baz.h#L20", mapping


def test_parser_handles_a_wrapped_signature():
    """A long overload list occasionally wraps across lines.

    Joining on unbalanced brackets costs nothing and means a wrapped signature
    is parsed rather than silently skipped -- and a silently missing method is
    indistinguishable from one that does not exist.
    """
    stub = (
        "class QgsThing(QgsBase):\n"
        "    def wrapped(self, first: 'QgsA',\n"
        "                second: 'QgsB' = ...) -> bool: ...\n"
        "    def plain(self) -> int: ...\n"
    )
    classes = parse_stub(stub, "qgis.core", {})
    entry = classes["QgsThing"]
    assert "wrapped" in entry.members, sorted(entry.members)
    assert "plain" in entry.members
    assert "second" in entry.members["wrapped"].signatures[0]


def test_parser_ignores_pyqt_bases():
    """`PyQt6.sip.wrapper` is noise in a bases list, not API."""
    classes = parse_stub(
        "class QgsThing(PyQt6.sip.wrapper):\n    def go(self) -> None: ...\n",
        "qgis.core",
        {},
    )
    assert classes["QgsThing"].bases == (), classes["QgsThing"].bases


def test_index_round_trips_through_json(index):
    """The shape is serialisable, which keeps a future cache an option."""
    import json

    restored = DocIndex.from_dict(json.loads(json.dumps(index.to_dict())))
    assert len(restored.classes) == len(index.classes)
    original = index.classes[STABLE_CLASS].members[STABLE_MEMBER]
    recovered = restored.classes[STABLE_CLASS].members[STABLE_MEMBER]
    assert recovered.signatures == original.signatures
    assert recovered.kind == original.kind


# ---------------------------------------------------------------------------
# REQ-6: the four outcomes
# ---------------------------------------------------------------------------


def test_exact_class_resolves(index):
    result = index.search(STABLE_CLASS)
    assert result["match"] == "exact_class", describe(result)
    assert result["class"]["name"] == STABLE_CLASS
    assert result["class"]["members"], "an exact class should list its members"


def test_exact_member_resolves_with_its_signatures(index):
    result = index.search(f"{STABLE_CLASS}.{STABLE_MEMBER}")
    assert result["match"] == "exact_member", describe(result)
    assert result["member"]["name"] == STABLE_MEMBER
    assert result["member"]["signatures"], describe(result["member"])


def test_fully_qualified_name_resolves(index):
    """A caller copying from an import line should not have to strip it."""
    result = index.search(f"qgis.core.{STABLE_CLASS}")
    assert result["match"] == "exact_class", describe(result)
    assert result["class"]["name"] == STABLE_CLASS


def test_near_miss_member_returns_siblings(index):
    """The test `tasks.md` asks for, and REQ-6's central case.

    "An identifier with no exact match returns the nearest namespace match
    with suggested siblings, not a bare 'not found'." The namespace is right
    and the member is misspelled, so the answer is the class plus the members
    it actually has.
    """
    result = index.search(f"{STABLE_CLASS}.featureCont")
    assert result["match"] == "namespace_only", describe(result)
    assert result["class"]["name"] == STABLE_CLASS
    assert STABLE_MEMBER in result["nearest_members"], result["nearest_members"]
    assert result["siblings"], "siblings are the point of this answer"
    sibling_names = {entry["name"] for entry in result["siblings"]}
    assert STABLE_MEMBER in sibling_names, sorted(sibling_names)
    assert "no member named" in result["message"], result["message"]


def test_near_miss_member_siblings_carry_signatures(index):
    """A name alone is half an answer; the caller needs how to call it."""
    result = index.search(f"{STABLE_CLASS}.featureCont")
    match = next(
        entry for entry in result["siblings"] if entry["name"] == STABLE_MEMBER
    )
    assert match["signatures"], describe(match)


def test_misspelled_class_returns_the_nearest_names(index):
    result = index.search("QgsVectorLayerr")
    assert result["match"] == "nearest_classes", describe(result)
    names = [entry["name"] for entry in result["classes"]]
    assert STABLE_CLASS in names, names
    assert names[0] == STABLE_CLASS, (
        f"the closest match should lead; got {names[:3]}"
    )


def test_free_text_finds_classes_by_substring(index):
    """Not every lookup starts from a name the caller already has."""
    result = index.search("zonal")
    assert result["match"] == "text", describe(result)
    names = [entry["name"] for entry in result["classes"]]
    assert any("Zonal" in name for name in names), names


def test_free_text_prefers_a_prefix_match(index):
    result = index.search("QgsCoordinateTransform")
    # An exact class, since that name exists.
    assert result["match"] == "exact_class", describe(result)
    assert result["class"]["name"] == "QgsCoordinateTransform"


def test_nothing_matching_still_explains_itself(index):
    """The one case with no candidates must still leave a next move.

    REQ-6 forbids a bare "not found", and the version pin is the actionable
    part: a class added after this build genuinely is not here, and saying so
    is more use than a shrug.
    """
    result = index.search("QgsCompletelyImaginaryThing")
    assert result["match"] == "none", describe(result)
    assert "pinned to QGIS" in result["message"], result["message"]
    assert str(len(index.classes)) in result["message"], result["message"]


def test_empty_query_is_handled(index):
    result = index.search("   ")
    assert result["match"] == "none"
    assert "no identifier" in result["message"]


def test_short_query_does_not_scan_everything(index):
    """A two-character free-text query would match half the API."""
    assert index.free_text("ab") == []


# ---------------------------------------------------------------------------
# The search tool
# ---------------------------------------------------------------------------


def test_search_tool_needs_no_qgis_startup(tools, monkeypatch):
    """The only tool here that touches neither QGIS nor the network.

    Patching the child launcher to fail proves it: search answers from the
    stubs parsed in this process.
    """
    import qgis_mcp_server.headless as headless_module

    def refuse(*_args, **_kwargs):
        raise AssertionError("search_pyqgis_docs must not launch a child")

    monkeypatch.setattr(headless_module.HeadlessRunner, "run_child", refuse)
    payload = tools.tools["search_pyqgis_docs"](query=STABLE_CLASS)
    assert payload["ok"] is True, describe(payload)
    assert payload["match"] == "exact_class"


def test_search_tool_reports_the_version_it_indexed(tools):
    """design.md §7 names cross-version drift a known limitation.

    A caller acting on a signature needs to know which build it came from, so
    it is on every response rather than only on failures.
    """
    payload = tools.tools["search_pyqgis_docs"](query=STABLE_CLASS)
    assert payload["qgis_version"].startswith("4."), payload["qgis_version"]
    assert payload["indexed_class_count"] > 1000
    assert payload["qgis_version"] in payload["index_note"], payload["index_note"]
    assert "may differ from another release" in payload["index_note"]


def test_search_tool_reports_the_serving_mode(tools):
    payload = tools.tools["search_pyqgis_docs"](query=STABLE_CLASS)
    assert payload["mode"] == ExecutionMode.HEADLESS
    assert payload["mode_reason"]


def test_documentation_tools_are_mode_agnostic():
    from qgis_mcp_server import modes

    for name in ("search_pyqgis_docs", "get_pyqgis_class_docs"):
        assert name not in modes.MODE_NAMED_TOOLS, name


# ---------------------------------------------------------------------------
# get_pyqgis_class_docs
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def class_docs(tools):
    payload = tools.tools["get_pyqgis_class_docs"](
        class_name=STABLE_CLASS, members=[STABLE_MEMBER, "crs"]
    )
    assert payload["ok"] is True, describe(payload)
    return payload


def test_class_docs_returns_signatures_and_prose(class_docs):
    """The two halves: stubs for the shape, runtime for the explanation."""
    assert class_docs["class"]["name"] == STABLE_CLASS
    assert class_docs["class"]["members"], "signatures should be present"
    assert class_docs["module"] == "qgis.core"
    assert class_docs["summary"], "the class description should be present"
    assert "vector layer" in class_docs["summary"].lower(), class_docs["summary"]


def test_class_docs_returns_requested_member_prose(class_docs):
    documentation = class_docs["member_documentation"]
    assert STABLE_MEMBER in documentation, sorted(documentation)
    assert documentation[STABLE_MEMBER], "the method's docstring should be present"
    assert "crs" in documentation
    assert "spatial reference" in documentation["crs"].lower(), documentation["crs"]


def test_class_docs_summarises_every_member(class_docs):
    """One line each is how a caller finds their way around a 245-member class."""
    summaries = class_docs["member_summaries"]
    assert len(summaries) > 50, len(summaries)
    assert "crs" in summaries
    # First prose paragraph, not the signature line the stubs already cover.
    assert "->" not in summaries["crs"], summaries["crs"]


def test_class_docs_reports_a_member_that_does_not_exist(tools):
    """Omitting it would read as an undocumented member rather than a wrong name."""
    payload = tools.tools["get_pyqgis_class_docs"](
        class_name=STABLE_CLASS, members=["definitelyNotAMethod"], include_summaries=False
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["member_documentation"]["definitelyNotAMethod"] is None


def test_class_docs_works_for_a_nested_class(tools):
    payload = tools.tools["get_pyqgis_class_docs"](
        class_name="QgsVectorLayer.LayerOptions", include_summaries=False
    )
    assert payload["ok"] is True, describe(payload)
    assert payload["summary"], describe(payload)
    assert "options" in payload["summary"].lower(), payload["summary"]


def test_class_docs_applies_req6_to_an_unknown_class(tools):
    """The contract does not stop at the search tool.

    An unrecognised class name here comes back with the nearest ones, for the
    same reason: a dead end costs a round-trip either way, and candidates make
    the second one count.
    """
    payload = tools.tools["get_pyqgis_class_docs"](class_name="QgsVectorLayerr")
    assert payload["ok"] is False, describe(payload)
    assert payload["error"]["code"] == ErrorCode.BAD_REQUEST
    names = [entry["name"] for entry in payload["error"]["classes"]]
    assert STABLE_CLASS in names, names


def test_prose_is_cached_between_calls(docs, tools):
    """A caller reading through one class asks repeatedly.

    The second call must not pay another QGIS startup, which is observable:
    with the child launcher removed, a cached answer still comes back.
    """
    first = tools.tools["get_pyqgis_class_docs"](
        class_name="QgsRectangle", include_summaries=False
    )
    assert first["ok"] is True, describe(first)

    original = docs.headless.run_child
    try:
        docs.headless.run_child = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a cached class should not launch a child")
        )
        second = tools.tools["get_pyqgis_class_docs"](
            class_name="QgsRectangle", include_summaries=False
        )
    finally:
        docs.headless.run_child = original

    assert second["ok"] is True, describe(second)
    assert second["summary"] == first["summary"]


def test_signatures_survive_prose_being_unavailable(docs, tools, monkeypatch):
    """A partial answer beats none.

    A few names are in the stubs but are not importable, and the signatures are
    already in hand by the time the prose lookup runs -- so losing the prose
    must not lose the API surface.
    """
    from qgis_mcp_protocol import ErrorCode as Codes
    from qgis_mcp_protocol import error as make_error

    docs._prose.clear()
    monkeypatch.setattr(
        docs.headless,
        "run_child",
        lambda *a, **k: make_error(Codes.CHILD_FAILED, "simulated import failure"),
    )
    payload = tools.tools["get_pyqgis_class_docs"](class_name="QgsPointXY")

    assert payload["ok"] is True, describe(payload)
    assert payload["class"]["members"], "signatures should still be returned"
    assert payload["prose_unavailable"]["message"] == "simulated import failure"
    assert "could not be read" in payload["note"], payload["note"]
    docs._prose.clear()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_tools_register_with_their_contracts(config):
    import asyncio

    from qgis_mcp_server.server import build_server

    server, ctx = build_server(config)
    try:
        registered = asyncio.run(server.list_tools())
    finally:
        ctx.close()
    descriptions = {tool.name: tool.description or "" for tool in registered}

    for name in ("search_pyqgis_docs", "get_pyqgis_class_docs"):
        assert name in descriptions, sorted(descriptions)

    search = descriptions["search_pyqgis_docs"]
    # REQ-6's promise, and the version pin, are both things a caller cannot
    # infer from the signature.
    assert "never just says" in search, search
    assert "namespace_only" in search, search
    assert "pinned to the installed QGIS version" in search, search
    assert "overload" in search, search

    class_docs = descriptions["get_pyqgis_class_docs"]
    # That a prose failure still returns signatures is the behaviour most
    # likely to be misread as a total failure, so the description says it.
    assert "prose_unavailable" in class_docs, class_docs
