# QGIS MCP Server

An MCP server exposing QGIS to an agent, over two independent execution paths:
a **live** path that talks to a listener plugin inside a running QGIS session,
and a **headless** path that drives `qgis_process` / a standalone
`QgsApplication` as a child process.

## Status

Early build. The specification is complete and lives in [`spec/`](spec/):

| Document | Contents |
|---|---|
| [`spec/requirements.md`](spec/requirements.md) | Normative obligations (EARS), with a traceability table |
| [`spec/design.md`](spec/design.md) | Architecture, execution paths, provider adapter layer, CRS policy |
| [`spec/tasks.md`](spec/tasks.md) | Build order, dependency-ordered |
| [`spec/qgis-mcp-server-spec.md`](spec/qgis-mcp-server-spec.md) | Narrative spec — source of context for the above |

Implemented so far:

- **Task 0** — synthetic test fixtures (`test-data/`)
- **Task 1** — headless `execute_pyqgis_code_for_cli`
- **Task 3** — `run_processing_algorithm`, `search_processing_algorithms`

## Spec deltas

Divergences from `spec/` found while building, recorded here rather than
silently absorbed. None changes a design decision; the spec has not been
amended.

| Where | Spec says | Reality | Resolution |
|---|---|---|---|
| `design.md` §2.4 | Use the **FastMCP** decorator API | FastMCP was renamed `MCPServer` in Python SDK 2.x (`from mcp.server.mcpserver import MCPServer`) | Same decorator API, same Pydantic schema derivation — only the import changed. Decision stands; pinned `mcp>=2.3.0` |
| `design.md` §3.2 | Headless falls back to "a generated standalone PyQGIS script" | Templating agent-authored code into a source file breaks on quoting, and reports traceback line numbers against the generated file rather than the submitted snippet | Code crosses the boundary as **JSON data** to a static runner. Same process topology, same fallback role — see `src/qgis_mcp_server/_child/runner.py` |
| `requirements.md` REQ-4 | Gates "delete a layer, overwrite a project file, or modify features" | A Processing destination pointing at an existing file is none of those literally, but is the same irreversible local change | `run_processing_algorithm` gates an existing destination *and* any `.qgz`/`.qgs` output behind `confirm=true`. An interpretation, not a silent extension — a destination that does not exist yet is never gated |
| `tasks.md` Task 0 | Fixtures include "one small real-world clip" | Synthetic values on real SU4080 coordinates cover the CRS *selection* tests without a download | Deferred to task 4.4, where a recorded real provider response is committed as a fixture anyway. See `test-data/README.md` |

## Requirements

- **QGIS 4.2** or newer. The project targets 4.2 deliberately
  (`spec/design.md` §6.2): 4.0 was the Qt5→Qt6 migration and 4.2 is the first
  4.x LTR. Qt6 is assumed throughout — plugin code imports through the
  `qgis.PyQt` shim, never `PyQt5`/`PyQt6` directly.
- **Python 3.12** for the server virtualenv. QGIS 4.2 bundles 3.12, and the
  shared protocol module must stay importable under both (`spec/design.md` §2.3).
- **[uv](https://docs.astral.sh/uv/)** for dependency management and distribution.

The server process never imports PyQGIS (`spec/design.md` §2.2) — it constructs
an environment for a child process instead. That is why the server runs in an
ordinary virtualenv and only needs to be pointed at a QGIS install once.

## Setup

```sh
uv sync
```

Then point the server at a QGIS install. Discovery is automatic where QGIS is
in a standard location; configure it explicitly when it is not, or when several
versions are installed side by side:

```sh
# Environment variable
export QGIS_MCP_QGIS_ROOT="/c/Program Files/QGIS 4.2.1"

# ...or qgis-mcp.toml in the working directory
[qgis]
root = "C:/Program Files/QGIS 4.2.1"
```

Validate the configuration before doing anything else. This checks the install
at startup rather than failing obscurely at the first tool call
(`spec/tasks.md` Task 1 acceptance):

```sh
uv run qgis-mcp --check
```

## Running

```sh
uv run qgis-mcp
```

## Tests

The suite runs entirely headless against synthetic fixtures (REQ-T2) and needs
no QGIS GUI interaction. Generate the fixtures once, then run it:

```sh
uv run python test-data/make_fixtures.py
uv run pytest
```

See [`test-data/README.md`](test-data/README.md) for the fixture contents and
the recorded truth that tests assert against.
