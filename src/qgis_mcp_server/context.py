"""What the tool modules are handed when they register.

Tools need the resolved configuration and the execution paths, and they need
them without reaching for module-level globals -- the tests construct a context
per case, pointing at a temp project or a deliberately broken QGIS root, and
global state would leak between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qgis_mcp_server.config import Config
from qgis_mcp_server.headless import HeadlessRunner


@dataclass
class ServerContext:
    """Everything the registered tools share for the life of one server."""

    config: Config
    headless: HeadlessRunner = field(init=False)

    def __post_init__(self) -> None:
        self.headless = HeadlessRunner(self.config)

    def close(self) -> None:
        self.headless.close()

    def __enter__(self) -> ServerContext:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


__all__ = ["ServerContext"]
