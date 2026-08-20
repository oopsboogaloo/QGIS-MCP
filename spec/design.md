# QGIS MCP Server — Design
Status: Draft v0.2
Derived from: `qgis-mcp-server-spec.md` §3 and §6
Companion documents: `requirements.md` (normative obligations, from §5/§9.5), `tasks.md` (build order, from §7)

---

## 0. About this document

This document covers *how* the server is built: process topology, language and runtime choices, the two execution paths, the extension points, and the environment concerns that have historically sunk PyQGIS projects. It does not restate the tool surface — that lives in the narrative spec — and it does not restate obligations, which live in `requirements.md`. Where a design choice exists to satisfy a requirement, the requirement is cited.

**One section is new.** §2 (language and runtime) records decisions taken in discussion that were never written into the narrative spec — the spec commits only to "Server language: Python". The elaborations here, particularly the process-boundary rule in §2.2, have not been through the same review as the rest and should be read as proposals rather than settled.

---

## 1. System overview

```
┌──────────────┐      MCP (stdio/SSE)      ┌───────────────────┐
│ Claude/agent │ ────────────────────────▶ │  qgis-mcp-server  │
└──────────────┘                           │  (Python process) │
                                           └─────────┬─────────┘
                                                     │
                  ┌──────────────────────────────────┼──────────────────────────────┐
                  │                                  │                              │
         (interactive mode)                  (headless mode)              (data acquisition)
                  │                                  │                              │
             TCP socket                       subprocess launch              provider adapters
                  │                                  │                              │
     ┌────────────▼────────────┐      ┌──────────────▼──────────────┐   ┌───────────▼───────────┐
     │  QGIS Plugin (listener)  │      │  qgis_process --json  /     │   │ REST · Catalogue ·    │
     │  inside live QGIS,       │      │  standalone QgsApplication  │   │ Overpass · OGC        │
     │  main-thread marshalled  │      │  (no GUI, one-shot)         │   │ (+ geocoding)         │
     └──────────────────────────┘      └─────────────────────────────┘   └───────────────────────┘
```

Three subsystems, deliberately independent: the two execution paths share a tool surface but no code path, and the acquisition layer is orthogonal to both — it produces files and layers that either path can then operate on.

---

## 2. Language and runtime

### 2.1 Python for both halves

The plugin half is not a choice. QGIS plugins are Python; the only alternative is a C++ plugin compiled against QGIS headers, which means matching their toolchain per-OS and recompiling every release. The test suite is likewise fixed: `qgis.testing` is Python `unittest`, and fixture generation is Python/GDAL.

The server half *could* be anything with an MCP SDK, since it reaches QGIS over a socket or a subprocess and both are language-agnostic. It is Python anyway, because the server's work is roughly 30% MCP plumbing and 70% constructing PyQGIS calls, parsing GIS responses, and talking to provider APIs that return GeoTIFF/GeoJSON/WFS. That second part is where Python's GIS ecosystem lives — GDAL/rasterio, OWSLib for the BGS OGC services, Overpass clients. A TypeScript or Go server would proxy all of it and additionally require maintaining a hand-written contract against the Python side.

### 2.2 The server process never imports PyQGIS

**Proposed rule: the server always crosses a process boundary to reach QGIS, and never does `import qgis.core` in its own process.**

The rationale is the friction described in §6.1. PyQGIS bindings are SIP-compiled against a specific QGIS build and Python version, so importing them requires the server to run under QGIS's bundled interpreter with `QGIS_PREFIX_PATH`/`PYTHONPATH` correct. Keeping the import out of the server process means it runs in an ordinary virtualenv and only has to *construct* the right environment for a child process. The setup problem shrinks from "make the server importable" to "point at the QGIS install once, in config" — and that config value is verifiable at startup rather than failing obscurely at first use.

The cost is one process boundary on every headless call. Given that the alternative is the single most common failure mode in PyQGIS tooling, that is a good trade.

### 2.3 Shared protocol module

Server and plugin exchange JSON over TCP. The request/response shapes are defined once in a small module vendored into both sides, with **no PyQGIS imports**, so it is import-safe in either environment.

**Constraint worth stating explicitly:** the shared module must target whichever Python QGIS bundles, not whatever the server virtualenv runs. The server may be on a newer interpreter; the plugin is not. No syntax newer than the QGIS-bundled version may appear in the shared file, even when the server itself could run it.

### 2.4 MCP framework

The `mcp` Python SDK's **FastMCP** decorator API, deriving tool schemas from type hints via Pydantic. This matters more than a stylistic preference here: the tool surface is roughly 28 tools with substantial parameter sets — bounding boxes, type lists, resolutions, formats, CRS identifiers — and hand-authoring those JSON schemas would be both the most tedious part of the build and the most likely to drift from the implementation.

Distribution via `uv`/`uvx`, which removes the historical Python packaging objection to a tool like this.

---

## 3. Execution paths

### 3.1 Interactive: the live listener plugin

A lightweight QGIS plugin opens a local TCP socket (settled in the narrative spec §8 — matching the Blender MCP approach). It is a persistent background listener, not an interactive REPL.

**Threading is the hard part, and the obvious design is wrong.** PyQGIS GUI objects — map canvas, layer tree, the project in a GUI session — are main-thread-only. Touching them from a worker thread does not raise a Python exception; it segfaults QGIS. The risk being managed is therefore *losing unsaved work to a crash*, not a stalled UI.

The pattern:

1. The socket listener accepts and reads on a background thread, and never executes there.
2. Submitted work is marshalled onto the main thread — `QTimer.singleShot(0, callable)`, or a signal connected with `Qt.QueuedConnection` to a slot owned by a main-thread object.
3. The background thread blocks on a future/event for the result, then writes the response.

This is structurally what the Blender MCP addon does, pulling queued work onto Blender's main thread via a modal timer operator.

**The consequence is accepted rather than hidden:** main-thread execution means a long operation freezes the GUI for its duration. Mitigated by a configurable socket-side timeout, so the agent receives an error rather than hanging — configurable because a legitimate Processing run on a large raster can take minutes.

**`QgsTask` has a real but narrow role.** A `QgsTask` touching the canvas will crash. The correct division is that GUI-free computation can and should run in a task off the main thread, with results applied back on the main thread. `run_processing_algorithm` is the case where this is worth the complexity, since those calls are long enough for the freeze to be felt.

### 3.2 Headless

**Prefer `qgis_process --json`** wherever the operation is a Processing algorithm. It is a supported CLI contract with stable JSON output, and markedly less fragile than hand-bootstrapping `QgsApplication`. Since `run_processing_algorithm` lands early in the build order and much of the terrain set (§4.7 of the spec) is thin wrappers over Processing, this covers more of the surface than it first appears.

Fall back to a generated standalone PyQGIS script, run as a subprocess under the QGIS-bundled interpreter, only for what Processing does not cover.

### 3.3 Mode selection

Governed by REQ-1, REQ-1a and REQ-1b. The design consequence: tools are partitioned at registration into mode-named and mode-agnostic sets, and the dispatcher consults that partition rather than inferring from context. Mode-agnostic tools attach the serving mode to their response — required by REQ-1b, and load-bearing because "the layer was added" means two different things depending on which path ran.

---

## 4. Provider adapter layer

Backs the data acquisition tools. The five v1 providers share almost no interface:

| Provider | Protocol shape | Why config alone doesn't cover it |
|---|---|---|
| OS Data Hub | REST + API key | Genuinely RESTful; needs a collection/product mapping in code, but close to config-driven |
| OpenTopography | REST + API key | Closest to config-driven — a single parameterised GET returns the GeoTIFF |
| Natural Earth | **No query API** | A fixed catalogue of zipped downloads by scale and theme. "Search" is filtering a static manifest; no server-side bbox filtering, so files are fetched whole and clipped locally |
| OpenStreetMap / Overpass | **Query language** | Requires generating Overpass QL per data type and converting Overpass JSON to features. Large areas need splitting or they time out |
| BGS | **OGC WMS/WFS** | `GetCapabilities` to discover, `GetFeature` to fetch — a standard, but a wholly different protocol, layered on the free-625k/paid-tier distinction |

**Interface.** Each adapter declares capabilities (data types served, coverage extent, whether credentials are required) and implements `search(aoi, types) -> [Dataset]` and `fetch(dataset, aoi, dest) -> path`.

**Protocol-family bases.** Adapters inherit from `RestApiAdapter`, `StaticCatalogueAdapter`, `OverpassAdapter` or `OgcAdapter`, so each protocol is written once. Within an existing family a new provider genuinely is close to a config entry; a provider introducing a new protocol shape needs a new base class. The claim that was wrong — and worth not reintroducing — is that *every* new provider is config-only.

**Dispatcher behaviours:**

- **Coverage pre-filtering.** Adapters declare their extent and are skipped before any network call when they cannot serve the AOI. Also lets the server answer "who could serve geology here" with no traffic at all.
- **Bounded parallel fan-out with per-adapter timeouts.** Queries are independent and run concurrently, but Overpass and Nominatim rate-limit, so concurrency is bounded per-provider.
- **Partial results, never a failed menu** (REQ-10). Failed, timed-out and credential-less adapters return a status alongside the successes.

**Geocoding** sits alongside the data adapters: ONS Open Geography and OS Names for UK places where available, Nominatim as the global no-key default. Resolutions are cached locally — it keeps Nominatim usage inside policy, and makes the tests deterministic without mocking the geocoder.

---

## 5. CRS policy

Providers disagree — OS and ONS serve EPSG:27700, OSM and Copernicus serve EPSG:4326, BGS varies by service — so any multi-provider acquisition arrives mixed.

**Why a policy is needed at all.** Mixed CRS is fine for display; QGIS reprojects on the fly. It bites in three places: distance/area analysis, combining rasters, and export to Blender. The first two are recoverable. The third fails silently — at 52°N one degree of longitude spans ~68.5 km against ~111.2 km for latitude, so a 4326 DEM treated as a square grid renders terrain stretched east–west by roughly 62%. The output looks like plausible terrain; it is simply not the shape of that place.

**Target selection** is derived per area of interest, not fixed per install (REQ-12):

| Tier | Condition | Target |
|---|---|---|
| 1 | AOI within the UK | EPSG:27700 |
| 2 | AOI within a single UTM zone | That UTM zone |
| 3 | AOI spans multiple zones | Server asks; returns candidates rather than choosing |

**The refinement that matters more than the tiers:** prefer whichever candidate projected CRS leaves the highest-resolution raster unresampled. Something always gets resampled; it should be the 30 m global DEM, not the 5 m national one. For UK work this falls out for free, since OS Terrain 5 is already 27700.

**Reprojection is eager, on registration, originals retained.** Lazy reprojection would make every tool CRS-aware and invites two layers silently disagreeing. The untouched download stays on disk so the resampling is re-derivable.

**AOI geometry travels as 4326; clip and export extents are in the target CRS.** Providers all accept 4326 bounding boxes, but a reprojected 4326 bbox is a curved quadrilateral rather than a rectangle, so clipping to it leaves a ragged edge and nodata to handle downstream.

**Vertical datums are recorded, not transformed** (REQ-13). See `requirements.md` for the reasoning; the design consequence is a metadata field per DEM and a comparison at the point of use, not a transformation pipeline.

---

## 6. Environment and installation

### 6.1 PyQGIS paths

Historically the single largest source of failure in standalone PyQGIS tooling: `PYTHONPATH` and `QGIS_PREFIX_PATH` must point at the QGIS install's bundled bindings. The §2.2 process-boundary rule confines this problem to constructing a child environment rather than the server's own, but it does not eliminate it. Install docs need a per-OS setup script, and the server should validate the configured QGIS path at startup rather than at first use.

### 6.2 Version target and Qt6

Targeting **QGIS 4.2**. Verified August 2026: 4.0 shipped 6 March 2026 as the Qt5→Qt6 migration, 4.2 followed 3 July 2026, and 4.2 becomes the first 4.x LTR in October 2026. 3.44 is the final 3.x release. Targeting 4.2 aims at the version about to become long-term stable rather than the end of the closing series.

Three consequences, the first of which invalidates the literal code in §3.1:

- **Import through the `qgis.PyQt` shim, never `PyQt5`/`PyQt6` directly.** PyQt6 scopes its enums, so `Qt.QueuedConnection` becomes `Qt.ConnectionType.QueuedConnection`. The shim keeps plugin source working across both. QGIS ships `pyqt5_to_pyqt6.py` for mechanical migration, relevant if any Blender-MCP-derived code is lifted across.
- **The plugin must declare `qgisMaximumVersion` in `metadata.txt`**, or QGIS 4 silently refuses to load it — a confusing first failure to debug.
- **Qt6 broke most third-party plugins.** Costs the listener nothing, since it is Qt6-native from the start, but any separately-relied-upon plugin may not be ported.

### 6.3 Provider credentials

OS Data Hub and OpenTopography require free registration for an API key. The server cannot complete a signup, so `check_api_credentials` exists to report what is missing and point at the registration page. Install docs need a "register here, put the key in this file" checklist alongside the PyQGIS path setup.

### 6.4 Processing provider availability

`search_processing_algorithms` must report what is *actually registered* in the running QGIS rather than a hardcoded provider list. `grass:*` is built in but requires GRASS installed; **`saga:*` is unavailable by default** — SAGA stopped being a core provider in QGIS 3.30 and needs a third-party plugin, which the server cannot install since plugin management is out of scope.

---

## 7. Design risks

- **Overpass at scale.** Large AOIs time out or are rejected. The `OverpassAdapter` will need tiling and result merging, which is the most likely place for the acquisition layer to need real work beyond its interface.
- **The GUI freeze is real.** §3.1's mitigation is a timeout, not a fix. If long Processing runs in live mode prove intolerable in practice, the escalation is moving more work into `QgsTask` — which increases the surface where a main-thread violation could be introduced.
- **Version drift.** PyQGIS tracks the C++ API and both move each release. The doc index is pinned to the local install; cross-version calls are a known limitation rather than something the server detects.
- **`fetch_elevation_data` duplicates a path.** It wraps `search_data_sources` + `download_and_register_layer` + `clip_to_aoi` for the most common case. The convenience is justified, but it is a second route to the same outcome and the two can drift — hence REQ-4d naming it explicitly so the confirmation gate cannot be bypassed through the wrapper.
