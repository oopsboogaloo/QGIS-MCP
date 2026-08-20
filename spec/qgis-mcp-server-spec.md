# QGIS MCP Server — Specification
Status: Draft v0.1
Author: Chloe (spec), Claude (drafting assistance)
Modelled on: existing Blender MCP tool surface (`Blender:execute_blender_code`, `get_objects_summary`, `get_screenshot_of_*`, `search_api_docs`, etc.)
---
## 1. Purpose
Provide an MCP server that exposes a running (or headless) QGIS instance to an LLM agent, so that map/layer/analysis work can be driven conversationally and iteratively — the same workflow the Blender MCP already provides for 3D scenes.
Primary use cases (Chloe-specific, informing priority order):
1. **Map commissions with a real-world GIS component.** Client work sometimes needs actual geographic data underneath the illustration (terrain, coastlines, hydrology, place data), not just freehand cartography.
2. **Real-world locations repurposed for fiction.** A recurring commission pattern: take a real city, street layout, or hill range and reuse it — sometimes lightly altered, sometimes wholesale — as the basis for a fictional or alternate-reality setting. Includes scenario work like the *Silver Elite* continent map (a sea-level-raised reimagining of the US), which was done manually last time and would benefit substantially from real elevation/hydrology data and GIS-driven sea-level modelling instead.
3. **Personal GIS art projects**, specifically the "3D terrain rendered over/with old maps" aesthetic — real elevation and hydrology data pulled from QGIS, then exported into Blender for the actual rendering. This makes **QGIS → Blender interoperability a first-class requirement**, not an afterthought: DEM/heightmap export, georeferenced textures, and vector-to-mesh-friendly formats all matter here.
4. Property due-diligence GIS work (flood risk, heritage overlays, constraint layers) — the original driver for learning QGIS, still relevant but secondary to the above now.
5. General PyQGIS learning — being able to ask for something and see it happen in the live app is the whole point.
---
## 2. Two operating modes (mirrors Blender MCP's live vs `_for_cli` split)
| Mode | Mechanism | Use case |
|---|---|---|
| **Interactive** | Companion QGIS plugin runs a small socket/HTTP listener inside a live QGIS session; MCP server forwards calls to it | Chloe has QGIS open, wants to see changes happen live, iterate visually |
| **Headless/CLI** | MCP server launches `qgis_process` or a standalone PyQGIS script (`QgsApplication` with no GUI) against a `.qgz`/`.qgs` project file | Batch jobs, scripted analysis, CI-style repeatable runs, no GUI needed |
This split is directly analogous to `execute_blender_code` (live addon) vs `execute_blender_code_for_cli` (background Blender process).
---
## 3. Architecture
```
┌─────────────┐      MCP (stdio/SSE)      ┌──────────────────┐
│ Claude/agent │ ───────────────────────▶ │  qgis-mcp-server  │
└─────────────┘                            │  (Python process) │
                                            └─────────┬────────┘
                                                       │
                          ┌────────────────────────────┴───────────────────────┐
                          │                                                    │
                 (interactive mode)                                  (headless mode)
                          │                                                    │
                 TCP/HTTP socket                                    subprocess launch
                          │                                                    │
              ┌───────────▼────────────┐                          ┌───────────▼────────────┐
              │  QGIS Plugin (listener) │                          │  qgis_process / PyQGIS  │
              │  running inside live    │                          │  standalone script      │
              │  QGIS application       │                          │  (no GUI, one-shot)      │
              └──────────────────────────┘                          └──────────────────────────┘
```
- **Server language:** Python (matches PyQGIS binding language directly — no cross-language marshalling needed)
- **Transport to Claude:** standard MCP (stdio for desktop, SSE/HTTP for remote)
- **Transport to live QGIS:** local TCP socket opened by a lightweight QGIS plugin (`Plugins → Python Console` equivalent, but a persistent background listener rather than an interactive REPL). This is the same pattern the Blender MCP addon uses inside Blender.
- **Headless execution:** `qgis_process` (QGIS's built-in CLI for running Processing algorithms) plus direct `QgsApplication` initialization for anything `qgis_process` doesn't cover.
---
## 4. Tool surface (draft)
Naming follows the existing Blender MCP convention: `execute_*`, `get_*_summary`, `get_screenshot_*`, `search_*_docs`, `jump_to_*`.
### 4.1 Execution
- **`execute_pyqgis_code`** — run arbitrary PyQGIS code in the live session. Assign to `result` (dict, JSON-serialisable) to return data. Direct analogue of `Blender:execute_blender_code`.
- **`execute_pyqgis_code_for_cli`** — run PyQGIS code headless against a specified `.qgz`/`.qgs` project. Analogue of `execute_blender_code_for_cli`.
- **`run_processing_algorithm`** — invoke a named Processing algorithm (e.g. `native:buffer`, `qgis:zonalstatistics`) with a parameter dict; wraps `qgis_process` for headless or `processing.run()` for live. Worth a dedicated tool rather than forcing raw code, since Processing algorithms are QGIS's primary "batteries included" analysis surface and parameter dicts are easy for an agent to construct from docs.
### 4.2 Project / layer introspection
- **`get_project_summary`** — layer count by type (vector/raster/mesh), CRS, extent, active layer, layout count. Analogue of `get_blendfile_summary_datablocks`.
- **`get_layer_summary`** — for a named layer: geometry type, CRS, feature count, field schema, renderer type, source path, extent. Analogue of `get_object_detail_summary`.
- **`get_layers_tree`** — full layer panel structure incl. groups/nesting. Analogue of `get_objects_summary`.
- **`get_missing_layers`** — broken data source references (moved/renamed files). Analogue of `get_blendfile_summary_missing_files`.
- **`get_project_path_info`** — project file path, save state, CRS, last-modified. Analogue of `get_blendfile_summary_path_info`.
### 4.3 Visual feedback
- **`get_screenshot_of_canvas`** — render the current map canvas view to PNG. Analogue of `get_screenshot_of_area_as_image`.
- **`get_screenshot_of_window`** — full QGIS window screenshot (for layout/print composer work, dialog states). Analogue of `get_screenshot_of_window_as_image`.
- **`render_map_to_path`** — export current canvas or a named Print Layout to PNG/PDF at a given path/DPI. Analogue of `render_viewport_to_path`.
### 4.4 Navigation / view control
- **`zoom_to_layer`** — fit canvas to a named layer's extent. Analogue of `jump_to_view3d_object_by_name`.
- **`set_active_layer`** — set which layer is selected in the Layers panel.
- **`toggle_layer_visibility`** — show/hide a named layer or group.
### 4.5 Documentation lookup
- **`search_pyqgis_docs`** — full-text search over the bundled/cached PyQGIS API reference (mirrors `Blender:search_api_docs`). Given PyQGIS's docs are Sphinx-built and match the C++ class structure closely, this can likely reuse the same doc-indexing approach as the Blender skill almost verbatim.
- **`get_pyqgis_class_docs`** — direct lookup by identifier (e.g. `qgis.core.QgsVectorLayer`), same discovery-pattern (`X.*`) behaviour as `Blender:get_python_api_docs`.
- **`search_processing_algorithms`** — list/search available Processing algorithm IDs and their parameter schemas (`native:buffer`, `gdal:*`, `grass:*`, `saga:*`). No Blender equivalent needed — this is QGIS-specific and important since algorithm parameter names aren't always guessable.
### 4.6 Data acquisition & source discovery
This is arguably as important as any analysis tool — a large share of real GIS friction is finding, licensing, and downloading the right dataset before any actual work can start. Targets known open sources rather than generic web search, since GIS data portals have their own query patterns (bounding box, place name, dataset ID) that generic search handles poorly.
- **`search_data_sources`** — query across a configured set of known providers by place name or bounding box, AND one or more requested data types in a single call (e.g. `["elevation", "buildings", "streets", "geology"]`), returning matching datasets grouped by type, each with resolution/scale, format, licence, provider, and approximate size. Deliberately multi-type rather than one-provider-per-call, so a compound request ("get me terrain, buildings, streets, and geology for this place") produces one grouped result set the agent can present as a single menu, instead of the agent having to guess which providers to query separately. Configured providers for v1:
  - **OS Data Hub** (Ordnance Survey) — UK topography, OS Terrain 50/5, building/street data. Relevant to UK commission and property work.
  - **OpenTopography** — global DEM access (SRTM, Copernicus GLO-30, national LiDAR where available). Primary elevation source for the terrain/Blender pipeline (§4.7).
  - **Natural Earth** — coastlines, admin boundaries, rivers at multiple scales. Good generic base layer, especially as a starting skeleton for fictional/alternate-reality maps.
  - **OpenStreetMap (via Overpass API)** — street layouts, building footprints, place names, points of interest. Directly serves the "lift a real city's street grid for a fictional setting" use case, and covers buildings/streets where OS Data Hub coverage is unavailable or licence terms don't fit.
  - **BGS (British Geological Survey)** — geology data type added as a core provider given how frequently this comes up for Chloe's terrain/cartography work. BGS Geology 625k (national scale) is free under OGL and covers vector bedrock/superficial-deposit polygons — commission-safe, no licence fee. Higher-resolution tiers (250K/50K/25K/10K) exist but carry a per-area commercial licence fee, so `search_data_sources` results for geology should surface the 625k free tier by default and flag the higher-res paid tiers as a distinct option with their licence terms shown up front (see `get_dataset_licence_info`) rather than silently picking the highest resolution available. BGS's GeoIndex also supports direct clip-to-extent, which the download step can mirror to avoid pulling more than needed.
  - Extensible list — new providers added as a config entry (base URL, auth type, query pattern) rather than needing new tool code per source, where the provider's API is reasonably RESTful.
- **`download_and_register_layer`** — download a specific dataset (from a `search_data_sources` result or a direct URL) and add it to the project as a layer in one step. Requires confirmation before executing per the standing file-download permission rule (state source, format, and approximate size before proceeding) — this is the one tool in the whole spec that's genuinely fetching external, un-trusted-until-verified data, so it shouldn't be silently automatic. Designed to be called once per selected dataset in a loop by the agent (see §4.6.1) rather than needing a bespoke "download everything" variant.
- **`get_dataset_licence_info`** — surface the licence/attribution terms for a dataset before or after download (e.g. OS Data Hub's OGL vs commercial tiers, OSM's ODbL attribution requirement, Natural Earth's public domain status). Matters specifically because commission work (§2, use case 1) may have redistribution/attribution obligations that differ per source — this shouldn't be left implicit.
- **`check_api_credentials`** — report which configured providers have valid API keys set up locally and which are missing credentials, rather than failing opaquely mid-download. Some sources (OS Data Hub, OpenTopography) require free registration for an API key — the MCP can't complete that signup step itself (falls under the same "account creation" restriction as any other service), so this tool's job is to clearly surface what's missing and point at the registration page, not attempt to work around it.
#### 4.6.1 UX pattern: discovery menu → automated fetch
This is the intended end-to-end interaction shape, and it's deliberately achieved through orchestration of the primitives above rather than one monolithic "get me everything" tool — keeps each tool testable in isolation (§9) while still giving a one-request, one-menu, one-confirmation experience in practice:
1. **Chloe asks in natural language** for what she wants at a place and rough scale (e.g. "Sudbury height map with buildings and streets" — see worked example below). No need to specify provider names or dataset IDs.
2. **Agent calls `search_data_sources`** once, with the location resolved to a bounding box and the implied data types inferred from the request (elevation + buildings + streets, in the example). Multiple providers may return options for the same type at different resolutions/licences.
3. **Agent presents the grouped results as a menu** — conversationally, not as a literal UI widget — e.g. "For elevation: OS Terrain 5 (5m, OGL) or Copernicus GLO-30 (30m, free); for buildings: OS building outlines or OSM building footprints; for streets: OSM road network." Chloe picks per-category or says "just use the best UK-native option for everything."
4. **Agent calls `check_api_credentials`** implicitly if a chosen provider needs one, surfacing any missing-key blocker before proceeding rather than mid-download.
5. **Agent calls `download_and_register_layer`** once per selected dataset (looping over the picks), each with its own confirmation per REQ-4/the standing download-permission rule — but since these happen in one continuous turn immediately after Chloe's selection, it reads as a single automated step from her side, not a manual multi-step process.
6. **Agent chains into `clip_to_aoi`** to trim everything to the actual area of interest, since provider extents are typically larger than the requested area.
7. From there the terrain/export tools (§4.7) or ordinary analysis tools (§4.1–4.5) pick up as normal — acquisition is a front-end to the rest of the tool surface, not a separate workflow.
No new tool is required for this pattern beyond what's already in §4.6 — it's a question of the agent's calling discipline, which should be documented as guidance in the eventual system prompt / plugin instructions for whatever surfaces this MCP, not enforced mechanically by the server.
#### 4.6.2 Worked example: Sudbury heightmap with buildings and streets
Illustrates the pattern above end-to-end, and doubles as a natural first integration test once `download_and_register_layer` has live coverage (§9.3's data-acquisition row).
> **Chloe:** "Sudbury height map with superimposed buildings and streets."
1. Agent resolves "Sudbury" — ambiguous without more context (Sudbury, Suffolk vs Sudbury, Ontario, vs Sudbury, London) — asks a single clarifying question only if genuinely ambiguous, otherwise defaults to the most contextually likely match (Suffolk, given Chloe's known location and prior property research there) and states the assumption.
2. `search_data_sources(location="Sudbury, Suffolk, UK", types=["elevation", "buildings", "streets"])` → menu: OS Terrain 5 / Copernicus GLO-30 for elevation; OS building outlines / OSM buildings for buildings; OSM road network for streets.
3. Chloe picks OS Terrain 5 (best UK resolution) + OS buildings + OSM streets, or simply says "best available."
4. Agent runs `download_and_register_layer` three times (one per dataset), each confirmed inline, then `clip_to_aoi` to the Sudbury built-up area boundary.
5. Result: three correctly-clipped, correctly-projected layers in the open project — DEM, building footprints, road network — ready either for further QGIS styling or, per Chloe's actual goal, straight into the §4.7 export tools (`export_heightmap` for the terrain, `export_vector_layer_for_mesh` for buildings/streets) for the Blender rendering pipeline.
### 4.7 Terrain, elevation & Blender export
Added specifically for the terrain/old-map rendering pipeline and real-location reuse work (use cases 2 and 3 above). These lean heavily on existing QGIS raster/DEM tooling and Processing algorithms rather than needing custom code.
- **`fetch_elevation_data`** — pull DEM/elevation data for a given extent, built on top of `search_data_sources`/`download_and_register_layer` (§4.6) for the actual source lookup and fetch, then clips to the specified extent. Kept as a separate convenience tool rather than requiring the agent to chain the generic acquisition tools manually every time, since "get me elevation for this area" is by far the most common data-acquisition need across all three use cases in §2.
- **`apply_sea_level_offset`** — raise/lower a DEM by a specified value and regenerate a derived coastline/land-water mask (raster reclassify + polygonize). Directly targets *Silver Elite*-style sea-level scenario work — turns a manual multi-step Photoshop-era process into one parametrised call.
- **`export_heightmap`** — export a DEM (or a derived/modified one) as a grayscale heightmap image (PNG/EXR/TIFF) at a specified resolution, correctly normalised, ready for a Blender displacement/geometry-nodes workflow.
- **`export_georeferenced_texture`** — export the current canvas render (or a specific layer combination — e.g. old-map raster overlay + hillshade + BGS geology polygons styled by rock type) as a texture image, plus its world-space bounding box, so it can be UV-aligned against a heightmap-derived mesh in Blender. Geology data (§4.6) is a natural texture source here — bedrock/superficial-deposit boundaries add real geological character under a rendered terrain without needing to hand-paint it.
- **`export_vector_layer_for_mesh`** — export vector layers (roads, coastline, contours) to a mesh-friendly format (e.g. GeoJSON with elevation-sampled Z values, or OBJ/SVG) for use as Blender curve/mesh input — useful for turning real street layouts or hill contour lines into fictional-map basework.
- **`clip_to_aoi`** — clip any layer (vector or raster) to a named area of interest (bounding box, drawn polygon, or another layer's extent) — the standard first step when lifting a real city/region as a base for fictional reuse.
This is the set that most directly serves the "beautiful 3D terrain + old map" aesthetic and the take-a-real-place-and-fictionalise-it workflow — it's arguably a bigger differentiator for Chloe's actual use than the general-purpose analysis tools in 4.1–4.5, and should be weighted accordingly in build order (see §7).
### 4.8 Explicitly out of scope for v1
- Editing/writing features into a layer's attribute table (data-mutating — treat like the existing "explicit permission required" action category; revisit once read-heavy tools are proven out)
- Publishing to QGIS Server / hosted services
- Plugin installation/management
---
## 5. Requirements (EARS notation)
**REQ-1 (connection):** WHEN a live QGIS session with the listener plugin active is available, the qgis-mcp-server SHALL connect to it over a local TCP socket before falling back to headless mode.
**REQ-2 (headless fallback):** WHEN no live session is available AND a project file path is supplied, the qgis-mcp-server SHALL execute requests via `qgis_process`/standalone `QgsApplication` without requiring the QGIS GUI to be open.
**REQ-3 (data return):** WHEN `execute_pyqgis_code` or `execute_pyqgis_code_for_cli` is called, the server SHALL return only JSON-serialisable data assigned to a `result` variable, mirroring the Blender MCP contract, to keep the calling convention consistent for the agent.
**REQ-4 (destructive actions):** IF a request would delete a layer, overwrite a project file, or modify features, THEN the server SHALL require an explicit `confirm=true` parameter, since QGIS has no native undo across a scripted session the way manual GUI edits do.
**REQ-5 (screenshots):** WHEN `get_screenshot_of_canvas` is called, the server SHALL return a PNG capped at a configurable byte-size limit (default: MCP message size limit), matching the Blender MCP screenshot contract.
**REQ-6 (docs):** WHEN `search_pyqgis_docs` is called with an identifier that has no exact match, THEN the server SHALL return the nearest namespace/partial match with suggested siblings, rather than a bare "not found," matching the Blender `get_python_api_docs` fallback behaviour.
**REQ-7 (algorithm discovery):** WHEN `run_processing_algorithm` is called with an unrecognised algorithm ID, THEN the server SHALL return the list of algorithm IDs matching a fuzzy search on the given name, to reduce failed round-trips from slightly-wrong IDs (e.g. `native:buffer` vs `qgis:buffer`, which differ across QGIS versions).
**REQ-8 (grouped data discovery):** WHEN `search_data_sources` is called with more than one requested data type, THEN the server SHALL return results grouped by type across all matching providers in a single response, rather than requiring one call per type, so the agent can present a single combined menu per the §4.6.1 interaction pattern.
---
## 6. Design notes / risks
- **PyQGIS environment setup is the main friction point historically** — `PYTHONPATH`/`QGIS_PREFIX_PATH` must point at the QGIS install's bundled Python bindings. The server's install docs need a clear per-OS setup script (this has tripped up most PyQGIS standalone-script tutorials found during research).
- **Version drift:** PyQGIS is "nearly identical" to the C++ API and both move with each QGIS release (currently 3.44 LTR / 4.2 latest as of this spec). Pin the doc-index and tested API surface to whichever version Chloe runs locally, and treat cross-version calls as a known limitation rather than silently failing.
- **Threading:** the live listener plugin must not block QGIS's main Qt event loop while executing agent-submitted code, or the GUI will freeze on longer operations. Likely needs to run submitted code in QGIS's own task/event-loop-safe mechanisms rather than a naive blocking socket handler.
- **Processing algorithm dict-building** is probably the highest-leverage tool for Chloe's actual use cases (buffers, zonal stats, flood/heritage overlay intersections) — worth prioritising `run_processing_algorithm` + `search_processing_algorithms` over raw `execute_pyqgis_code` in the build order, even though raw execution is the more "general" capability.
- **API key setup for data sources (§4.6)** is a one-off manual step, not something the MCP can automate — free registration with OS Data Hub and OpenTopography is required before those providers are usable, and account creation stays outside what the server should attempt on Chloe's behalf. The install docs need a short "register here, put the key in this config file" checklist alongside the PYTHONPATH setup steps already needed for PyQGIS itself.
---
## 7. Suggested build order (tasks.md outline)
0. **Create synthetic test fixtures** (`test-data/`) — tiny deterministic DEM, sample vector layer, one small real-world CRS test clip. See §9.2/9.6 — everything downstream depends on this existing first.
1. Headless `execute_pyqgis_code_for_cli` — no plugin needed, proves the PyQGIS environment/path setup works at all. Paired test: run trivial code against fixture, assert structured return.
2. `get_project_summary` / `get_layer_summary` headless — read-only introspection, low risk. Paired test: assert exact fixture properties (feature count, CRS, field schema).
3. `run_processing_algorithm` + `search_processing_algorithms` headless — highest practical value for real analysis tasks, and the foundation the terrain tools (§4.7) and data acquisition tools (§4.6) are built on top of. Paired test: known algorithm + fixture input → hand-calculable expected output.
4. **Data acquisition set (§4.6)** — `search_data_sources`, `get_dataset_licence_info`, `check_api_credentials` first (no external network calls needed for their tests — mock provider responses against fixtures); `download_and_register_layer` last, since it's the one tool that needs real network calls and confirm-gating (REQ-4) properly wired up before it's safe to use unattended.
5. **Terrain/Blender export set (§4.7)** — `fetch_elevation_data`, `clip_to_aoi`, `export_heightmap`, `export_georeferenced_texture` first (these are largely thin wrappers over Processing algorithms + raster export, so low incremental cost once #3 and #4 exist); `apply_sea_level_offset` and `export_vector_layer_for_mesh` next. Pulled forward in priority given this is the use case with the clearest, most distinctive payoff (map commissions, fictional-location reuse, and the old-map/3D-terrain art projects all depend on it). Paired tests per §9.3 table — file existence/format/dimensions/stats assertions.
6. Live listener plugin (`execute_pyqgis_code`, screenshots, view control) — once headless path is solid, add the interactive loop. Covered by the lighter smoke-test pass (§9.4) rather than full unit coverage.
7. Documentation search tools — can be built in parallel any time; no dependency on the above.
8. Destructive/write operations — deferred until the above is stable and confirm-gating (REQ-4) is implemented.
Each numbered task above is expected to produce both the tool and its test in the same unit of work (REQ-T1) — this list is the skeleton for `tasks.md` in the SDD three-file structure, with `requirements.md` covering §5/§9.5 and `design.md` covering §3/§6.
---
## 8. Decisions (resolved)
- **Live listener transport:** TCP socket, matching the Blender MCP approach. Confirmed.
- **`run_processing_algorithm` output handling:** algorithm output layers are auto-added to the project canvas by default (no separate "add to map" step required). Confirmed.
- **QGIS Server:** out of scope. This spec targets QGIS Desktop only — single-user, local project files. QGIS Server (the separate FCGI/WSGI component for publishing WMS/WFS web map services) is a distinct tool set with no current use case here and is not planned for a later version either.
---
## 9. Testing strategy
Goal: every tool in §4 should be verifiable by the agent itself during a Claude Code build session, without Chloe needing to open QGIS and eyeball results. This follows the same three-file SDD approach as the UBS work (requirements → design → tasks), with testability treated as a first-class requirement rather than an afterthought bolted on at the end.
### 9.1 Why this is tractable for QGIS specifically
Unlike Blender (where "does it look right" is often genuinely visual and needs a human), most QGIS operations produce **structured, assertable output**: feature counts, CRS strings, geometry validity, raster statistics (min/max/mean elevation), file existence and format. The agent can check "did this work" programmatically for the large majority of tools without needing to look at a picture. Screenshots remain useful as a supplementary check (Claude can read an image directly), but shouldn't be the primary pass/fail signal.
### 9.2 Test harness
- **Framework:** QGIS ships its own `qgis.testing` module (`start_app()`, `QgsApplication` bootstrapping) built on standard Python `unittest` — this is the same mechanism QGIS's own core test suite uses, so it's a well-trodden path rather than something bespoke.
- **Fixtures:** a small `test-data/` directory of synthetic, deterministic inputs — a tiny DEM (e.g. 50×50 px GeoTIFF with a known elevation pattern), a handful of vector features with known geometry/attributes, one small real-world clip (e.g. a known UK grid square) for CRS/reprojection tests. Synthetic and tiny by design, so tests run in seconds and results are exactly predictable (no "roughly looks right" ambiguity).
- **Isolation:** tests run against the headless path (`execute_pyqgis_code_for_cli` / `qgis_process`) by default, since it requires no GUI and is what CI-style automated runs need. Live-listener-plugin tools get a thinner smoke-test layer (§9.4) rather than full coverage, since spinning up a real GUI session in an automated run is inherently more fragile.
### 9.3 Per-tool test pattern
Each tool ships with a paired test that asserts on structured output, not visual inspection:
| Tool category | Assertion style |
|---|---|
| Introspection (`get_layer_summary`, etc.) | Exact match against known fixture properties (feature count, CRS EPSG code, field names/types) |
| Data acquisition (§4.6) | `search_data_sources`/`get_dataset_licence_info`/`check_api_credentials` tested against mocked provider responses (no live network calls in the automated suite); `download_and_register_layer` covered by a single opt-in integration test against a known small real dataset, run manually/occasionally rather than in the default automated pass, since it depends on external services being up and credentials being present |
| Processing algorithms | Output feature/pixel count, geometry validity (`isGeosValid()`), attribute value spot-checks against hand-calculated expected results on the synthetic fixture |
| Terrain/export tools (§4.7) | Output file exists, correct format/dimensions, raster statistics within expected range (e.g. heightmap min/max matches source DEM min/max after normalisation), georeferencing bounds match input extent |
| `apply_sea_level_offset` | Known synthetic DEM + known offset → deterministic, hand-computable expected land/water polygon count and area |
| Screenshot/render tools | File exists, correct dimensions/format, non-blank (basic pixel-variance check) as an automated floor; Claude can additionally open and visually sanity-check the image directly when running the test session, as a secondary check beyond the automated assertion |
### 9.4 Live-mode smoke tests
For the TCP-listener plugin (§2, interactive mode), full unit coverage isn't practical to automate unattended. Instead: a small smoke-test script that launches QGIS with the plugin loaded, opens the fixture project, runs 3–4 representative calls over the socket (one from each tool category), and asserts the responses — enough to catch "the listener is broken" without needing GUI interaction. This runs less frequently than the headless suite (e.g. before a release/merge point) rather than on every change.
### 9.5 Requirements (EARS)
**REQ-T1:** WHEN a new tool is implemented, THEN a corresponding automated test using the synthetic fixture data SHALL be added in the same task/commit, per the SDD tasks.md breakdown — no tool is considered "done" without its test.
**REQ-T2:** WHEN the test suite is run, THEN it SHALL execute entirely via the headless path and require no manual QGIS GUI interaction, so Claude Code can run and interpret it unattended.
**REQ-T3:** WHEN a test fails, THEN the failure output SHALL include the expected vs actual structured values (not just pass/fail), so the agent can diagnose and fix without needing Chloe to reproduce the issue manually.
**REQ-T4:** WHERE a tool's correctness genuinely cannot be reduced to a structured assertion (e.g. cartographic "does this look good" styling questions), THEN the test SHALL still assert the mechanical basics (file produced, correct dimensions, non-blank), and the remaining visual judgement SHALL be explicitly flagged in the task as needing a one-off human check rather than silently assumed to be covered.
### 9.6 Practical effect on the build order
Testing isn't a separate late-stage phase — it's folded into each step in §7: every tool task includes writing its fixture-based test as part of the same unit of work, so the suite grows alongside the tool surface rather than being retrofitted. The synthetic `test-data/` fixtures are created once, early, as task 0 in §7, since almost everything downstream depends on them.
