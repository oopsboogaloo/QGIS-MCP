"""Runs the live listener inside a headless QgsApplication, for testing.

``tasks.md`` 6.5 describes the smoke tests as launching QGIS with the plugin
loaded and running a few calls over the socket, and warns that a main-thread
violation "cannot be validated by a test that merely passes".

That warning stands for the *consequences* of a violation. The boundary itself
turns out to be testable, which is worth more than it sounds: a headless
``QgsApplication([], False)`` is still a ``QApplication`` and ``exec()`` runs a
real event loop, so a signal emitted from a worker thread is dispatched to the
main thread exactly as it would be in the GUI. Verified before this harness was
written -- emitted from thread A, slot observed running on thread B, where B is
the thread that called ``exec()``.

So this harness runs the real ``MainThreadMarshaller`` and the real
``QgisMcpListener``, with the real command handlers, and a test can prove
across the socket that work landed on the main thread. What it cannot cover is
the canvas and window commands, which need a GUI; those report the interface as
unavailable here and are left to the one-off manual check.

Not a test itself, and not importable as one: it is launched as a subprocess
under the QGIS interpreter by ``test_live_listener.py``.

Protocol with the launching test, on stdout, one JSON object per line:

* ``{"ready": true, "port": N, "main_thread": id, ...}`` once listening
* ``{"stopped": true}`` on clean exit

Usage: ``python live_harness.py [--project PATH] [--port N] [--lifetime SEC]``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback

#: Repo root, so the plugin package and the protocol module are importable.
#: The plugin is normally loaded by QGIS from a profile directory; here it is
#: imported directly, which is the same code by a different route.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for path in (_ROOT, os.path.join(_ROOT, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)


def say(payload: dict) -> None:
    """One JSON line on stdout, flushed.

    Flushed every time: the launching test blocks reading the ready line, and
    a buffered write would look like a harness that failed to start.
    """
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--lifetime", type=float, default=120.0)
    args = parser.parse_args(argv[1:])

    from qgis.core import QgsApplication, QgsProject
    from qgis.PyQt.QtCore import QTimer

    from qgis_mcp_plugin.commands import build_handlers
    from qgis_mcp_plugin.listener import QgisMcpListener
    from qgis_mcp_plugin.marshaller import MainThreadMarshaller

    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix:
        QgsApplication.setPrefixPath(prefix, True)
    app = QgsApplication([], False)
    app.initQgis()

    main_thread = threading.get_ident()
    logs: list[str] = []

    try:
        if args.project:
            if not QgsProject.instance().read(args.project):
                # Not fatal: a project with broken layers still exercises the
                # boundary, and get_missing_layers exists for exactly that.
                logs.append(f"project read reported problems: {args.project}")

        # Constructed on the thread that will call exec(), which is what makes
        # it the main thread for everything the listener submits.
        marshaller = MainThreadMarshaller()
        listener = QgisMcpListener(
            marshaller,
            _handlers_with_shutdown(build_handlers(), app),
            port=args.port,
            job_timeout=30.0,
            log=logs.append,
        )
        port = listener.start()

        say(
            {
                "ready": True,
                "port": port,
                "main_thread": main_thread,
                "project": QgsProject.instance().fileName() or None,
                "layer_count": len(QgsProject.instance().mapLayers()),
            }
        )

        # A hard lifetime, so a test that dies without sending `shutdown`
        # cannot leave a QGIS process holding the port until the machine is
        # rebooted.
        QTimer.singleShot(int(args.lifetime * 1000), app.quit)
        app.exec()

        listener.stop()
        say({"stopped": True, "logs": logs[-20:]})
        return 0
    except Exception as exc:  # noqa: BLE001 - must reach the launching test
        say(
            {
                "ready": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "logs": logs[-20:],
            }
        )
        return 1
    finally:
        app.exitQgis()


def _handlers_with_shutdown(handlers: dict, app) -> dict:
    """Add a ``shutdown`` command, so a test can stop the harness cleanly.

    Deliberately *not* part of the plugin's own command set: a running QGIS
    should not expose "quit the application" over a socket. It exists here only
    because the harness has no other way to be told it is finished.
    """

    def shutdown(_params: dict) -> dict:
        # Quitting from inside a handler means quitting from the main thread,
        # since that is where handlers run -- so the event loop sees it.
        app.quit()
        return {"stopping": True}

    return {**handlers, "shutdown": shutdown}


if __name__ == "__main__":
    sys.exit(main(sys.argv))
