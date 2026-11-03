# Thermal case configuration

Case files use **JSON only**, parsed with Python's standard library. Use
`.json` filenames, double-quoted keys and strings, and no comments or trailing
commas. Legacy YAML syntax is rejected. Paths are relative to the case file.

`"schema": 1` identifies the case interface. Paths are relative to the case file;
`project_root` encloses the board and mechanical inputs. `output` must be a
dedicated directory. The pipeline owns its stage directories; manual work
belongs in another directory. CLI execution options override case execution.

| Section | Meaning |
|---|---|
| `board`, `project_root`, `output` | Source ownership and generated files |
| `sources.refs` | Unique populated resistor references; independent of modeled board extent |
| `sources.kind` | Required: `balancing` uses a configured resistor chain; `watts` uses explicit per-reference powers |
| `sources.nodes` | Ordered balancing nodes declaring `ref`, `balance_net`, `tap_net`, `expected_value` |
| `sources.schedule` | `automatic` (default), `odd` or `even`; alternate nonadjacent chain channels |
| `geometry.resistor_body_mm`, `terminal_width_mm` | Required package dimensions `[length, width, height]` and width of each end terminal, in mm |
| `geometry.mode` | `hybrid`, `explicit` or `crop` |
| `geometry.detail_margin_mm` | Default 10 mm around resistor package footprints |
| `geometry.tile_mm` | Default 5 mm spatial averaging outside the detailed region |
| `geometry.crop_mm` | `[xmin,ymin,xmax,ymax]` in saved KiCad XY coordinates |
| `geometry.plating_mm`, `solder_mm` | Initial assumptions: 0.025 mm barrel walls and 0.1 mm solder |
| `mesh.fine_mm`, `coarse_mm`, `scale`, `chord_mm` | Gmsh sizing and KiCad polygon chord error |
| `materials` | Conductivity diagonal `[kx,ky,kz]` W/mK, density kg/m³, heat capacity J/kgK |
| `cooling` | Ambient °C and exposed-surface convection W/m²K |
| `analysis` | `steady` or `transient` |
| `transient` | On/off durations, timestep and field-output interval in seconds |
| `execution` | Backend, MPI ranks, threads per rank and GPU count |

Material conductivities, densities and heat capacities are explicit inputs in
the physical case, included in provenance and used for transient energy checks.
They are not manufacturer-certified thermal models. Resistor power is uniformly
distributed through the modeled ceramic body, so its maximum is a body-model
temperature rather than a validated resistive-film temperature.

## Explicit heat sources

For constant loads use `"kind": "watts"`, a nonempty unique `refs` list and a
`watts` mapping containing every selected reference. Powers must be finite and
nonnegative with a positive total. Component references must exist and be populated.
The [layered-via example](../examples/layered-via/case.json) supplies this format.

For balancing, list the chain nodes in electrical order, from one end of the
cell stack to the other. Channel 1 connects nodes 0 and 1; channel 2 connects
nodes 1 and 2, and so on. The order of `refs` and the ordering of a footprint's
pads do not determine channel order. For example, a two-channel chain uses:

```json
{
  "sources": {
    "kind": "balancing",
    "refs": ["R1", "R2", "R3"],
    "nodes": [
      {"ref": "R1", "balance_net": "SW0", "tap_net": "TAP0", "expected_value": "10R"},
      {"ref": "R2", "balance_net": "SW1", "tap_net": "TAP1", "expected_value": "10R"},
      {"ref": "R3", "balance_net": "SW2", "tap_net": "TAP2", "expected_value": "10R"}
    ],
    "cell_voltage_v": 3.0,
    "resistance_ohm": 10.0,
    "switch_resistance_ohm": 0.5,
    "phase_s": 2.0,
    "schedule": "automatic"
  }
}
```

At least two nodes are required, and their references must cover `refs` exactly
once. All balance and tap net names must be unique and disjoint. Each selected
resistor must have exactly the two declared pad nets and a value string matching
`expected_value`. This is a literal saved-value assertion; electrical resistance
comes from `resistance_ohm`, not parsing the value string. Ensure these declarations
agree with the circuit and record their provenance in the case assumptions.

All four numerical parameters above must be supplied explicitly. Cell voltage,
resistance and phase duration must be finite and positive; switch resistance
may be zero for an ideal switch. The model assumes identical cells and equal
resistors. One active channel carries `V / (2R + Rswitch)`, and each resistor
in its path dissipates `I²R`. Only those external resistor losses heat the FEM;
switch losses are reported separately.

`odd` and `even` hold their respective channel sets active. `automatic` alternates
them every `phase_s` in a transient and applies their equal-time average in a
steady solve. With an odd number of channels, the phases have different total
loads; both resistor and switch-loss accounting use the actual active count.
A single-channel chain has an unpowered even phase.

Earlier schema-1 balancing cases must add this explicit topology and electrical
parameters. Every source kind now requires `kind`, `geometry.resistor_body_mm`
and `geometry.terminal_width_mm`. All package dimensions must be positive, with
two terminal widths smaller than the body length. Existing resolved results and
provenance are historical records: leave them unchanged and choose a fresh output
parent when migrating a case. The study runner deliberately rejects mixing
historical and new source configurations.

## Geometry and heat accounting

The hybrid approximation mixes local copper fractions in parallel in-plane,
combines layer resistance in series through thickness and includes plated-via
area and volume-weighted heat capacity. It does not resolve all remote copper
connectivity. Expand the detailed region and compare results before relying on
the approximation. All copper nets are retained in the explicit region.

The planar material arrangement is snapped at 0.1 µm to remove numerical
zero-width overlay slivers. Gmsh triangulates the arrangement; shared nodes are
swept through the exact saved layer heights into linear prisms. Holes remain
void and plated barrels remain hollow. Material interfaces share node indices.
The final mesh is in metres; Elmer performs no additional scale conversion.
Saved soldermask openings come from KiCad's mask-layer polygon conversion,
including saved margins, tented vias and mask graphics. They are distinct from
the copper pad polygons used for solder contacts. The package template currently
supports front-side resistor bodies and through plated barrels; backside sources
and blind/buried vias are rejected explicitly.
`thermal-surface.stl` represents the actual meshed surface. The original
`board.step` preserves KiCad geometry for CAD inspection.

The optional `mesh.merge_identical_dielectric_plies: true` removes internal
mesh planes only between adjacent dielectric subplies with the same saved
layer name. They already use the same assumed FR4 material. Copper, mask and
distinct dielectric-layer boundaries stay explicit; the extracted stackup is
preserved verbatim. The default is false. This is numerical coarsening and must
be compared with the full-ply mesh before acceptance. `physical-groups.json`
records every removed plane and the resulting through-thickness levels.

Only real exposed surfaces receive convection. Artificial crop cuts are
insulated. A crop must enclose each selected source and must not cut a drilled
hole. Rejecting an invalid crop prevents silently discarding a heat path.

Elmer's `Heat Source` is power per unit mass. Each heater receives
`watts / (density × meshed volume)`; source normalization and heat conservation
are checked independently. Solver temperatures use kelvin; reports and images
use Celsius. The native scalar columns remain alongside derived CSV.
`ThermalScalars` compiles Elmer's native SaveScalars with a narrow correction:
an empty MPI partition contributes a neutral value to minimum/maximum reduction.
Otherwise a resistor entirely on one rank can incorrectly report a 0 K minimum.
The full patched upstream source, its license header and source hashes are
retained under `solve/native/`; all other native scalar behavior is preserved.

Transient timesteps must divide the on/off durations and field interval, and
for balancing sources also the configured phase duration. The load uses the
phase immediately before each step endpoint, so the heat input lasts exactly
`transient.on_s`. Fixed-watt sources have no implicit phase-duration constraint.
Cell depletion, diagnostic pauses and thermal shutdown feedback are not modeled.

For local debugging only, `PCB_THERMAL_ENGINE=native` uses installed host tools
and the Elmer build under `.cache/native/elmer`. Native versions are identified
separately from container versions. Reproducible deployment uses the pinned
container images and exported inventories.

## Mechanical STEP contacts

Separate mechanical inputs are optional.
The current conformal mesher supports a single straight-sided vertical prism
per STEP, with translation and rotation around Z. Its flat contact footprint
must fit entirely on uninterrupted top soldermask, outside resistor bodies and
pad openings. Curved parts, sloped walls, overlapping parts and unsupported
contact locations fail explicitly. A general curved heatsink/enclosure requires
an extension to the geometry/contact mesher; it is not approximated silently.

```json
{
  "mechanicals": [
    {
      "name": "cooling_block",
      "step": "mechanics/block.step",
      "material": "aluminium",
      "translate_mm": [
        0,
        0,
        0.135
      ],
      "rotate_z_deg": 0,
      "contact": {
        "kind": "layer",
        "thickness_mm": 0.1,
        "material": "thermal_pad"
      }
    }
  ]
}
```

The STEP path is relative to `project_root`. Define `aluminium` and
`thermal_pad` in `materials` with editable conductivity, density and heat
capacity values before using this example.

A `perfect` contact has no additional layer. A `layer` contact models finite
thermal resistance and heat capacity through its actual thickness and material,
with shared interface nodes. The example assumes top soldermask at z=0.035 mm
and a STEP starting at its own z=0. Use the extracted stack heights to choose
the placement for your board; these example coordinates are not defaults.
Original KiCad STEP and mechanical inputs remain separate. Canonical exports
`pcb-thermal-coordinates.step`, `copper-thermal-coordinates.step` and
`resistors-thermal-coordinates.step` use saved KiCad XY and front-copper z=0.
`step-audit.json` records the transform, outline/thickness checks and any
packages whose named CAD centre could not be verified.

## Long transient runs

For fixed materials, mesh, timestep and convection, `ScheduledHeat` reuses
Elmer HeatSolver's bulk stiffness/mass matrices and updates the integrated
odd/even source vectors at every phase boundary and cooldown. `FastAudit`
caches exact linear-element quadrature weights for scalar histories. Native
SaveScalars independently checks the final row. These are lab-supplied Elmer
modules compiled against the selected solver; source and hashes are retained.
Set `numerics.cache_bulk: false` and `numerics.fast_audit: false` to execute the
standard Elmer reference path. Edited nonlinear/moving-mesh SIFs must disable
these optimizations. The layered-via verification compares both paths.

Fields remain native VTU/PVTU with a PVD time collection. A watcher losslessly
compresses completed VTU files and verifies all field, geometry and connectivity
arrays before atomic replacement; hashes and byte counts are recorded in
`field-compression.json`. Scalars remain available every timestep. Solver
runtime and sampled peak process-tree memory appear in `resources.json`.

`outputs.line_mm` optionally supplies two `[x,y,z]` endpoints for native
SaveLine output; coordinates use the thermal frame in millimetres and are
converted once. `outputs.line_divisions` defaults to 200. `outputs.render`
defaults to true. The rendered report also includes sampled PCB temperatures
beneath footprint centres and pads in `nearby-board-temperatures.csv`; open
drills or locations outside the modeled domain remain invalid/blank. These
samples refer to PCB material at the selected field time, not component or
junction temperatures.

An explicit `--sif` is captured before replacing the solve stage, including
when it comes from that run's previous GUI folder. The GUI's local mesh path
is normalized to the scripted `mesh/` directory; the supplied text is retained
as `case.override.sif`. Keep native scalar/output definitions if derived
reports are required. Changed physical assumptions must also be reflected in
the case file so heat accounting remains meaningful. Failed stage directories
are retained under `failed-attempts/` for diagnosis.

## CPU solver and memory budget

`execution.cpu_solver: hypre` is the default MPI-capable iterative solver.
`execution.cpu_solver: cholmod` selects a single-rank sparse Cholesky solver;
set `execution.mpi_ranks: 1`. It can reuse its factorization for the fixed-step
linear transient, but its memory requirement depends strongly on mesh fill-in.
Use `execution.max_memory_gib` to stop the solver if sampled process-tree RSS
exceeds your budget. The measurement interval is 0.5 s, so a short allocation
can exceed the threshold before termination. Limits and termination status are
recorded in `solve/resources.json`; failed solves are never switched to another
backend. The GUI settings and native SIF reflect the selected solver.

### Resource limits

`execution.max_memory_gib` defaults to 8 GiB. Podman/Docker apply this to solver
and numerical-worker containers with no extra container swap; the process
monitor also retains 1.5 GiB of host available RAM and 2 GiB of disk space.
A pressure stop is a failed stage with retained logs, not a valid thermal result.
Only one pipeline runs per lab checkout. Schedule large native diagnostics and
GUI reopening separately on small-memory machines. Refined domains may need
more memory; inspect each run's resource measurements.

## JSON migration verification, 2026-11-01

The layered-via example case definition was renamed to
`.json` without changing its bytes or physical values. Generated sweep and
study inputs also use JSON. Case parsing rejects YAML syntax and works without
site packages; all 33 unit tests pass. The standalone CPU example completes
extraction, meshing, conversion, solving, reporting and headless rendering.
The CUDA comparison on the same mesh has a maximum nodal temperature difference
of 1.46e-6 °C, below the 0.1 °C backend-agreement criterion. Backend agreement
on this fixture does not establish physical-model accuracy for other boards.

The editable installation was refreshed offline using the existing environment.
Installed container images were reused; a full source rebuild was not repeated.
NVIDIA tooling accepts a temporary JSON device specification, and bootstrap
scripts now request JSON. Existing host GPU configuration, historical result
records and downloaded third-party source caches were left unchanged.

## Explicit-source verification, 2026-11-03

All 45 unit tests pass, including independent nodal-circuit comparisons for
multiple chain lengths and electrical values, explicit topology validation,
package dimensions, phase timing and fixed-watt loads. The tests and the
layered-via CPU pipeline ran from an isolated copy containing only maintained
source files, with no external hardware checkout or prior stage artifacts.

The steady fixture completes extraction, meshing, conversion, solving and
rendering. Its relative heat-balance error is 6.95e-11 for the 0.1 W load.
The alternating-load transient regression compares standard MPI Elmer with
cached MPI, serial and CHOLMOD paths; the largest absolute scalar difference
is 1.59e-7, below the existing 1e-5 criterion. Fixed-watt standard/cached MPI
runs also agree (8.16e-11 maximum difference) with a three-second timestep
that does not divide five seconds. Each run passes the native heat accounting.

The transient verifier creates its own output parent on a fresh checkout.
Local logs and check records are under `results/validation/decoupling-2026-11-03/`.
Existing images were reused; no source rebuild, new CUDA or GUI verification
was performed. These checks establish tool behavior on the synthetic fixture,
not physical accuracy for other boards.
