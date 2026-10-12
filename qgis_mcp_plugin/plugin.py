"""Plugin lifecycle: the menu entry, and starting and stopping the listener.

Task 6.1. Everything here runs on the QGIS main thread -- QGIS calls
``initGui`` and ``unload`` from it -- which is why the marshaller is
constructed here. Its thread affinity decides where every submitted command
executes, so building it anywhere else would silently relocate the whole
plugin's work onto the wrong thread.

Imports go through ``qgis.PyQt`` (``design.md`` §6.2), never ``PyQt5`` or
``PyQt6`` directly.
"""

from __future__ import annotations

import traceback

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from qgis_mcp_protocol.socket_frame import BIND_HOST, DEFAULT_PORT

from .commands import build_handlers
from .listener import QgisMcpListener
from .marshaller import MainThreadMarshaller

LOG_TAG = "qgis-mcp"

#: Settings keys, under QGIS's own QSettings so the choice survives a restart.
SETTING_PORT = "qgis_mcp/port"
SETTING_AUTOSTART = "qgis_mcp/autostart"


class QgisMcpPlugin:
    """Starts and stops the MCP listener from the Plugins menu."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self._marshaller: MainThreadMarshaller | None = None
        self._listener: QgisMcpListener | None = None
        self._action: QAction | None = None

    # -- lifecycle ---------------------------------------------------------

    def initGui(self) -> None:  # noqa: N802 - the name QGIS calls
        """Add the menu entry. Called on the main thread."""
        # Constructed here, on the main thread, and never moved. See the module
        # docstring: this object's thread is where all command work runs.
        self._marshaller = MainThreadMarshaller()

        self._action = QAction("Start MCP listener", self.iface.mainWindow())
        self._action.setCheckable(True)
        self._action.triggered.connect(self._toggle)
        self.iface.addPluginToMenu("QGIS MCP", self._action)

        if self._setting_bool(SETTING_AUTOSTART, False):
            self._start()

    def unload(self) -> None:
        """Remove the menu entry and stop the listener.

        Stopping matters more than tidying the menu: a listener left running
        after the plugin is unloaded would hold its port and keep answering
        commands against objects whose plugin no longer exists.
        """
        self._stop(quiet=True)
        if self._action is not None:
            self.iface.removePluginMenu("QGIS MCP", self._action)
            self._action = None
        self._marshaller = None

    # -- actions -----------------------------------------------------------

    def _toggle(self, checked: bool) -> None:
        if checked:
            self._start()
        else:
            self._stop()

    def _start(self) -> None:
        if self._listener is not None and self._listener.running:
            return
        if self._marshaller is None:
            self._marshaller = MainThreadMarshaller()

        port = self._setting_int(SETTING_PORT, DEFAULT_PORT)
        self._listener = QgisMcpListener(
            self._marshaller,
            build_handlers(),
            port=port,
            log=self._log,
        )
        try:
            bound = self._listener.start()
        except OSError as exc:
            self._listener = None
            self._set_checked(False)
            # Shown rather than only logged: the user asked for this and needs
            # to know it did not happen. A port already in use is the common
            # case and usually means a second QGIS is running.
            self._warn(
                f"Could not open the MCP listener on port {port}.\n\n{exc}\n\n"
                f"Another QGIS session may already have it. Change the port in "
                f"Settings → Options → Advanced under "
                f"{SETTING_PORT}, or stop the other listener."
            )
            return

        self._set_checked(True)
        if self._action is not None:
            self._action.setText("Stop MCP listener")
        self._message(
            f"MCP listener running on {BIND_HOST}:{bound}", Qgis.MessageLevel.Success
        )

    def _stop(self, *, quiet: bool = False) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001 - unload must not fail
                self._log("stopping the listener failed:\n" + traceback.format_exc())
        self._set_checked(False)
        if self._action is not None:
            self._action.setText("Start MCP listener")
        if not quiet:
            self._message("MCP listener stopped", Qgis.MessageLevel.Info)

    # -- helpers -----------------------------------------------------------

    def _set_checked(self, checked: bool) -> None:
        if self._action is None:
            return
        # Blocked so setting the state from code does not re-enter _toggle and
        # immediately undo what just happened.
        self._action.blockSignals(True)
        self._action.setChecked(checked)
        self._action.blockSignals(False)

    def _log(self, message: str) -> None:
        """Log from either thread.

        ``QgsMessageLog.logMessage`` is documented as thread-safe, which the
        listener relies on: it logs from the socket thread, and routing those
        through the marshaller would mean a log line could not be written while
        the main thread was busy -- exactly when one is most wanted.
        """
        QgsMessageLog.logMessage(message, LOG_TAG, Qgis.MessageLevel.Info)

    def _message(self, text: str, level) -> None:
        self._log(text)
        bar = self.iface.messageBar()
        if bar is not None:
            bar.pushMessage("QGIS MCP", text, level=level, duration=5)

    def _warn(self, text: str) -> None:
        self._log(text)
        QMessageBox.warning(self.iface.mainWindow(), "QGIS MCP", text)

    def _setting_int(self, key: str, default: int) -> int:
        from qgis.PyQt.QtCore import QSettings

        value = QSettings().value(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _setting_bool(self, key: str, default: bool) -> bool:
        from qgis.PyQt.QtCore import QSettings

        value = QSettings().value(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes")


__all__ = ["LOG_TAG", "SETTING_AUTOSTART", "SETTING_PORT", "QgisMcpPlugin"]
