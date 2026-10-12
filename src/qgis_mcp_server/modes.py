"""Which execution path serves a tool, and how that is decided.

``spec/design.md`` §3.3: "tools are partitioned at registration into mode-named
and mode-agnostic sets, and the dispatcher consults that partition rather than
inferring from context." This module holds the partition and the resolution.

Keeping it as declared data rather than an inference has a specific payoff.
REQ-1a's scope is a *list* in the requirements document -- the two raw-execution
tools, the two screenshot tools, the navigation tools, and pointedly not
``render_map_to_path``. A rule that guessed from a tool's behaviour would get
``render_map_to_path`` wrong, because its Print Layout variant is headless-
capable while its canvas variant is not. So the list is written down.
"""

from __future__ import annotations

from dataclasses import dataclass

from qgis_mcp_protocol import ErrorCode, ExecutionMode, Envelope, error

#: Tools that name their execution mode, and so fall under REQ-1a: when the
#: mode they name is unavailable they return an error naming it, rather than
#: executing in the other one.
#:
#: Taken from REQ-1a's scope note verbatim. ``render_map_to_path`` is
#: deliberately absent -- see the module docstring.
MODE_NAMED_TOOLS: dict[str, str] = {
    "execute_pyqgis_code": ExecutionMode.LIVE,
    "execute_pyqgis_code_for_cli": ExecutionMode.HEADLESS,
    "get_screenshot_of_canvas": ExecutionMode.LIVE,
    "get_screenshot_of_window": ExecutionMode.LIVE,
    "zoom_to_layer": ExecutionMode.LIVE,
    "set_active_layer": ExecutionMode.LIVE,
    "toggle_layer_visibility": ExecutionMode.LIVE,
}


#: Mode-agnostic tools that actually have a live implementation.
#:
#: REQ-1b says a mode-agnostic tool should prefer the live session "when one is
#: reachable". That presumes the tool *can* run live, and most cannot: there is
#: no live counterpart to ``search_processing_algorithms`` or
#: ``export_heightmap``, and reporting ``live`` for one of those because a
#: session happens to be open would be a lie about where the work ran.
#:
#: So preference is declared per tool rather than inferred from reachability,
#: and a tool absent from this set reports headless with that as its reason --
#: which is a different statement from "no live session was found".
#:
#: **Empty, deliberately.** Task 6 gives the live path its mode-*named* tools:
#: raw execution, the screenshots, the navigation commands. The mode-agnostic
#: ones -- §4.2's introspection set, Processing, the terrain exports -- still
#: have only a headless implementation, so none of them belongs here yet.
#:
#: Listing one before it has a live path would make its reported reason a lie:
#: it would say "no live QGIS session is reachable" while a session sat open
#: and ignored. An empty set says the true thing instead, and each entry gets
#: added with the live implementation that earns it.
LIVE_CAPABLE_TOOLS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ModeDecision:
    """Which path will serve a call, and why.

    ``reason`` exists because REQ-1b's reported mode is not cosmetic. When a
    caller asks why their layer is not on screen, "headless, because no live
    session was reachable" answers it and "headless" does not.
    """

    mode: str
    reason: str

    @property
    def is_live(self) -> bool:
        return self.mode == ExecutionMode.LIVE


def resolve(tool_name, *, live_available=None) -> ModeDecision:
    """Decide which path serves ``tool_name`` (REQ-1b).

    Mode-named tools resolve to the mode they name -- availability is the
    caller's problem to report, per REQ-1a, because the error must name the
    missing mode rather than substitute the other one.

    Mode-agnostic tools prefer live when they have a live implementation *and*
    a session is reachable, and fall back to headless otherwise.

    Args:
        tool_name: The tool being dispatched.
        live_available: Whether a live session is reachable. Passed in rather
            than probed here, so a tool that never touches the live path costs
            no socket connect -- and so this module needs no global state,
            which would otherwise leak between server instances and between
            tests.
    """
    named = MODE_NAMED_TOOLS.get(tool_name)
    if named is not None:
        return ModeDecision(named, f"{tool_name} names the {named} mode")

    if tool_name not in LIVE_CAPABLE_TOOLS:
        return ModeDecision(
            ExecutionMode.HEADLESS,
            f"{tool_name} has no live implementation, so it runs headless "
            f"whether or not a QGIS session is open",
        )
    if live_available:
        return ModeDecision(
            ExecutionMode.LIVE, "a live session was reachable and is preferred"
        )
    return ModeDecision(
        ExecutionMode.HEADLESS,
        "no live QGIS session is reachable, so this ran headless against the "
        "project file on disk",
    )


def unavailable(tool_name: str, detail: str) -> Envelope:
    """Build the error for a tool whose only execution path is unavailable.

    The code differs by partition, and the distinction is the point. For a
    mode-named tool the mode itself is what is missing, which REQ-1a requires
    the error to say. For a mode-agnostic tool every path in the REQ-1b chain
    has been exhausted, and naming a mode would misdescribe it -- the problem
    is the QGIS install, not a choice between paths.
    """
    named = MODE_NAMED_TOOLS.get(tool_name)
    if named is not None:
        return error(
            ErrorCode.MODE_UNAVAILABLE,
            f"{named} execution is unavailable: {detail}",
            mode=named,
            missing_mode=named,
        )
    return error(
        ErrorCode.QGIS_NOT_FOUND,
        f"no execution path is available for {tool_name}: {detail}",
        mode=ExecutionMode.HEADLESS,
    )


__all__ = [
    "MODE_NAMED_TOOLS",
    "LIVE_CAPABLE_TOOLS",
    "ModeDecision",
    "resolve",
    "unavailable",
]
