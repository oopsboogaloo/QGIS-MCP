"""Failures a command can report, with the detail that must survive.

Shared by ``commands.py`` (which raises these) and ``listener.py`` (which turns
them into envelopes), so neither has to import the other.

The reason this exists as its own module rather than as bare exceptions: a
command failure usually carries structured detail the caller needs, and
stringifying an exception throws that away. The case that forced it is
:class:`CodeRaised` -- when agent-authored code fails, REQ-4a's backup path is
the single most important thing in the response, and a failure that reported
only ``RuntimeError: ...`` would have discarded it.
"""

from __future__ import annotations

from typing import Any

from qgis_mcp_protocol import ErrorCode


class CommandError(Exception):
    """A command failed in a way worth reporting as itself.

    Carries an error code and arbitrary detail, both of which reach the caller
    intact. Default code is ``BAD_REQUEST``: most command failures are the
    caller having asked for something impossible -- a layer that is not there,
    a parameter out of range.
    """

    def __init__(
        self, message: str, *, code: str = ErrorCode.BAD_REQUEST, **detail: Any
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = {key: value for key, value in detail.items() if value is not None}


class InterfaceUnavailable(CommandError):
    """A command needs the QGIS interface and there is none.

    Distinct from a plain failure because the remedy is different in kind: no
    parameter change will help, the command simply requires the desktop
    application. In a real session this cannot happen -- it exists for the
    headless harness, where the canvas commands are the ones that cannot be
    covered automatically.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.MODE_UNAVAILABLE)


class CodeRaised(CommandError):
    """Agent-authored code raised.

    The backup path travels with the failure, not beside it. REQ-4a makes that
    backup the reason raw execution is exempt from REQ-4's confirmation gate,
    so a caller whose code just failed needs to know where the snapshot is --
    and that is exactly the moment a response is least likely to be read
    carefully. It goes in the error payload.
    """

    def __init__(
        self,
        message: str,
        *,
        backup: dict[str, Any],
        stdout: str = "",
        traceback_text: str = "",
        exception_type: str = "",
    ) -> None:
        super().__init__(
            message,
            code=ErrorCode.CODE_RAISED,
            backup=backup,
            stdout=stdout or None,
            traceback=traceback_text or None,
            exception_type=exception_type or None,
            recovery=(
                f"the project state before this ran is saved at "
                f"{backup.get('backup_path')}. It covers project structure "
                f"only, not underlying data."
            ),
        )


__all__ = ["CodeRaised", "CommandError", "InterfaceUnavailable"]
