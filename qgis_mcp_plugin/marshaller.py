"""Moving work from the socket thread onto the QGIS main thread.

This is the file ``tasks.md`` warns about:

    The design's hardest correctness requirement. Listener reads on a
    background thread; work is marshalled to the main thread; the reader
    blocks on the result. **A main-thread violation segfaults QGIS rather
    than raising** -- there is no exception to catch, so this cannot be
    validated by a test that merely passes. Review the threading boundary
    deliberately.

So the reasoning is written out here rather than left implicit.

**What must never happen.** PyQGIS objects backed by Qt GUI classes -- the map
canvas, the layer tree view, the project in a GUI session -- may only be
touched from the thread that created them, which is QGIS's main thread. Doing
it from a worker raises nothing. It corrupts Qt's internal state and the process
dies, taking any unsaved edits with it. The risk being managed is therefore
*losing the user's work*, not a stalled interface.

**Why ``QTimer.singleShot`` is not used, despite being the design's first
suggestion.** ``QTimer.singleShot(0, callable)`` creates its timer *in the
calling thread*. From the socket worker -- a bare ``threading.Thread`` with no
Qt event loop -- there is nothing to dispatch it.

Measured on QGIS 4.2.1 with Qt 6.11, the consequence is worse than the
callable not running, and worse than the docstring this replaces claimed:

1. the ``singleShot`` call returns without raising;
2. the callable never fires -- confirmed by polling for two seconds;
3. and from that point the **main event loop stops responding at all**.
   ``app.quit()`` from the worker is ignored, and a watchdog timer scheduled
   on the main thread *before* ``exec()`` never fires either. The process has
   to be killed.

In a real session that is a hung QGIS and the user's unsaved work gone, which
is precisely the harm §3.1 is written to avoid. So the trap is not a silent
no-op to be worked around but a hang to be stayed well clear of.

``design.md`` §3.1 offers the alternative in the same breath, and that is what
this uses: a signal emitted across threads into a slot owned by an object
living on the main thread.

**Why a signal is safe to emit from a worker at all.** Qt's signal delivery is
thread-aware. With ``Qt.ConnectionType.QueuedConnection`` the emission only
packages the arguments and posts an event to the receiver's event loop; the
slot body then runs on the receiver's thread. Emitting is the one Qt operation
deliberately designed to cross threads, and it is the only one this module
performs from the worker side.

**The handshake.** The worker emits, then waits on a ``threading.Event`` that
the main-thread slot sets once it has stored a result. Nothing is shared
mutably except that one job object, which exactly one thread writes to at a
time: the worker fills in the request and then only reads, the main thread only
writes the outcome. A timeout releases the worker without cancelling the job,
because a half-run GUI operation cannot be unwound -- the result is abandoned,
not interrupted.

Imports go through ``qgis.PyQt`` (``design.md`` §6.2): PyQt6 scopes its enums,
so ``Qt.QueuedConnection`` became ``Qt.ConnectionType.QueuedConnection``, and
the shim keeps this source working on either.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from typing import Any, Callable

from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal

#: How long the worker waits for the main thread, when no timeout is given.
#:
#: Generous because the main thread may legitimately be busy: a Processing run
#: over a large raster takes minutes, and the user may be mid-drag on the
#: canvas. ``design.md`` §3.1 makes the same point about the socket timeout --
#: a limit that fires on real work teaches the caller to distrust the tool.
DEFAULT_JOB_TIMEOUT = 300.0


class Job:
    """One unit of work handed from the socket thread to the main thread.

    Ownership is strictly sequenced, which is what makes this safe without a
    lock around the fields: the worker populates ``call`` and then does not
    touch the object again until ``done`` is set; the main thread writes
    ``result`` or ``error`` and then sets ``done``.
    """

    __slots__ = ("call", "result", "error", "traceback", "done", "job_id")

    def __init__(self, call: Callable[[], Any]) -> None:
        self.call = call
        self.result: Any = None
        self.error: BaseException | None = None
        self.traceback: str = ""
        self.done = threading.Event()
        self.job_id = uuid.uuid4().hex[:12]


class MainThreadMarshaller(QObject):
    """Runs callables on the thread that owns this object.

    Construct it on the QGIS main thread -- during plugin initialisation -- and
    never move it. Its own thread affinity is what decides where submitted work
    executes, so creating it anywhere else would silently relocate every
    operation this plugin performs.
    """

    #: Emitted by a worker, received on the main thread. The payload is the
    #: Job; `object` rather than a typed signature so no Qt metatype
    #: registration is needed for a plain Python object.
    _submitted = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # The thread this object belongs to, captured so a submission from the
        # main thread itself can be recognised and run directly. Without that
        # check, a main-thread caller would post an event to its own loop and
        # then block waiting for the loop it is itself blocking -- an instant
        # deadlock, and a very confusing one.
        self._home_thread = threading.get_ident()
        self._submitted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    # -- main thread -------------------------------------------------------

    def _run(self, job: Job) -> None:
        """Execute a job. **Always runs on the main thread.**

        Every exception is captured onto the job rather than propagating. An
        exception escaping a slot invoked from Qt's event loop would reach
        QGIS's own exception hook, where it becomes a dialog in the user's face
        and leaves the worker waiting out its full timeout for a result that is
        never coming.
        """
        try:
            job.result = job.call()
        except BaseException as exc:  # noqa: BLE001 - recorded, then reported
            job.error = exc
            job.traceback = traceback.format_exc()
        finally:
            # Set last and unconditionally: the worker is blocked on this, and
            # a path that skipped it would hang the connection rather than
            # fail it.
            job.done.set()

    # -- worker thread -----------------------------------------------------

    def submit(
        self, call: Callable[[], Any], *, timeout: float = DEFAULT_JOB_TIMEOUT
    ) -> Any:
        """Run ``call`` on the main thread and return its result.

        Blocks the calling thread until the main thread has finished, re-raising
        whatever the callable raised so a caller sees the same exception it
        would have seen running in-process.

        Raises:
            MainThreadTimeout: the main thread did not finish in time. The job
                is abandoned rather than cancelled -- a GUI operation part-way
                through cannot be unwound, and trying would be the very thing
                that corrupts state.
        """
        job = Job(call)

        if threading.get_ident() == self._home_thread:
            # Already on the main thread. Running directly is not an
            # optimisation but a correctness requirement: posting to the event
            # loop and then blocking on the result would deadlock, since the
            # thread that must dispatch the event is the one waiting.
            self._run(job)
        else:
            self._submitted.emit(job)
            if not job.done.wait(timeout):
                raise MainThreadTimeout(
                    f"the QGIS main thread did not complete this operation "
                    f"within {timeout:.0f}s. It may be busy with a long "
                    f"render or a modal dialog may be open. The operation was "
                    f"abandoned, not cancelled, so it may still complete."
                )

        if job.error is not None:
            raise job.error
        return job.result


class MainThreadTimeout(RuntimeError):
    """The main thread did not finish a submitted job in time."""


__all__ = [
    "DEFAULT_JOB_TIMEOUT",
    "Job",
    "MainThreadMarshaller",
    "MainThreadTimeout",
]
