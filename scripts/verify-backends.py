#!/usr/bin/env python3
"""Compare CPU and CUDA steady solutions for an explicit case on the same mesh."""
import argparse
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from pcb_thermal.common import ROOT,write_json,WorkflowError,serial_workflow
from pcb_thermal.config import load
from pcb_thermal.pipeline import run
from pcb_thermal.runtime import Runtime

p=argparse.ArgumentParser(description=__doc__);p.add_argument('case',type=Path);a=p.parse_args()
source,base,project,board,cpu=load(a.case)
if base['analysis']!='steady':raise WorkflowError('Provide a steady baseline case')
root=cpu.parent;inputs=root/'study-inputs';inputs.mkdir(exist_ok=True)
for backend in ('cpu','cuda'):
    c=copy.deepcopy(base);out=cpu if backend=='cpu' else root/(cpu.name+'-cuda')
    c.update(project_root=str(project),board=str(board),output=str(out),name=out.name)
    path=inputs/('backend-'+backend+'.json');write_json(path,c)
    run(path,backend=backend,mpi_ranks=2 if backend=='cpu' else 1)
check=root/'backend-comparison';check.mkdir(exist_ok=True)
@serial_workflow
def compare():
    Runtime().run('tools',['pvpython','/workflow/tests/compare_fields.py',
        '/work/'+cpu.name,'/work/'+cpu.name+'-cuda','/work/backend-comparison'],output=root,log=check/'compare.log',memory_gib=4)
compare()
