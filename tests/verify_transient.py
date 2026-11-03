"""Run after the layered-via example: PYTHONPATH=src python3 tests/verify_transient.py."""
from pathlib import Path
import json,shutil,time,numpy as np
from pcb_thermal.runtime import Runtime
from pcb_thermal.common import ROOT,write_json
from pcb_thermal.solver import generate,surface_area,verify_log
from pcb_thermal.pipeline import copytree
from pcb_thermal.postprocess import report
rt=Runtime();src=ROOT/'results/examples/layered-via';base=ROOT/'results/validation/fast-audit'
if base.exists():base.rename(base.with_name(base.name+'-previous-'+str(time.time_ns())))
base.mkdir(parents=True)
case=json.loads((src/'case.resolved.json').read_text());case.update(analysis='transient',transient={'on_s':10,'off_s':10,'step_s':.5,'field_interval_s':5})
power=json.loads((src/'solve/power.json').read_text());meta=json.loads((src/'mesh/physical-groups.json').read_text());area=surface_area(np.load(src/'mesh/mesh.npz'))
# A changing total load detects frozen cached sources as well as scalar errors.
power.update(schedule='automatic',phase_s=5)
for p in power['resistors'].values():p.update(odd_w=2*p['average_w'],even_w=0.)
rows={}
def execute(label,fast,ranks):
 out=base/label;out.mkdir(exist_ok=True);solve=out/'solve';solve.mkdir(exist_ok=True)
 if not (solve/'mesh').exists():copytree(src/'convert/mesh',solve/'mesh')
 case['execution']['cpu_solver']='cholmod' if label=='cached-cholmod' else 'hypre'
 case['numerics']={'cache_bulk':fast,'fast_audit':fast};write_json(out/'case.resolved.json',case);write_json(solve/'power.json',power)
 generate(case,power,meta,solve,ranks=ranks,surface_area_m2=area)
 rt.run('cpu',['/usr/bin/python3','-m','pcb_thermal.worker','compile','/work/case.resolved.json'],output=out,log=out/'compile.log')
 if ranks>1 and not (solve/'mesh/partitioning.2').exists():rt.run('cpu',['ElmerGrid','2','2','/work/solve/mesh','-metis',str(ranks)],output=out,log=out/'partition.log')
 cmd=['ElmerSolver','case.sif'] if ranks==1 else ['mpirun','-np',str(ranks),'ElmerSolver_mpi','case.sif']
 rt.run('cpu',cmd,output=solve,env={'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1'},log=solve/'solver.log');verify_log(solve/'solver.log');report(out,case)
 return np.loadtxt(solve/('fast-scalars.dat' if fast else 'scalars.dat'))

for label,fast,ranks in [('standard-mpi',False,2),('cached-mpi',True,2),('cached-serial',True,1),('cached-cholmod',True,1)]:
 rows[label]=execute(label,fast,ranks)
a=rows['standard-mpi'];checks={}
for label,row in rows.items():
 err=float(np.max(np.abs(a-row)));checks[label]={'max_absolute_scalar_difference':err,'pass':err<1e-5}
# Fixed watts must not acquire an implicit five-second switching constraint.
case['transient']={'on_s':12,'off_s':6,'step_s':3,'field_interval_s':6}
power=json.loads((src/'solve/power.json').read_text())
fixed=execute('fixed-standard-mpi',False,2)
cached=execute('fixed-cached-mpi',True,2)
error=float(np.max(np.abs(fixed-cached)))
checks['fixed-cached-mpi']={'max_absolute_scalar_difference':error,'pass':error<1e-5}
write_json(base/'checks.json',checks);print(checks)

if not all(v["pass"] for v in checks.values()):raise SystemExit("Transient/serial/MPI comparison failed")
