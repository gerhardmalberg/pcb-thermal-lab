# KiCad 10 example validation — 2026-11-08

The standalone layered-via fixture includes a KiCad 10 project.
The R1 reference clears both pads, its fabrication
value is hidden, and the pads no longer include the silkscreen layer. Extracted
copper, mask openings, holes, component geometry and materials preserve the original
physical model. KiCad 10.0.8 project loading and front/back
plots passed. Existing mask-bridge and dangling-track DRC findings remain;
there are no silkscreen overlaps.

All **46 unit tests pass**, including the regression proving local KiCad state
is ignored while board/project files and referenced models invalidate inputs.

Fresh CPU (two MPI ranks) and CUDA (one rank) steady solves use identical native
meshes. The maximum nodal temperature difference is **8.64e-7 °C**, below the
0.1 °C criterion. The CPU transient regression passes standard/cached, serial/MPI
and CHOLMOD comparisons. Its largest scalar difference is **1.65e-7**, below 1e-5;
the fixed-watt standard/cached comparison also passes.

```bash
PYTHONPATH=src python3 scripts/verify-backends.py examples/layered-via/case.json
PYTHONPATH=src python3 tests/verify_transient.py
```

The steady CPU/CUDA runs took 22.9 s / 22.8 s end-to-end, respectively.
The combined backend comparison took 47.6 s and the transient regression 55.2 s with
installed images. These are observed workstation timings, excluding builds.
Subsequent complete CPU/CUDA runs reused cached stages; native output sizes and
modification times remained unchanged.
[Compact evidence](validation/kicad10-2026-11-08.json) retains the numerical checks.
Current outputs are in `results/examples/` and `results/validation/fast-audit/`;
affected prior outputs and source snapshots remain under
`results/history/2026-11-08-before-kicad-cuda/`. Historical board-specific evidence
was not rerun or modified.
