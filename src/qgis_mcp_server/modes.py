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


#: Why the live path is never selected yet. Stated once, so the several places
#: that report it cannot drift into describing it differently.
_LIVE_NOT_IMPLEMENTED = (
    "the live listener plugin is not implemented in this build (tasks.md task "
    "6), so no live session is reachable"
)


def live_is_reachable() -> bool:
    """Whether a live QGIS session with the listener plugin can be reached.

    Always ``False`` here. Task 6 replaces this with a socket probe; it is a
    function rather than a constant so that the REQ-1b fallback chain below is
    already written in its final shape and task 6 changes one body, not every
    call site.
    """
    return False


def resolve(tool_name: str) -> ModeDecision:
    """Decide which path serves ``tool_name`` (REQ-1b).

    Mode-named tools resolve to the mode they name -- availability is the
    caller's problem to report, per REQ-1a, because the error must name the
    missing mode rather than substitute the other one.

    Mode-agnostic tools prefer live when reachable and fall back to headless.
    """
    named = MODE_NAMED_TOOLS.get(tool_name)
    if named is not None:
        return ModeDecision(named, f"{tool_name} names the {named} mode")
    if live_is_reachable():
        return ModeDecision(
            ExecutionMode.LIVE, "a live session was reachable and is preferred"
        )
    return ModeDecision(ExecutionMode.HEADLESS, _LIVE_NOT_IMPLEMENTED)


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
        live_unavailable_because=_LIVE_NOT_IMPLEMENTED,
    )


__all__ = [
    "MODE_NAMED_TOOLS",
    "ModeDecision",
    "live_is_reachable",
    "resolve",
    "unavailable",
]
