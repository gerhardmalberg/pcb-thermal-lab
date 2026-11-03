# Running and reviewing studies

Install the lab's CLI once as described in the [quick start](../README.md#quick-start).
A hardware checkout can live anywhere. Supply its case path explicitly; paths
inside the case resolve relative to that case, not the current working directory.

```bash
pcb-thermal study /path/to/project/case.json \
  --section steady --schedules odd even --time-budget-minutes 120 --detail-check
```

This command selects odd/even steady heating at h = 10, 5, 50, 20 W/m²K, in that
order, with odd then even at each coefficient. Existing automatic results in the
same output parent are verified and included without being rerun. Omit
`--schedules` to select automatic, odd and even. The case must use balancing
sources with the explicit topology described in [case configuration](cases.md#explicit-heat-sources);
fixed-power fixtures use `run` or `sweep` instead.

## Budget and interruption

Steady studies explicitly select CPU, Hypre, two MPI ranks and one thread per
rank. They do not probe NVIDIA or switch backends after solver failure. Podman
or Docker is required for owned-container cleanup. The budget includes historical
artifact checks, any required extraction/meshing, solving, numerical checks and
rendering. Checking historical results starts the clock slightly before the
first new numerical job.

The first case gets at most 20 minutes including initial checks and preparation;
subsequent cases get 12 minutes each. `--first-case-minutes 12` can shorten the
first allowance when preparation is already available; it cannot extend the
20-minute maximum. Every allowance is also limited by the
remaining overall budget minus a two-minute shutdown reserve. An expired case
stops the batch; it is not silently skipped or retried. The runner labels its
containers with a unique batch identifier and removes only those containers on
failure, timeout or interruption. Completed outputs and interrupted logs remain.

Optional `--detail-check` runs one automatic h=10 comparison with a 20 mm detailed
region if all selected cases finish with at least 25 minutes remaining. It gets
at most 20 minutes, including preparation and reporting. This is a limited
10-versus-20 mm check, not the full detail/crop validation campaign.

Use Ctrl-C to stop a foreground batch. To survive terminal closure, start the
same command in a systemd user service with an explicit working directory and
log destination. Stop that specific service with `systemctl --user stop NAME`.
Do not use a system-wide container or process kill command. An interrupted
transient currently restarts from ambient; restart-file continuation is not
implemented.

## Evidence and statuses

The case's output parent contains `study-steady.json`, `study-steady.csv`,
`study-steady.md` and `steady-convection.png`. The checkpoint records the active
case, completed records, newly completed cases, elapsed seconds and optional
comparison status. Status is `complete`, `failed`, `budget_exhausted` or
`interrupted`. Execution status `complete` can coexist with `validation_status: domain_unconverged`
when the limited detail check fails. Reports and plots then explicitly identify
the completed solves as provisional. A failed new case does not remove completed
records. A partial
plot includes only accepted completed cases.

Historical and new results must have intact artifact hashes, unchanged design
inputs, matching geometry/material/source assumptions and identical converted
mesh hashes. Schedule and convection coefficient may differ. Native Elmer must
report successful completion and the steady heat imbalance must be below 1%.
A mismatch stops the batch with an explicit reason; it is not treated as an
equivalent model. Historical tool and implementation identities remain preserved. Saved case
configuration must match its provenance. Manually overridden SIFs remain usable
with `run --sif`, but automated schedule studies reject them because their
physical equivalence cannot be inferred from the case configuration.

Three-resolution mesh checks and detail comparisons require hotspot changes
below **both 1 °C and 2% of peak temperature rise**. Heat balance alone does not
establish domain convergence or hardware accuracy. A transient comparison checks
every shared history time, including warm-up and switching, rather than only the
cooled endpoint.

## Other study capabilities and resources

The separate `mesh`, `detail`, `crop`, `transient` and `all` sections remain
available for additional studies. They use the extended runner;
they do not accept the steady runner's schedule selection or time budget.
They are not scheduled or started by the bounded command above.

One pipeline runs per checkout. The default worker/solver limit is 8 GiB,
with a host reserve of 1.5 GiB and disk reserve of 2 GiB. Each numerical stage
records sampled peak RSS, elapsed time and disk usage in `resources.json`.
Schedule large builds and GUI verification separately on memory-limited hosts.

## Reusable checks

From the lab checkout:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 tests/verify_deadline.py
pcb-thermal run "$PWD/examples/layered-via/case.json" --backend cpu
PYTHONPATH=src python3 tests/verify_interfaces.py
PYTHONPATH=src python3 tests/verify_transient.py
# Optional device/GUI checks, performed separately:
PYTHONPATH=src python3 tests/verify_cuda.py
PYTHONPATH=src python3 tests/verify_gui.py
```

The deadline check uses short-lived sleeping containers, including an independent
control container that must remain running until the test cleans it up. Numerical
and GUI checks create ignored local evidence under `results/validation/`.
GUI verification uses a private authenticated X display and disposable inputs;
optional host dependencies are Xvfb, xauth, xwd, Pillow, xdotool and Openbox.

## Publish a compact snapshot

After the study stops, this verifies each completed run again and copies three
figures plus a CSV/provenance snapshot to an explicit documentation directory:

```bash
python3 scripts/publish-steady.py /path/to/results/CASE /path/to/case-docs/report
```

It excludes incomplete solves, native meshes, fields, restart files and local
absolute paths. A completed solve can still be a provisional prediction: the
snapshot records failed or deferred domain validation separately from execution.

## Compare backends for a steady case

```bash
python3 scripts/verify-backends.py /path/to/project/case.json
```

This runs CPU with two ranks and CUDA with one rank, then checks matching mesh
coordinates and temperature fields. CUDA and its installed image are required.
The CPU output uses the case's output directory; CUDA uses its sibling with a
`-cuda` suffix. Choose fresh outputs when preserving a historical run.
