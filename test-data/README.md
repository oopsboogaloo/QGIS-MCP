# Test fixtures

Small, deterministic, exactly-predictable inputs for the test suite (Task 0).

## Regenerating

```sh
python test-data/make_fixtures.py
```

Outputs land in `test-data/generated/`, which is **not** tracked — the
generator is the artefact, not its output. The script needs GDAL, numpy and
PyQGIS, and re-executes itself under the QGIS-bundled interpreter when it finds
it is running somewhere without them, so it works from the server's virtualenv
as well as from QGIS's own Python.

## Files

| File | Contents |
|---|---|
| `dem_su4080_27700.tif` | 50 × 50 DEM, EPSG:27700, 20 m cells. The main terrain fixture |
| `dem_wgs84_geographic.tif` | 20 × 20 DEM, EPSG:4326. Geographic-CRS cases only |
| `vectors.gpkg` | Three layers: `survey_points`, `aoi_boundary`, `tracks` |
| `fixture_project.qgz` | Project referencing the above, CRS EPSG:27700, relative paths |

A `.aux.xml` sidecar may appear next to a `.tif` — GDAL writes it when it
computes band statistics. It is regenerable and carries nothing tests rely on.

## Where the expected values live

[`truth.py`](truth.py) — and nowhere else. It is the single source the suite
imports, and every value in it is **hand-computed from the declared pattern**,
not measured from a generated file.

That separation is the point. Task 0's acceptance condition is that tests
"assert against recorded truth rather than against whatever the file happens to
contain", which rules out the obvious shortcut of having the generator measure
its own output into a manifest: a generator bug would then be recorded as the
expectation and verified against itself. So the flow is one-directional —

```
truth.py  ──declares──▶  make_fixtures.py  ──writes──▶  generated/
    │                            │
    └──────asserts against───────┘   (verification step, at build time)
    │
    └──────asserts against───────▶  tests/   (at test time)
```

— and `make_fixtures.py` fails rather than writing fixtures that disagree with
the declaration. This already earned its keep: the first run caught an error in
the hand-computed elevation ringing the landlocked basin (the ring is
8-connected, so the diagonal corner is the lowest cell in it, not the
edge-adjacent one).

The tests re-check the same properties at test time, which catches a different
failure: fixtures left on disk from an older `truth.py`. The generator cannot
detect that, because it is not running.

## The primary DEM

EPSG:27700, over the real Ordnance Survey 1 km grid square **SU4080** —
eastings 440000–441000, northings 180000–181000. The coordinates are real so
that CRS tests have a real-world answer (REQ-12 tier 1 resolves a UK area of
interest to EPSG:27700) without needing a download. The elevation *values* are
synthetic.

Row 0 is the northernmost row, following the north-up geotransform.

```
base[row, col] = 100.0 + 2.0 * row + 1.0 * col
```

then, in order:

| Region | Rows | Cols | Value | Cells |
|---|---|---|---|---|
| hill | 10–14 | 10–14 | base + 200.0 | 25 |
| coastal inlet | 20–29 | 0–4 | −3.0 | 50 |
| landlocked basin | 30–34 | 30–34 | −5.0 | 25 |
| nodata | 0–2 | 47–49 | −9999.0 | 9 |

The regions do not overlap, which is what makes every count a plain
multiplication.

| Property | Value |
|---|---|
| Valid cells | 2491 of 2500 |
| Valid elevation range | **−5.0 m to 342.0 m** |
| Nodata sentinel | −9999.0 |
| Displacement ratio (REQ-11) | 0.347 |
| Cells below sea level | 75 |
| Lowest land ringing the basin | 187.0 m (row 29 / col 29, the NW diagonal) |

### Why the nodata cells are required, not incidental

They are what catches REQ-11's normalisation trap. The sentinel (−9999) is
lower than the true minimum (−5), so an implementation that normalises without
masking spans −9999 to 342 instead of −5 to 342 — compressing all real terrain
into about **3.4%** of the output range. That reads as a flat plane when
rendered, and is invisible in the file's metadata. A clean synthetic DEM lets
the bug through undetected.

### Why both a basin and a coast are required

The landlocked basin is the only way to test `model_sea_level_change`'s
`connectivity` option in both directions. The coastal inlet is its necessary
counterpart: with nowhere the sea actually reaches, both modes would agree on
every input and the test would pass while proving nothing.

At a 0.0 m threshold:

| Mode | Flooded cells | Flooded area |
|---|---|---|
| `bathtub` (default) | 75 | 30 000 m² |
| `ocean-connected` | 50 | 20 000 m² |

The difference is **exactly the 25-cell basin**. A test asserting that catches
an implementation that ignores `connectivity` outright.

The basin is ringed by land of at least 187 m, so nothing marginal decides the
outcome — and the enclosure is verified rather than assumed.

No vertical datum is declared on this DEM, which also makes it the fixture for
REQ-13's "unknown datum" warning path.

## The geographic DEM

EPSG:4326, bounds −1.2700 to −1.2550 longitude, 51.1700 to 51.1800 latitude
(over land in Hampshire, England). Two narrow purposes: REQ-11 part 4, where a
geographic-CRS DEM must be rejected or reprojected rather than turned into a
ratio computed from degrees; and REQ-12 tier 1, where a UK area resolves to
EPSG:27700.

Its bounds are **declared** in 4326, not transformed from the 27700 extent.
Transforming would put computed lat/lon into a file meant to hold
hand-checkable constants, and would invite tests to assume the two DEMs are
co-registered. **They are not** — nothing should rely on them overlapping.

## Vector layers

All EPSG:27700, inside the DEM extent.

### `survey_points` — 5 points

Fields `id` (Integer), `name` (String), `elevation_m` (Real).

| name | easting | northing | elevation_m |
|---|---|---|---|
| Alpha | 440100 | 180100 | 120.5 |
| Bravo | 440300 | 180500 | 245.0 |
| Charlie | 440500 | 180500 | 318.25 |
| Delta | 440700 | 180200 | 95.75 |
| Echo | 440900 | 180900 | 160.0 |

`elevation_m` is independent attribute data and is **deliberately not** sampled
from the DEM. Tying the two together would couple two fixtures and let an error
in one look like truth in the other.

### `aoi_boundary` — 1 polygon

Fields `id` (Integer), `name` (String). A rectangle from (440200, 180300) to
(440600, 180600): 400 m × 300 m, so **area exactly 120 000 m²** and perimeter
1400 m.

Its edges fall exactly on 20 m cell boundaries, covering 20 × 15 = 300 whole
DEM cells with no partial cells to round. That is deliberate — a fixture whose
clip result depends on the clipper's rounding rule cannot test the clipper.

Clipping `survey_points` to it leaves **2 of 5**: Bravo and Charlie.

### `tracks` — 2 linestrings

Fields `id` (Integer), `name` (String), `surface` (String). Lengths are exact
by construction:

| name | geometry | length |
|---|---|---|
| track_a | (440050, 180050) → (440550, 180050) | 500 m |
| track_b | (440600, 180800) → (440900, 180800) → (440900, 180400) | 700 m |

## The project

`fixture_project.qgz`, titled "QGIS MCP synthetic fixtures (SU4080)", project
CRS EPSG:27700, with four layers and no print layouts.

The layer tree is deliberately not flat:

```
dem_su4080            (layer, visible)
vectors               (group,  visible)
  ├─ survey_points    (layer, visible)
  ├─ aoi_boundary     (layer, visible)
  └─ tracks           (layer, HIDDEN)
```

The group and the one unchecked layer both exist so that `get_layers_tree` has
something to report. In a flat, all-visible project, a tool that walks the
layer tree and one that simply lists `mapLayers()` in arbitrary order would
agree on every assertion — so neither nesting nor visibility would be tested.
Order is set explicitly by the generator rather than left to `addMapLayer`'s
insert-at-top default, which makes it a declared property rather than an
incidental one.

Layer paths are stored **relative** to the project, so `generated/` stays
portable. A project holding absolute paths would resolve only on the machine
that built it, which would make REQ-2's "supply a project file path" contract
untestable anywhere else.

Relative storage also buys a test for free: copying `fixture_project.qgz` alone
into an empty directory breaks all four layer references at once, which is a
real moved-project failure rather than a hand-corrupted file. That is how
`get_missing_layers` is tested, with no extra fixture.

### One field the two readers disagree about

GeoPackage layers carry an `fid` primary key. OGR treats it as the feature id
and leaves it out of the field list; `QgsVectorLayer.fields()` includes it as an
ordinary `Integer64` field. Neither is wrong, so `truth.py` records both views —
`SURVEY_POINTS_FIELDS` for the OGR-level schema, `pyqgis_fields()` for what QGIS
sees. A test that assumed they agreed would fail for a reason that looks like a
fixture fault.

## Still to come

Task 0 lists "one small real-world clip (e.g. a known UK grid square) for
CRS/reprojection tests". The synthetic fixtures sit on real SU4080 coordinates,
which covers the CRS *selection* tests (tier 1 and tier 2 are deterministic and
need no real data). A genuinely downloaded clip is deferred to task 4.4, where
the first provider adapter lands and a recorded real response is committed as a
fixture anyway — that is the point at which real data starts paying for itself
rather than being a second thing to keep in step.
