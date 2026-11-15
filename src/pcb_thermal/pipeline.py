"""Stage execution with content-addressed reuse, immutable sources and provenance."""
import json
import os
import shutil
import time
from pathlib import Path
from .common import ROOT,CACHE,WorkflowError,digest,fingerprint,write_json,serial_workflow
from .config import load
from .runtime import Runtime

STAGES=('extract','mesh','convert','solve','postprocess')
DEPENDENCIES={
    'extract':('extract.py','step.py'),
    'mesh':('geometry.py','mechanical.py','power.py'),
    'convert':(),
    'solve':('solver.py','power.py','measure.py','native.py','ScheduledHeat.F90','FastAudit.F90','compress-fields.py'),
    'postprocess':('postprocess.py','render.py'),
}

def valid_cache(folder,key):
    try:
        d=json.loads((folder/'stage.json').read_text())
        return d['key']==key and bool(d['artifacts']) and all((folder/p).is_file() and digest(folder/p)==h for p,h in d['artifacts'].items())
    except (OSError,ValueError,KeyError):return False

def complete(folder,key):
    artifacts={str(p.relative_to(folder)):digest(p) for p in folder.rglob('*') if p.is_file() and p.name not in ('stage.json','stage.log')}
    if not artifacts:raise WorkflowError('Stage completed without artifacts')
    write_json(folder/'stage.json',{'key':key,'artifacts':artifacts,'completed_unix':time.time()})

def copytree(source,dest):
    # Hard-link immutable input meshes inside a run to avoid unnecessary copies.
    def link(a,b):
        try:os.link(a,b)
        except OSError:shutil.copy2(a,b)
    shutil.copytree(source,dest,copy_function=link,ignore=shutil.ignore_patterns('stage.json','stage.log','partitioning.*'))

def source_inputs(case,project,board):
    """Hash design inputs and referenced assets, excluding local editor state."""
    extensions={'.kicad_pcb','.kicad_pro','.kicad_sch','.kicad_mod','.kicad_sym','.kicad_wks'}
    files={p for p in board.parent.glob('*.kicad_*') if p.is_file() and p.suffix in extensions and not p.name.startswith(('_autosave-', '~', '.'))}
    files.add(board)
    source={str(p.relative_to(project)):digest(p) for p in sorted(files)}
    # Model paths are resolved again during extraction; hash all local referenced assets.
    import re
    for name in re.findall(r'\(model\s+"([^"]+)"',board.read_text()):
        p=Path(name.replace('${KIPRJMOD}',str(board.parent)))
        if not p.is_absolute():p=board.parent/p
        if p.is_file() and p.resolve().is_relative_to(project):source[str(p.resolve().relative_to(project))]=digest(p)
    for item in case.get('mechanicals',[]):
        p=(project/item['step']).resolve()
        if not p.is_relative_to(project) or not p.is_file():raise WorkflowError('Mechanical STEP must exist inside project_root')
        source[str(p.relative_to(project))]=digest(p)
    return source

@serial_workflow
def run(path,stage='all',force=False,backend=None,mpi_ranks=None,threads_per_rank=None,gpu_count=None,sif=None,runtime=None):
    # Capture an explicitly edited SIF before replacing any owned output stage.
    # This also supports a SIF selected from the previous run's GUI directory.
    override_text=Path(sif).read_text() if sif else None
    override_hash=digest(sif) if sif else None
    path,case,project,board,out=load(path,dict(backend=backend,mpi_ranks=mpi_ranks,threads_per_rank=threads_per_rank,gpu_count=gpu_count))
    # A saved run must remain interpretable independently of the input case's
    # working directory, including when later imported into a steady study.
    case.update(project_root=str(project), board=str(board), output=str(out))
    rt=runtime or Runtime();out.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(out).free<2*1024**3:raise WorkflowError('Less than 2 GiB free; free space before starting a stage')
    owned=out/'.pcb-thermal-output'
    if not owned.exists():
        if any((out/s).exists() for s in STAGES):raise WorkflowError('Output has unowned stage directories; choose a new output')
        owned.write_text('PCB Thermal Lab generated output\n')
    source=source_inputs(case,project,board)
    implementation={str(p.relative_to(ROOT)):digest(p) for d in ('src','scripts') for p in (ROOT/d).rglob('*') if p.is_file() and p.suffix in ('.py','.F90','.sh')}
    tool_id=rt.image_id('tools');provenance={'status':'running','started_unix':time.time(),'source':source,'case':case,'implementation':implementation,'tools':tool_id,'engine':rt.engine}
    write_json(out/'case.resolved.json',case);write_json(out/'provenance.json',provenance)
    selected=None;effective_ranks=case['execution']['mpi_ranks'];keys={}
    required=STAGES if stage=='all' else STAGES[:STAGES.index(stage)+1]
    try:
        for name in required:
            folder=out/name
            if name=='extract':parameters={'source':source,'chord':case['mesh']['chord_mm'],'refs':case['sources']['refs']}
            elif name=='mesh':parameters={'extract':keys['extract'],'geometry':case['geometry'],'mesh':case['mesh'],'materials':case['materials'],'refs':case['sources']['refs'],'mechanicals':case.get('mechanicals',[])}
            elif name=='convert':parameters={'mesh':keys['mesh'],'converter':rt.image_id('cpu')}
            else:
                if selected is None:
                    selected,reason=rt.select(case['execution']['backend'],case['execution']['gpu_count']);print(reason,flush=True)
                    if selected=='cuda':
                        if mpi_ranks is None:effective_ranks=1
                        if effective_ranks>case['execution']['gpu_count']:raise WorkflowError('CUDA ranks must not exceed gpu_count')
                    provenance.update(backend=selected,backend_reason=reason,solver=rt.image_id(selected),mpi_ranks=effective_ranks)
                parameters={'convert':keys['convert'],'case':{k:v for k,v in case.items() if k not in ('output','name','project_root','board')},'backend':selected,'solver':rt.image_id(selected),'ranks':effective_ranks,'sif':override_hash}
                if name=='postprocess':parameters.update(solve=keys['solve'],name=case.get('name'))
            dependencies=('common.py','config.py','runtime.py','pipeline.py','worker.py')+DEPENDENCIES[name]
            stage_code={p:h for p,h in implementation.items() if Path(p).name in dependencies}
            key=fingerprint({'parameters':parameters,'tools':tool_id,'implementation':stage_code,'stage':name});keys[name]=key
            if not force and valid_cache(folder,key):print(name+': cached',flush=True);continue
            # Geometry is independent of balancing phase and cooling. Reuse identical
            # stage products across the twelve study cases without rebuilding meshes.
            reusable=CACHE/'stages'/name/key
            if name in ('extract','mesh','convert') and not force and valid_cache(reusable,key):
                if folder.exists():shutil.rmtree(folder)
                copytree(reusable,folder);complete(folder,key);print(name+': shared cache',flush=True);continue
            if folder.exists():
                if not (folder/'stage.json').exists():
                    retained=out/'failed-attempts'/f'{name}-{time.time_ns()}'
                    retained.parent.mkdir(exist_ok=True);folder.rename(retained)
                else:shutil.rmtree(folder)
            folder.mkdir();print(name+': running',flush=True)
            command=['/usr/bin/python3','-m','pcb_thermal.worker',name,'/work/case.resolved.json']
            env={'OMP_NUM_THREADS':str(case['execution']['threads_per_rank']),'OPENBLAS_NUM_THREADS':'1'}
            if name in ('extract','mesh','postprocess'):
                if name=='extract':command+=['--board','/project/'+str(board.relative_to(project))]
                if name=='mesh':command+=['--board','/project/.project-root']
                guarded=['/usr/bin/python3','-m','pcb_thermal.measure',f'/work/{name}/resources.json','--max-rss-gib',str(case['execution']['max_memory_gib']),'--']+command
                rt.run('tools',guarded,project=project,output=out,env=env,log=folder/'stage.log',memory_gib=case['execution']['max_memory_gib'])
            elif name=='convert':
                rt.run('cpu',['ElmerGrid','14','2','/work/mesh/board.msh','-out','/work/convert/mesh'],output=out,env=env,log=folder/'stage.log',memory_gib=case['execution']['max_memory_gib'])
                from .worker import audit_conversion
                audit_conversion(out/'mesh/physical-groups.json',folder/'mesh',folder/'stage.log')
            elif name=='solve':
                mesh_info=json.loads((out/'mesh/physical-groups.json').read_text())
                frames=1 if case['analysis']=='steady' else round((case['transient']['on_s']+case['transient']['off_s'])/case['transient']['field_interval_s'])
                # Reserve uncompressed field size: lossless compression normally
                # reduces this substantially, but must not be assumed to succeed.
                field_budget=(56*mesh_info['nodes']+40*mesh_info['elements'])*frames
                free=shutil.disk_usage(out).free
                if free<field_budget+2*1024**3:
                    raise WorkflowError(f'Run needs a conservative {field_budget/1024**3:.1f} GiB field budget; only {free/1024**3:.1f} GiB is free')
                copytree(out/'convert/mesh',folder/'mesh')
                if override_text is not None:(folder/'case.override.sif').write_text(override_text)
                rt.run('tools',command+['--backend',selected,'--ranks',str(effective_ranks)],project=project,output=out,env=env,log=folder/'stage.log')
                rt.run(selected,['/usr/bin/python3','-m','pcb_thermal.worker','compile','/work/case.resolved.json'],output=out,env=env,log=folder/'stage.log')
                for module in ('ScheduledHeat.so','FastAudit.so','ThermalScalars.so'):shutil.copy2(folder/module,folder/'mesh'/module)
                if effective_ranks>1:rt.run(selected,['ElmerGrid','2','2','/work/solve/mesh','-metis',str(effective_ranks)],output=out,env=env,log=folder/'stage.log')
                solver=['ElmerSolver','case.sif'] if effective_ranks==1 else ['mpirun','-np',str(effective_ranks),'ElmerSolver_mpi','case.sif']
                measured=['/usr/bin/python3','-m','pcb_thermal.measure','/work/resources.json','--heartbeat','/work/.solver-active']
                if 'max_memory_gib' in case['execution']:measured+=['--max-rss-gib',str(case['execution']['max_memory_gib'])]
                measured+=['--']+solver
                from concurrent.futures import ThreadPoolExecutor
                active=folder/'.solver-active';active.touch()
                compress=['/usr/bin/python3','-m','pcb_thermal.measure','/work/compression-resources.json',
                          '--max-rss-gib',str(case['execution']['max_memory_gib']),
                          '--','pvpython','/workflow/scripts/compress-fields.py','/work']
                def solve():
                    try:
                        rt.run(selected,measured,output=folder,gpu_count=case['execution']['gpu_count'] if selected=='cuda' else 0,env=env,log=folder/'solver.log',memory_gib=case['execution']['max_memory_gib'])
                    finally:active.unlink(missing_ok=True)
                streaming=case['analysis']=='transient' and case.get('outputs',{}).get('compress_during_solve',case['execution']['cpu_solver']!='cholmod')
                if streaming:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        compressor=pool.submit(rt.run,'tools',compress+['--watch'],output=folder,log=folder/'compression.log',memory_gib=case['execution']['max_memory_gib'])
                        solve();compressor.result()
                else:
                    # Direct factors can occupy most of RAM. Release the solver
                    # before allocating VTK's verification/compression arrays.
                    solve()
                    rt.run('tools',compress,output=folder,log=folder/'compression.log',memory_gib=case['execution']['max_memory_gib'])
                from .solver import verify_log
                verify_log(folder/'solver.log')
            complete(folder,key)
            if name in ('extract','mesh','convert'):
                if reusable.exists():shutil.rmtree(reusable)
                reusable.parent.mkdir(parents=True,exist_ok=True);copytree(folder,reusable);complete(reusable,key)
            provenance['last_stage']=name;write_json(out/'provenance.json',provenance)
        after={s:digest(project/s) for s in source}
        if after!=source:raise WorkflowError('Source hashes changed during run')
        provenance.update(status='complete',completed_unix=time.time(),source_preserved=True)
    except BaseException as error:
        provenance.update(status='failed',error=str(error));write_json(out/'provenance.json',provenance);raise
    write_json(out/'provenance.json',provenance)
    return out
