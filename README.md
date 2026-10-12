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
- **Task 2** — `get_project_summary`, `get_layer_summary`, `get_layers_tree`,
  `get_missing_layers`, `get_project_path_info`
- **Task 3** — `run_processing_algorithm`, `search_processing_algorithms`
- **Task 4.0** — provider interface, capability declarations, and the
  dispatcher (coverage pre-filtering, bounded fan-out, REQ-10 partial
  results); `check_api_credentials`
- **Task 4.1** — `resolve_place` (Nominatim; the two UK gazetteers deferred)
- **Task 4.2** — `clip_to_aoi`
- **Task 4.3** — CRS policy and `select_target_crs`
- **Task 4.4** — the OpenTopography adapter
- **Task 4.5** — `search_data_sources`
- **Task 4.6** — `get_dataset_licence_info`, `check_api_credentials`
- **Task 4.7** — `download_and_register_layer`
- **Task 5.1** — `fetch_elevation_data`
- **Task 5.2** — `export_heightmap`
- **Task 5.3** — `export_georeferenced_texture`
- **Task 5.4** — `model_sea_level_change`
- **Task 5.5** — `export_vector_layer_for_mesh`
- **Task 5.6** — `render_map_to_path`
- **Task 6** — the live listener plugin (`qgis_mcp_plugin/`) and the live path:
  `get_live_session`, `execute_pyqgis_code`, `get_screenshot_of_canvas`,
  `get_screenshot_of_window`, `zoom_to_layer`, `set_active_layer`,
  `toggle_layer_visibility`, `list_project_backups`
- **Task 4.8** — the remaining provider adapters: OS Data Hub (NGD Features),
  OpenStreetMap (Overpass), Natural Earth, BGS (WMS)
- **Task 7** — `search_pyqgis_docs`, `get_pyqgis_class_docs`

Not started: **task 8** (write operations), which the spec defers.

## Spec deltas

Divergences from `spec/` found while building, recorded here rather than
silently absorbed. None changes a design decision; the spec has not been
amended.

| Where | Spec says | Reality | Resolution |
|---|---|---|---|
| `design.md` §2.4 | Use the **FastMCP** decorator API | FastMCP was renamed `MCPServer` in Python SDK 2.x (`from mcp.server.mcpserver import MCPServer`) | Same decorator API, same Pydantic schema derivation — only the import changed. Decision stands; pinned `mcp>=2.3.0` |
| `design.md` §3.2 | Headless falls back to "a generated standalone PyQGIS script" | Templating agent-authored code into a source file breaks on quoting, and reports traceback line numbers against the generated file rather than the submitted snippet | Code crosses the boundary as **JSON data** to a static runner. Same process topology, same fallback role — see `src/qgis_mcp_server/_child/runner.py` |
| `requirements.md` REQ-4 | Gates "delete a layer, overwrite a project file, or modify features" | A Processing destination pointing at an existing file is none of those literally, but is the same irreversible local change | `run_processing_algorithm` gates an existing destination *and* any `.qgz`/`.qgs` output behind `confirm=true`. An interpretation, not a silent extension — a destination that does not exist yet is never gated |
| `tasks.md` Task 4.1 | Three geocoders: Nominatim, ONS Open Geography, OS Names | Both UK gazetteers return EPSG:27700 coordinates, and converting them to the 4326 the AOI convention requires needs a PROJ transform — a different shape of adapter from Nominatim's single stdlib GET | Nominatim implemented, and it delivers the whole REQ-9 contract including boundary polygons. The two UK gazetteers are deferred to task 4.7, which already owns transforms; the tiering mechanism is in place so each is an adapter rather than a rework |
| `design.md` §3.1 | Marshal to the main thread with "`QTimer.singleShot(0, callable)`, **or** a signal connected with `Qt.QueuedConnection`" | The two are not interchangeable from the socket thread. Measured on QGIS 4.2.1 / Qt 6.11: `singleShot` returns without raising, the callable never fires (polled for two seconds), and from that point the **main event loop stops responding at all**. A timer must be started in the thread that will run it, and the socket thread has no event loop | The queued signal, which is the spec's own second option — `MainThreadMarshaller` owns a main-thread `QObject` and a `pyqtSignal` connected `Qt.ConnectionType.QueuedConnection`. The alternative is recorded because the spec presents it first and as an equal |
| `tasks.md` 6.2 | A main-thread violation "cannot be validated by a test that merely passes" | True of the *consequences*; not of the boundary. A headless `QgsApplication([], False)` is still a `QApplication`, `exec()` runs a real event loop, and a signal from a worker thread dispatches to the thread that called `exec()` exactly as in the GUI | The boundary **is** tested: `tests/live_harness.py` runs the real listener in a headless QGIS and the suite asserts across a socket that handlers execute on the main thread, verified by mutation (bypassing `submit()` fails all three assertions). Only the canvas and window captures still need a GUI, and they assert `mode_unavailable` here |
| `requirements.md` REQ-1b | A mode-agnostic tool prefers the live session "when one is reachable" | Most mode-agnostic tools have no live implementation — there is no live counterpart to `search_processing_algorithms` or `export_heightmap`, so reporting `live` because a session happens to be open would misstate where the work ran | Preference is declared per tool in `modes.LIVE_CAPABLE_TOOLS`, currently **empty**: task 6 delivers the mode-*named* live tools only. A tool absent from the set reports headless with "no live implementation" as its reason, which is a different statement from "no live session was found". Each entry gets added with the live implementation that earns it |
| `tasks.md` 4.8 | "Remaining adapters. Independent of each other; add one at a time." | Three of the four are vector, and the download pipeline was raster-only: `paths_for` hardcoded `suffix = ".tif"` and reprojection was `gdal:warpreproject`. Running OSM or OS through it would have written GeoJSON into a file named `.tif` | The pipeline derives the extension from `Dataset.format` and takes `native:reprojectlayer` for vector. Adapters that cannot be driven are not a deliverable, so this was done as part of 4.8 rather than deferred |
| `catalogue.py` BGS | "the 1:625,000-scale products are available under OGL" | The live service states its own terms, and they are broader than OGL: "made available for all uses - including commercial use". Separately, BGS's `BGS_Detailed_Geology` service is 1:50,000 — the *licensed* tier — and is trivially reachable | The adapter serves only the 625k service, and reads the access constraints from GetCapabilities rather than restating them, so the licence cannot go stale in a constant. The 50k service is deliberately not offered |
| `catalogue.py` OS Data Hub | Lists `elevation` and `admin_boundaries` among its data types | Both come from the OS *Downloads* API; the NGD Features API this adapter uses serves neither | The capability declaration is left alone — OS does offer them — and the adapter reports per data type where they actually live, so asking for UK elevation says "Downloads API, and OpenTopography works today" rather than returning nothing |
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

**On OneDrive:** this checkout lives under OneDrive, whose cloud-files filter
does not support hardlinks, and `uv` fails with `os error 396` when it tries.
Set `UV_LINK_MODE=copy` once and it goes away:

```powershell
setx UV_LINK_MODE copy
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
(`spec/tasks.md` Task 1 acceptance), and reports which provider keys are set:

```sh
uv run qgis-mcp --check
```

## Provider API keys

Two providers need a free API key. The server cannot complete a signup, so
`--check` and `check_api_credentials` report what is missing and where to get
it (`spec/design.md` §6.3).

| Provider | Register | Then |
|---|---|---|
| OS Data Hub | [osdatahub.os.uk](https://osdatahub.os.uk/) | API Dashboard → My Projects → Create a new project → Add an API to this project. Enable at least **OS Names** and **OS Features** |
| OpenTopography | [portal.opentopography.org](https://portal.opentopography.org/newUser) | MyOpenTopo Dashboard → Get an API Key → Request API key |

Copy [`qgis-mcp.toml.example`](qgis-mcp.toml.example) to `qgis-mcp.toml` and
fill in the `[credentials]` table, or set the environment variables
`QGIS_MCP_OS_DATA_HUB_KEY` / `QGIS_MCP_OPENTOPOGRAPHY_KEY`, which take
precedence. `qgis-mcp.toml` is gitignored.

Key values never appear in a tool response, a log line or `--check` output —
only presence, source and length. Length is reported because it distinguishes a
real key from an empty string left in a config file; no prefix or suffix is,
because a partial key is still a leaked key.

**Two limits worth knowing before you build on them.** OpenTopography's free
non-academic allowance is **50 calls per 24 hours**, which is why live fetching
stays an opt-in integration test rather than part of the default test pass. And
its free key **does not cover commercial integration** — that needs a separate
Enterprise key. `check_api_credentials` reports both under
`commercial_attention`.

## Running

```sh
uv run qgis-mcp
```

The headless path works on its own. The live tools additionally need the
listener plugin running inside QGIS.

## The live listener plugin

`qgis_mcp_plugin/` is a QGIS plugin. Install it with:

```sh
uv run python install_plugin.py            # link this checkout into the profile
uv run python install_plugin.py --list     # show profiles and what is installed
uv run python install_plugin.py --copy     # self-contained copy, to hand to someone
uv run python install_plugin.py --uninstall
```

Then in QGIS: **Plugins → Manage and Install Plugins → Installed**, tick
*QGIS MCP*, and start it from **Plugins → QGIS MCP → Start MCP listener**.
QGIS scans the plugin directory at startup, so restart it if it was already
running.

The default is a link (a directory junction on Windows, which needs no
administrator rights), so an edit to this checkout takes effect on the next
QGIS start with nothing to re-copy and no second copy to drift.

There is a script rather than a documented path for two reasons, both of which
produce a plugin that looks broken rather than mis-installed:

- **The profile tree is `QGIS4`, not `QGIS3`.** QGIS 4 shares nothing with 3.x
  — settings, plugins and saved connections do not carry across — and a plugin
  placed in the 3.x path simply never appears in the Plugin Manager.
- **The plugin needs `qgis_mcp_protocol`, which lives in `src/`.** It shares
  the envelope and framing contract with the server rather than restating it
  (`design.md` §2.3). The server imports it from its virtualenv and headless
  children get it staged onto `PYTHONPATH`, but a GUI QGIS adds only the
  profile's plugin directory to `sys.path`. So the plugin locates the package
  itself — through the link to this checkout, or from the `_vendor/` copy that
  `--copy` writes — and reports where it found it. See
  [`qgis_mcp_plugin/_bootstrap.py`](qgis_mcp_plugin/_bootstrap.py).

Port and autostart live in `QSettings` under `qgis_mcp/port` and
`qgis_mcp/autostart`; the server side is `live.port` in `qgis-mcp.toml` or
`QGIS_MCP_LIVE_PORT`.

Set `live.enabled = false` (or `QGIS_MCP_LIVE_ENABLED=0`) to keep the server
headless even with QGIS open.

Three things about it are deliberate rather than incidental:

- **The socket is loopback-only and not configurable.** It executes arbitrary
  Python inside your QGIS session, so `BIND_HOST` is pinned to `127.0.0.1` in
  code. There is no setting to widen it.
- **One client at a time**, and a second is told why rather than left hanging.
- **Nothing executes on the socket thread.** Touching PyQGIS off the main
  thread does not raise, it segfaults, so every command is marshalled across a
  queued Qt signal and the listener thread only ever reads bytes. `ping`
  reports the thread it ran on, which is how the test suite proves this.

Before live code execution, a project backup is written and its path returned —
that is what REQ-4a substitutes for REQ-4's confirmation gate. It covers
project *structure*: layer references, styling, layouts, CRS, settings. It does
**not** cover underlying data, so code that overwrites a GeoTIFF or deletes a
shapefile is not protected and not reversible. Restoring is manual in this
build (open the `.qgz`); `restore_project_backup` belongs to task 8.

## A worked acquisition

The acquisition path is complete end to end. A typical sequence:

1. **`resolve_place("Andover, Hampshire")`** → an AOI bbox in EPSG:4326, with a
   real boundary polygon. An ambiguous name comes back as `ambiguous_place` with
   candidates rather than a guess.
2. **`search_data_sources(place=...)`** → one menu grouped by data type, a
   per-provider status, any datasets excluded on area or latitude limits, and a
   preview of the CRS acquired layers would land in.
3. **`download_and_register_layer(provider, dataset, place=...)`** → refused,
   with the source, format, licence tier and approximate size stated (REQ-4d).
4. **The same call with `confirm=true`** → downloads, reprojects to the target
   CRS keeping the original on disk (REQ-12), and registers the layer with its
   vertical datum and provenance (REQ-13).
5. **`clip_to_aoi`** to trim to the exact area, by bbox or boundary polygon.

Or, for elevation, **`fetch_elevation_data`** does steps 2–5 in one call (with
the same confirmation gate), and **`export_heightmap`** turns the result into a
16-bit displacement map with the numbers Blender needs:

```
bits_per_channel         16
source_elevation_min_m   87.18      <- the real range 0..65535 maps to
source_elevation_max_m   126.01
extent_width_m           989.21     <- the ground it covers
extent_height_m          1059.87
aspect_ratio             0.933
displacement_ratio       0.0393     <- strength for a 1-unit-wide plane
nodata_cells             0
```

Set the plane's X size to 1, its Y size to 1/`aspect_ratio`, and the
displacement strength to `displacement_ratio`. Without those numbers the
vertical exaggeration is guesswork and differs between exports.

**`export_georeferenced_texture`** renders the same extent as an image with its
world box, so texture and heightmap UV-align. **`export_vector_layer_for_mesh`**
drapes roads or contours over the DEM as GeoJSON-with-Z or OBJ polylines. And
**`render_map_to_path`** exports a Print Layout to PNG, PDF or SVG — headless,
since a layout is a project object rather than a canvas.

One caution that applies to both rendering tools: **always use the returned
`extent`, never the one you asked for.** QGIS expands an extent to match the
output image's aspect ratio, so a 1 km square requested at 256×200 px really
renders 1.28 km × 1 km. A texture UV-mapped to the requested box would be out
by 140 m per side, with nothing in the image to show it. Passing `pixel_size`
instead of `width`+`height` avoids the expansion entirely.

What is *not* there yet: four of the five declared providers have no adapter, so
only global elevation is actually fetchable. `search_data_sources` names the gap
in `providers_not_yet_implemented` rather than letting it look like those
providers had nothing to offer.

## Looking up the API

`search_pyqgis_docs` indexes the PyQGIS API of the QGIS build actually
installed, by parsing the type stubs it ships (`_core.pyi` and friends). That
takes about 0.2 s for 3221 classes and needs no QGIS startup, so lookup is
instant — and the signatures describe *this* build rather than whatever release
the online docs happen to show.

It never answers a bare "not found". A misspelled member returns the class's
real members; a misspelled class returns the nearest names; anything else falls
back to a substring search:

```
QgsVectorLayer.featureCont  -> namespace_only   closest: featureCount, selectedFeatureCount, ...
QgsCoordinateRefSystem      -> nearest_classes  QgsCoordinateReferenceSystem, ...
hillshade                   -> text             QgsHillshadeFilter, QgsHillshadeRenderer, ...
```

`get_pyqgis_class_docs` adds the prose, read from the running bindings — which
costs one QGIS startup the first time a class is asked about and nothing after.

## Tests

The suite runs entirely headless against synthetic fixtures (REQ-T2) and needs
no QGIS GUI interaction. Generate the fixtures once, then run it:

```sh
uv run python test-data/make_fixtures.py
uv run pytest
```

The geocoder fixtures in `test-data/geocoder-cache/` are **committed**, unlike
the generated ones — they are recorded real Nominatim responses. The suite
reads them with network access disabled, so a fixture gap fails as an explicit
error rather than quietly going online and passing. Re-record with:

```sh
uv run python test-data/record_geocoder_fixtures.py
```

The OpenTopography fixtures in `test-data/provider-fixtures/` are committed for
the same reason — recorded real responses, including the two HTTP 400 error
bodies, which are where the actual parsing happens. Re-record with
`test-data/record_provider_fixtures.py` (spends 3 API calls).

**Integration tests are excluded from the default pass**, and that is not a
preference: they spend OpenTopography calls against an allowance of 50 per
24 hours, so a plain `pytest` must not touch them. Run them deliberately:

```sh
uv run pytest -m integration
```

See [`test-data/README.md`](test-data/README.md) for the fixture contents and
the recorded truth that tests assert against.

### The live listener, and what the suite cannot reach

`tests/test_live_listener.py` runs the **real** listener, marshaller and command
handlers inside a headless `QgsApplication` (`tests/live_harness.py`) and drives
them over a real socket. It is still part of the default pass — no GUI, no
network, no manual step.

Two things are left to a one-off manual check, because they need a desktop QGIS
and no automated assertion can stand in for them:

- `get_screenshot_of_canvas` and `get_screenshot_of_window` against a real
  canvas. Headlessly they correctly report `mode_unavailable`, which is
  asserted, but that is the absence of a canvas rather than a capture.
- `zoom_to_layer` and `set_active_layer` landing where you expect on screen.

Do that against a throwaway project — `test-data/generated/fixture_project.qgz`
— never one with unsaved work. A main-thread violation segfaults QGIS rather
than raising.
