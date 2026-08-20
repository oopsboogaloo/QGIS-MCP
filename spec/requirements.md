# QGIS MCP Server — Requirements
Status: Draft v0.2
Derived from: `qgis-mcp-server-spec.md` §5 and §9.5
Companion documents: `design.md` (architecture and design notes, from §3/§6), `tasks.md` (build order, from §7)

---

## 0. About this document

This is the normative requirements set for the QGIS MCP server. The narrative specification remains the source of context — use cases, tool descriptions, worked examples — while this document carries the testable obligations extracted from it.

**Notation.** Requirements use EARS (Easy Approach to Requirements Syntax):

| Form | Meaning |
|---|---|
| **WHEN** *trigger*, the system SHALL *response* | Event-driven |
| **IF** *condition*, **THEN** the system SHALL *response* | Unwanted-condition / guard |
| **WHERE** *feature is present*, the system SHALL *response* | Optional-feature / scoped |

**Numbering.** Requirements are numbered by the order they were established, not by priority. Lettered suffixes (REQ-4a…4d) subdivide an original requirement that proved too coarse; they are not sub-priorities. Numbers are stable — a superseded requirement is marked superseded rather than renumbered, so that commit messages and task references stay valid.

**A recurring contract.** Four requirements independently converged on the same shape: REQ-6 (documentation lookup), REQ-7 (algorithm IDs), REQ-9 (place names) and REQ-12 (continental CRS choice) all resolve ambiguity by *returning the candidates rather than guessing, and never failing bare*. This is the default shape for any new requirement facing an underdetermined input. An extra round-trip is consistently cheaper than a confidently wrong result the agent cannot detect.

---

## 1. Connection and execution mode

**REQ-1 (connection).** WHEN a live QGIS session with the listener plugin active is available, the qgis-mcp-server SHALL connect to it over a local TCP socket.

**REQ-1a (no silent fallback).** IF a tool that names its execution mode is called AND the mode it names is unavailable, THEN the server SHALL return an explicit error naming the missing mode rather than executing in the other one.

*Scope:* `execute_pyqgis_code`, `execute_pyqgis_code_for_cli`, the two screenshot tools, and all navigation/view-control tools. `render_map_to_path` is deliberately excluded — Print Layout rendering works headless, so it is mode-agnostic under REQ-1b; only its canvas variant requires a live session.

*Rationale:* a live-mode call that silently ran headless would execute against a project file with no visible effect on screen, which is indistinguishable from the tool having failed — the worst possible failure mode for the interactive loop the server exists to provide.

**REQ-1b (mode selection for mode-agnostic tools).** WHERE a tool does not name its execution mode, the server SHALL prefer the live session when one is reachable, fall back to headless against the configured project file when it is not, AND report which mode served the call in the tool's response.

*Rationale:* the reported mode is not cosmetic. For `download_and_register_layer` and `run_processing_algorithm` — which auto-add outputs to the project — it is the only way to know whether the resulting layer landed in the open canvas or in a project file on disk.

**REQ-2 (headless fallback).** WHEN no live session is available AND a project file path is supplied, the server SHALL execute requests via `qgis_process` / standalone `QgsApplication` without requiring the QGIS GUI to be open.

**REQ-3 (data return).** WHEN `execute_pyqgis_code` or `execute_pyqgis_code_for_cli` is called, the server SHALL return only JSON-serialisable data assigned to a `result` variable.

*Rationale:* mirrors the Blender MCP contract, keeping one calling convention across both tool surfaces.

---

## 2. Safety, confirmation and recovery

**REQ-4 (destructive actions).** IF a request would delete a layer, overwrite a project file, or modify features, THEN the server SHALL require an explicit `confirm=true` parameter.

*Rationale:* QGIS has no native undo across a scripted session the way manual GUI edits do.

**REQ-4a (raw execution is exempt, but backed up).** WHERE a tool executes agent-authored code, confirm-gating per REQ-4 SHALL NOT apply. INSTEAD, before executing, the server SHALL write the current project state to a timestamped backup and SHALL return that backup's path in the tool response.

*Rationale:* the server cannot statically determine whether submitted code is destructive, and gating every call would make the interactive loop unusable.

**REQ-4b (backup mechanics).** WHEN taking a REQ-4a backup, the server SHALL serialise the *current in-memory* project (`QgsProject.instance().write()`) rather than copying the project file from disk. Backups SHALL be written to a configurable directory, defaulting to a central per-user cache location rather than the project directory, named by project name plus ISO timestamp, and SHALL be pruned to a configurable retention count (default: 20 most recent per project).

*Rationale:* in live mode the on-disk file may be stale by an arbitrary amount of unsaved work. The central default ensures unsaved and untitled projects are still covered.

**REQ-4c (stated limits of the backup).** The REQ-4a backup covers project structure only — layer references, styling, layouts, project CRS and settings. It does NOT cover underlying data. The server SHALL state this limit in the tool description for both raw-execution tools.

*Residual risk, accepted:* raw code that truncates a data provider, overwrites a GeoTIFF, or deletes a shapefile is unprotected. Restore in v1 is manual (open the backup `.qgz`); a `list_project_backups` / `restore_project_backup` pair is deferred to the write-operations phase.

**REQ-4d (external data fetch).** IF a tool fetches data from an external provider, THEN the server SHALL require explicit confirmation stating source, format, licence tier and approximate size before proceeding.

*Scope:* `download_and_register_layer`, and `fetch_elevation_data` which wraps it. The wrapper is named explicitly because convenience wrapping is exactly how a fetch could otherwise slip through unconfirmed.

*Rationale:* this is a separate gate from REQ-4, not an instance of it. REQ-4 guards against irreversible local change; REQ-4d guards against pulling external, un-verified data of unknown size and unknown licence terms.

---

## 3. Discovery and fallback contracts

*All four requirements in this section share the contract described in §0: return candidates, never guess, never fail bare.*

**REQ-6 (documentation lookup).** WHEN `search_pyqgis_docs` is called with an identifier that has no exact match, THEN the server SHALL return the nearest namespace/partial match with suggested siblings, rather than a bare "not found".

**REQ-7 (algorithm discovery).** WHEN `run_processing_algorithm` is called with an unrecognised algorithm ID, THEN the server SHALL return the list of algorithm IDs matching a fuzzy search on the given name.

*Rationale:* reduces failed round-trips from slightly-wrong IDs — `native:buffer` versus `qgis:buffer` differ across QGIS versions.

**REQ-9 (place resolution and ambiguity).** WHEN `search_data_sources` is called with a `place` string rather than a bounding box or AOI polygon, THEN the server SHALL resolve it via `resolve_place` and proceed automatically IF exactly one candidate is returned above the configured confidence threshold; OTHERWISE it SHALL return an error enumerating the candidates with their types and administrative context, rather than selecting one.

*Rationale:* ambiguity should cost one extra round-trip rather than silently producing data for the wrong continent's Sudbury.

**REQ-10 (degraded provider results).** WHEN `search_data_sources` fans out to provider adapters AND one or more adapters fail, time out, or are skipped for missing credentials, THEN the server SHALL return the results from the adapters that succeeded, together with a per-provider status for those that did not, rather than failing the call.

*Rationale:* a single unreachable provider must never cost the whole menu. A partial menu annotated "BGS: skipped, no API key" is far more useful than an error — and this means a missing key surfaces inline during search rather than requiring a separate pre-flight check.

---

## 4. Data acquisition

**REQ-8 (grouped data discovery).** WHEN `search_data_sources` is called with more than one requested data type, THEN the server SHALL return results grouped by type across all matching providers in a single response, rather than requiring one call per type.

*Rationale:* the agent presents one combined menu per request; one call per type would fragment it.

---

## 5. Spatial correctness

*These three requirements exist because each guards a failure that is **silent** — producing plausible-looking output that is wrong in a way neither the agent nor a casual look at the map will catch.*

**REQ-11 (heightmap output contract).** WHEN `export_heightmap` is called, the server SHALL:
1. write at least 16 bits per channel;
2. exclude nodata from the normalisation range;
3. return alongside the file the source elevation min and max in metres mapped to the output's full range, the ground extent width and height in metres, and the derived ratio `(max_elev − min_elev) / extent_width` — the displacement strength for a plane one Blender unit wide, with aspect taken from the returned extent dimensions;
4. reject or reproject a geographic-CRS DEM rather than emitting a ratio computed from degrees.

*Failure modes guarded:* 8-bit output gives 256 elevation steps — 1.2 m per step across a 300 m range, which reads as stair-stepping on any rendered hillside. Nodata left in normalisation becomes the range minimum, crushing real terrain into a fraction of a percent of the output range. A heightmap image carries no absolute scale, so without the returned metadata the vertical exaggeration is set by eye and is irreproducible between exports.

**REQ-12 (target CRS selection).** WHEN layers are acquired, the server SHALL reproject them on registration to a single projected (metre-based) target CRS derived from the area of interest, SHALL retain the unmodified downloaded original on disk, and SHALL report the chosen CRS and the tier that selected it.

WHEN an export runs, the server SHALL verify its input is already in a projected CRS rather than reprojecting at export time.

IF the AOI spans multiple UTM zones, THEN the server SHALL NOT select a projection automatically, and SHALL instead return the candidate projections — any standard EPSG code covering the AOI, alongside an equal-area or conformal projection centred on the AOI, with the trade-off stated.

*Rationale:* at continental scale the choice between equal-area and conformal is a cartographic decision with a visible effect on the finished map, not a technical default the server is entitled to make. Continental AOIs are rare enough that one question costs little. Verifying rather than reprojecting at export avoids a second resampling of the layer the render is built from.

**REQ-13 (vertical datum is recorded, not transformed).** WHEN a DEM is acquired, the server SHALL record its vertical datum as layer metadata. WHEN `model_sea_level_change` runs against a DEM whose vertical datum is unknown, OR WHEN two DEMs with differing vertical datums are combined, THEN the server SHALL emit a warning naming the datums involved and SHALL proceed rather than blocking.

*Rationale:* sea level modelling operates in single-metre increments, so a datum mismatch of a metre or two is a large fraction of the modelled signal, and stray ellipsoidal heights would be wrong by roughly 50 m in the UK. Implementing geoid transformation is disproportionate to v1; a visible caveat removes the silence, which is the actual danger.

---

## 6. Visual output

**REQ-5 (screenshots).** WHEN `get_screenshot_of_canvas` is called, the server SHALL return a PNG capped at a configurable byte-size limit, defaulting to the MCP message size limit.

---

## 7. Testing

**REQ-T1 (test parity).** WHEN a new tool is implemented, THEN a corresponding automated test using the synthetic fixture data SHALL be added in the same task/commit. No tool is considered done without its test.

**REQ-T2 (unattended execution).** WHEN the test suite is run, THEN it SHALL execute entirely via the headless path and require no manual QGIS GUI interaction.

**REQ-T3 (diagnosable failures).** WHEN a test fails, THEN the failure output SHALL include the expected versus actual structured values, not just pass/fail.

**REQ-T4 (irreducibly visual cases).** WHERE a tool's correctness genuinely cannot be reduced to a structured assertion, THEN the test SHALL still assert the mechanical basics — file produced, correct dimensions, non-blank — and the remaining visual judgement SHALL be explicitly flagged in the task as needing a one-off human check rather than silently assumed covered.

---

## 8. Traceability

| Requirement | Binds | Verified by |
|---|---|---|
| REQ-1, REQ-2 | Server connection logic | Live smoke tests; headless suite runs under REQ-2 by construction |
| REQ-1a | Mode-named tools | Call each with its mode unavailable; assert error names the missing mode |
| REQ-1b | Mode-agnostic tools | Assert response carries the serving mode in both live and headless |
| REQ-3 | Both raw-execution tools | Trivial code against fixture; assert structured return |
| REQ-4 | Destructive typed tools | Assert call without `confirm=true` is refused |
| REQ-4a–4c | Both raw-execution tools | Assert backup exists at returned path pre-execution; assert it captures in-memory state, not the stale file; assert retention pruning |
| REQ-4d | `download_and_register_layer`, `fetch_elevation_data` | Assert unconfirmed call is refused, including via the wrapper |
| REQ-5 | `get_screenshot_of_canvas` | File exists, correct dimensions/format, non-blank, within byte cap |
| REQ-6 | `search_pyqgis_docs` | Unknown identifier returns siblings, not "not found" |
| REQ-7 | `run_processing_algorithm` | Wrong-but-close ID returns fuzzy matches |
| REQ-8 | `search_data_sources` | Multi-type call returns one grouped response |
| REQ-9 | `search_data_sources`, `resolve_place` | Ambiguous name (Sudbury) returns candidates; unambiguous proceeds |
| REQ-10 | Provider dispatcher | Inject failing, timing-out and credential-less adapters; assert menu survives with statuses |
| REQ-11 | `export_heightmap` | Bit depth ≥16; elevation range matches fixture truth; nodata fixture normalises from real range |
| REQ-12 | Acquisition registration, export tools | Tier selection against known-answer AOIs; original byte-identical after reprojection; continental AOI returns candidates |
| REQ-13 | DEM acquisition, `model_sea_level_change` | Assert datum recorded; assert warning on unknown and on mismatch |
| REQ-T1–T4 | The suite itself | Reviewed per task rather than asserted programmatically |

---

## 9. Out of scope for v1

Recorded here so that absence reads as a decision rather than an oversight.

- **Dedicated feature-editing tools.** Mutation should require deliberately dropping to raw code. Note this is not a guarantee of immutability — `execute_pyqgis_code` can edit features and no static check can prevent it (REQ-4a–4c).
- **Vertical datum transformation** between geoid models. Datums are recorded and conflicts warned about per REQ-13, but not converted.
- **Publishing to QGIS Server** or hosted services. Not planned for a later version either.
- **Plugin installation and management.** Note the consequence: SAGA has not been a core Processing provider since QGIS 3.30 and requires a third-party plugin, so `saga:*` algorithms are unavailable unless installed manually.
