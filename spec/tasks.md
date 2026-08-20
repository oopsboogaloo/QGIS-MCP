# QGIS MCP Server — Tasks
Status: Draft v0.2
Derived from: `qgis-mcp-server-spec.md` §7, with testing folded in per §9.6
Companion documents: `requirements.md` (normative obligations), `design.md` (architecture)

---

## 0. About this document

The build order, broken into units of work. Tasks are ordered by dependency and by how much they de-risk what follows, not by tool count — task 4 is the largest by a wide margin despite looking like one line in the original outline.

**Definition of done (REQ-T1).** A task is complete when the tool *and its fixture-based test* are both written, in the same commit. No tool counts as done without its test. Testing is not a phase here; it is part of each unit of work.

**Failure output (REQ-T3)** applies to every test in every task rather than to any one of them: assertions must report expected versus actual structured values, not just pass/fail. This is a property of how tests are written throughout, so it is stated once here instead of being repeated per task. It exists so a failing run is diagnosable directly from its output, without anyone re-running the case by hand to find out what it actually produced.

**Task IDs are stable.** Sub-tasks use dotted numbers. Top-level numbers match the original §7 outline so that existing references stay valid.

**Every test runs headless** (REQ-T2), against synthetic fixtures, with no GUI interaction — except task 6, which is inherently interactive and gets a thinner smoke-test layer instead.

---

## 1. Dependency overview

```
  0  fixtures
  │
  1  execute_pyqgis_code_for_cli ── proves the environment works
  │
  2  introspection (read-only)
  │
  3  run_processing_algorithm ──────┬──────────────┐
  │                                 │              │
  4  acquisition + CRS + clip       │              │
  │                                 │              │
  5  terrain / Blender export ──────┘              │
                                                   │
  6  live listener plugin ─────────────────────────┘
  7  documentation tools     (no dependencies — parallel at any point)
  8  write operations        (deferred until 1–6 stable)
```

**Critical path:** 0 → 1 → 3 → 4 → 5. Tasks 2, 6 and 7 hang off it without blocking it.

**Parallelisable:** task 7 at any time. Task 2 any time after 1. Within task 4, adapters after the first are independent of each other (REQ-10 means a half-populated provider list still returns a working menu).

---

## 2. Tasks

### Task 0 — Synthetic test fixtures
**Depends on:** nothing. Everything downstream depends on this.

Create `test-data/` with small, deterministic, exactly-predictable inputs.

**Deliverables:**
- Tiny DEM (~50×50 px GeoTIFF) with a known elevation pattern
- **Nodata cells** in that DEM — required, not incidental
- **A landlocked basin below sea level** in that DEM — also required
- A handful of vector features with known geometry and attributes
- One small real-world clip (e.g. a known UK grid square) for CRS/reprojection tests
- A fixture `.qgz` project referencing the above

**Acceptance:** every fixture property a later test will assert against is documented alongside the fixture — feature counts, CRS codes, field schema, elevation min/max — so tests assert against recorded truth rather than against whatever the file happens to contain.

**Why the two required DEM features:** they are the cases separating a correct terrain implementation from one that merely looks correct. Nodata catches REQ-11's normalisation trap; the landlocked basin is the only way to test `model_sea_level_change`'s connectivity option in both directions. A clean synthetic DEM lets both bugs through undetected.

---

### Task 1 — Headless `execute_pyqgis_code_for_cli`
**Depends on:** 0
**Satisfies:** REQ-2, REQ-3, REQ-1a (headless half)

The first real task because it proves the PyQGIS environment and path setup work at all — the historically most common failure point (`design.md` §6.1). No plugin needed.

**Deliverables:** the tool; the child-process environment construction per `design.md` §2.2; startup validation of the configured QGIS path.

**Test:** run trivial code against the fixture project, assert the structured `result` return.

**Acceptance:** a deliberately wrong `QGIS_PREFIX_PATH` produces a clear diagnostic at startup, not an obscure import error at first call.

---

### Task 2 — Headless introspection
**Depends on:** 1 · **Parallel-safe**
**Satisfies:** REQ-1b (reporting the serving mode)

`get_project_summary`, `get_layer_summary`, `get_layers_tree`, `get_missing_layers`, `get_project_path_info`.

Read-only and low risk, which makes this the right place to establish the mode-reporting convention before anything depends on it.

**Test:** assert exact fixture properties — feature count, CRS EPSG code, field names and types.

---

### Task 3 — Processing algorithms
**Depends on:** 1
**Satisfies:** REQ-7

`run_processing_algorithm` and `search_processing_algorithms`. Highest practical value per unit of work, and the foundation both the acquisition and terrain sets are built on.

**Deliverables:** `qgis_process --json` invocation path (`design.md` §3.2); fuzzy ID matching for REQ-7; provider enumeration reporting what is *actually registered* rather than a hardcoded list.

**Test:** known algorithm + fixture input → hand-calculable expected output. Separately, assert a slightly-wrong algorithm ID returns fuzzy matches rather than a bare failure.

**Note:** `saga:*` will be absent unless SAGA's third-party provider is installed, and plugin installation is out of scope. The tool must report absence plainly rather than implying the algorithms exist.

---

### Task 4 — Data acquisition, CRS policy, clipping
**Depends on:** 3
**Satisfies:** REQ-4d, REQ-8, REQ-9, REQ-10, REQ-12

The largest task by a wide margin — five providers spanning four protocol families, plus the CRS policy. Built incrementally: each sub-task below is independently shippable.

#### 4.0 — Adapter interface and dispatcher
Interface per `design.md` §4: capability declaration, `search(aoi, types)`, `fetch(dataset, aoi, dest)`. Dispatcher with coverage pre-filtering and bounded parallel fan-out.

**Test:** REQ-10 directly — inject one failing, one timing-out and one credential-less adapter; assert the menu still returns successful results with per-provider statuses attached.

#### 4.1 — `resolve_place`
Geocoding adapters (Nominatim as no-key default; ONS Open Geography and OS Names for UK) plus the local resolution cache.

First because `search_data_sources` depends on it for place-name input, and REQ-9's ambiguity contract is easiest to get right before anything is layered on top.

**Test:** cached real responses committed as fixtures. An ambiguous name returns the candidate list; an unambiguous one proceeds without asking. No geocoder mocking needed — the cache makes it deterministic.

#### 4.2 — `clip_to_aoi`
**Depends only on 3** — can be built at any point in this task, or before it.

A thin wrapper over `native:clip` and `gdal:cliprasterbymasklayer`. Built here rather than with the terrain set because the acquisition flow needs it: provider extents routinely exceed the requested area, so without it every acquisition ends in untrimmed layers.

**Test:** both input forms — a bounding box for the DEM, a boundary polygon for the vector layers.

#### 4.3 — CRS policy
Tier selection per REQ-12, eager reprojection on registration, originals retained.

**Test:** tier selection against AOIs with known correct answers — a UK bbox resolves to EPSG:27700, a single-zone non-UK bbox to its UTM zone, a continental bbox returns candidates rather than a CRS. Assert the original file on disk is byte-identical after reprojection. The highest-resolution-raster refinement gets its own case: given a 27700 DEM and a 4326 vector, assert the DEM was not resampled.

Tiers 1 and 2 are deterministic and fully testable offline. Tier 3 needs no projection maths at all — only the candidate list.

#### 4.4 — First adapter: OpenTopography
The natural first: closest to config-driven, and serves the highest-priority data type. Establishes the `RestApiAdapter` family.

**Test:** recorded real response committed as a fixture, pinning the parsing.

#### 4.5 — `search_data_sources`
Grouped multi-type results per REQ-8; place-name resolution via REQ-9.

**Test:** a multi-type call returns one grouped response, not one per type.

#### 4.6 — `get_dataset_licence_info` and `check_api_credentials`
Both read from adapter capability declarations. Neither needs network access in test.

**Note:** REQ-10 makes `check_api_credentials` a diagnostic rather than a required pre-flight — missing keys already surface inline during search.

#### 4.7 — `download_and_register_layer`
**Last in this task.** The only tool needing real network calls, and the confirmation gate (REQ-4d) must be correctly wired before it is safe to use unattended.

**Test:** assert an unconfirmed call is refused. Live fetching covered by a single opt-in integration test against a known small dataset, run occasionally rather than in the default pass.

#### 4.8 — Remaining adapters
OS Data Hub (`RestApiAdapter`), Natural Earth (`StaticCatalogueAdapter`), OpenStreetMap (`OverpassAdapter`), BGS (`OgcAdapter`). Independent of each other; add one at a time.

**Expect Overpass to be the hard one** — see `design.md` §7. Large AOIs time out and need tiling and result merging.

---

### Task 5 — Terrain and Blender export
**Depends on:** 3, 4
**Satisfies:** REQ-11, REQ-13

Prioritised above the live plugin because this is the use case with the clearest payoff, and because most of it is thin wrappers over what tasks 3 and 4 already built.

#### 5.1 — `fetch_elevation_data`
Wraps search + download + clip for the most common request. Carries REQ-4d confirmation exactly as the underlying download does — the wrapper must not become a way to skip the gate.

#### 5.2 — `export_heightmap`
**Test the full REQ-11 contract:** bit depth ≥16; the returned elevation range matches the fixture DEM's true min/max; and a fixture containing nodata normalises from the real range rather than the nodata sentinel. That last assertion is the one that catches the failure which looks fine until it is rendered.

#### 5.3 — `export_georeferenced_texture`
**Test:** file exists, correct dimensions and format, non-blank, georeferencing bounds match the input extent. Per REQ-T4, the cartographic "does this look good" judgement is explicitly flagged as needing a one-off human check rather than assumed covered.

#### 5.4 — `model_sea_level_change`
Thresholding, not DEM mutation. Outputs: land/water mask, polygonised coastline, optional depth raster. `connectivity` option supporting both bathtub (default) and ocean-connected.

**Test:** known DEM + known threshold → hand-computable land/water polygon count and area. **Assert the source DEM's statistics are unchanged after the call** — that is the direct check that the non-mutation rule held. The landlocked basin fixture covers `connectivity` both ways: bathtub floods it, ocean-connected does not. Also assert the REQ-13 warning fires on unknown or mismatched vertical datums.

#### 5.5 — `export_vector_layer_for_mesh`

#### 5.6 — `render_map_to_path`
Grouped here rather than with the other §4.3 visual tools because it is **mode-agnostic** (REQ-1b): Print Layout rendering works headless, and only the canvas variant needs a live session. Putting it in task 6 would have made a headless-capable tool wait on the live plugin for no reason, and would have left batch layout export unavailable in exactly the mode built for batch work.

Shares rendering machinery with 5.3.

**Test:** headless layout render to PNG and to PDF — file exists, correct dimensions and DPI, non-blank. The canvas variant is covered by task 6's smoke tests instead, being the half that genuinely requires a GUI.

---

### Task 6 — Live listener plugin
**Depends on:** 1 (headless path solid first)
**Satisfies:** REQ-1, REQ-1a (live half), REQ-4a, REQ-4b, REQ-4c, REQ-5

#### 6.1 — Plugin skeleton
**Must declare `qgisMaximumVersion` in `metadata.txt`** or QGIS 4 silently refuses to load it. Import through the `qgis.PyQt` shim, never `PyQt5`/`PyQt6` directly.

#### 6.2 — TCP listener with main-thread marshalling
The design's hardest correctness requirement (`design.md` §3.1). Listener reads on a background thread; work is marshalled to the main thread; the reader blocks on the result. Configurable socket timeout.

**A main-thread violation segfaults QGIS rather than raising** — there is no exception to catch, so this cannot be validated by a test that merely passes. Review the threading boundary deliberately.

#### 6.3 — `execute_pyqgis_code` with backup
REQ-4a/4b/4c: pre-execution backup serialising the *in-memory* project, path returned to the caller, retention pruning, and the stated limits in the tool description.

#### 6.4 — Screenshots and view control
`get_screenshot_of_canvas` (REQ-5 byte cap), `get_screenshot_of_window`, `zoom_to_layer`, `set_active_layer`, `toggle_layer_visibility`.

#### 6.5 — Smoke tests
Launch QGIS with the plugin loaded, open the fixture project, run 3–4 representative calls over the socket — one per tool category — and assert the responses. Enough to catch "the listener is broken" without GUI interaction. Runs before a release or merge point rather than on every change.

---

### Task 7 — Documentation tools
**Depends on:** nothing · **Parallel at any point**
**Satisfies:** REQ-6

`search_pyqgis_docs` and `get_pyqgis_class_docs`. Doc index pinned to the local QGIS version.

**Test:** an identifier with no exact match returns the nearest namespace match with siblings, not a bare "not found".

---

### Task 8 — Write operations
**Depends on:** 1–6 stable
**Satisfies:** REQ-4

Deferred deliberately. Includes the `list_project_backups` / `restore_project_backup` pair that REQ-4c defers, which is what makes the task-6 backups actually recoverable rather than merely present.

**Test:** assert every gated call is refused without `confirm=true`.

---

## 3. Sequencing notes

- **Task 4 dominates the schedule.** Four protocol families and the CRS policy. Sub-tasks 4.0–4.3 are the foundation; 4.4 onward is repetitive by design. If time is short, a single adapter plus the CRS policy already produces a working acquisition path.
- **Task 5 is cheap once 3 and 4 exist** — mostly thin wrappers — which is why it precedes the live plugin despite the plugin being more visible.
- **Tasks 2 and 7 are filler work** in the good sense: independent, low-risk, useful when the critical path is blocked.
- **Task 6 is the riskiest per line of code.** The main-thread constraint has no failure mode short of a crash, and no test that passes can prove its absence.
