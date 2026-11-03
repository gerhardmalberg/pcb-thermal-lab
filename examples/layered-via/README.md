# Layered board and plated-via verification fixture

This synthetic 20 × 15 mm two-layer PCB has one 0.1 W resistor, finite copper,
soldermask, solder and a hollow plated through via. It is an Apache-2.0 lab
fixture, independent of external hardware checkouts. It exercises the same production stages:

```bash
./pcb-thermal run examples/layered-via/case.json --backend cpu
PYTHONPATH=src /usr/bin/python3 tests/verify_transient.py
PYTHONPATH=src /usr/bin/python3 tests/verify_interfaces.py
# Optional GUI reopening check; also needs xdotool on the host:
PYTHONPATH=src /usr/bin/python3 tests/verify_gui.py
```

The second command compares standard Elmer with the cached transient modules,
including serial/MPI agreement, warm-up/cooldown energy balance, and the final
cached scalar row against native SaveScalars. It also compares standard/cached
fixed-watt runs with a three-second timestep. Results are generated under
`results/examples/layered-via/` and `results/validation/fast-audit/`.
The analytical slab/cooling references run with `pcb-thermal doctor --full`.
