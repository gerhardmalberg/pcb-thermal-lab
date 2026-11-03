"""Native Elmer scalar interpretation, heat accounting and review reports."""
import csv
import json
from pathlib import Path
import numpy as np
from .common import WorkflowError,write_json,digest

def report(out,case):
    out=Path(out);dest=out/'postprocess';dest.mkdir(exist_ok=True)
    audit=json.loads((out/'solve/audit.json').read_text());power=json.loads((out/'solve/power.json').read_text())
    native=out/'solve/scalars.dat'
    if (out/'solve/fast-scalars.dat').exists():
        reference=np.atleast_2d(np.loadtxt(native))
        fast=np.atleast_2d(np.loadtxt(out/'solve/fast-scalars.dat'))
        if not np.allclose(reference[-1],fast[-1],rtol=1e-8,atol=1e-6):
            raise WorkflowError('Cached scalar history differs from native SaveScalars verification')
        native=out/'solve/fast-scalars.dat'
    data=np.atleast_2d(np.loadtxt(native));columns=audit['columns']
    if data.shape[1]!=len(columns):raise WorkflowError(f'Native scalar columns differ from schema ({data.shape[1]} vs {len(columns)}); inspect scalars.dat.names')
    if not np.isfinite(data).all():raise WorkflowError('Nonfinite thermal results')
    values={name:data[:,i] for i,name in enumerate(columns)}
    ambient=audit['ambient_k'];area=values.get('exposed_area_m2',audit['surface_area_m2']);h=case['cooling']['h_w_m2k']
    energy=values['energy_absolute_j']-ambient*audit['total_heat_capacity_j_k']
    removed=h*(values['exposed_temperature_integral_k_m2']-ambient*area)
    transient=case['analysis']=='transient';times=values['time_s']
    input_power=np.full(len(data),power['total_w'])
    if transient:
        if power.get('schedule')=='automatic':
            odd=sum(p['odd_w'] for p in power['resistors'].values())
            even=sum(p['even_w'] for p in power['resistors'].values())
            odd_phase=(np.floor((times-1e-8)/power['phase_s']).astype(int)%2)==0
            input_power=np.where(odd_phase,odd,even)
        input_power[times>case['transient']['on_s']]=0.
    residual=input_power-removed
    if transient:
        dt=np.diff(np.r_[0,times]);storage_rate=np.diff(np.r_[0,energy])/dt
        residual-=storage_rate
        denom=max(power['total_w'],1e-20)
        balance=float(np.max(np.abs(residual))/denom)
    else:balance=float(abs(residual[-1])/max(power['total_w'],1e-20))
    enriched={**values,'heat_input_w':input_power,'heat_removed_w':removed,'stored_energy_j':energy,'heat_balance_residual_w':residual}
    with (dest/'history.csv').open('w') as stream:
        writer=csv.writer(stream, lineterminator="\n");writer.writerow(enriched)
        writer.writerows(zip(*enriched.values()))
    resistors=[]
    for ref,p in power['resistors'].items():
        resistors.append({'reference':ref,'average_power_w':p['average_w'],'odd_power_w':p['odd_w'],'even_power_w':p['even_w'],
                          'minimum_c':float(values[ref+'_min_k'].min()-273.15),
                          'maximum_c':float(values[ref+'_max_k'].max()-273.15),'final_mean_c':float(values[ref+'_mean_k'][-1]-273.15)})
    with (dest/'resistors.csv').open('w') as stream:
        w=csv.DictWriter(stream,fieldnames=list(resistors[0]), lineterminator="\n");w.writeheader();w.writerows(resistors)
    summary={'schema':1,'case':case.get('name'),'analysis':case['analysis'],'ambient_c':case['cooling']['ambient_c'],'h_w_m2k':h,
             'resistor_heat_w':power['total_w'],'excluded_switch_loss_w':power['switch_loss_w'],
             'maximum_temperature_c':float(values['temperature_max_k'].max()-273.15),
             'minimum_temperature_c':float(values['temperature_min_k'].min()-273.15),
             'board_minimum_c':float(values['board_min_k'].min()-273.15) if 'board_min_k' in values else None,
             'board_maximum_c':float(values['board_max_k'].max()-273.15),'final_heat_removed_w':float(removed[-1]),
             'maximum_heat_balance_relative_error':balance,'heat_balance_pass':balance<0.01,
             'mesh_convergence':'requires comparison study','hardware_correlation':'not performed','resistors':resistors,
             'assumptions':case.get('assumptions',[]),'native_scalar_file':'../solve/scalars.dat','native_column_names':'../solve/scalars.dat.names'}
    summary.update(materials=case['materials'],geometry=case['geometry'],cooling=case['cooling'],
                   native_scalar_file='../solve/'+native.name,native_column_names='../solve/'+native.name+'.names',
                   resources=json.loads((out/'solve/resources.json').read_text()) if (out/'solve/resources.json').exists() else None)
    override=(out/'solve/case.override.sif').exists()
    summary['solver_input']={'authoritative_file':'../solve/case.sif','sha256':digest(out/'solve/case.sif'),
                             'manually_supplied':override,'json_physics_verified_against_manual_sif':False if override else None}
    write_json(dest/'summary.json',summary)
    text=[f'# {case.get("name","Thermal result")}','',
          f'Maximum predicted temperature: **{summary["maximum_temperature_c"]:.3f} °C**. Board maximum: **{summary["board_maximum_c"]:.3f} °C**.',
          f'Resistor heat: {power["total_w"]:.6f} W; excluded switch losses: {power["switch_loss_w"]:.6f} W.',
          f'Ambient {case["cooling"]["ambient_c"]} °C; convection h = {h} W/m²K.',
          f'Heat-balance relative error: {balance:.3%}; criterion <1%: {"PASS" if balance<.01 else "FAIL"}.','',
          'These are resistor-only numerical predictions. Mesh/domain convergence is a separate check; hardware correlation has not been performed.','',
          '| Resistor | Mean input W | Maximum °C | Final mean °C |','|---|---:|---:|---:|']
    text += [f'| {r["reference"]} | {r["average_power_w"]:.6f} | {r["maximum_c"]:.3f} | {r["final_mean_c"]:.3f} |' for r in resistors]
    if override:
        text+=['','The manually supplied [SIF](../solve/case.sif) is authoritative. The material, cooling and source tables below are declarations from the case JSON; their agreement with manual SIF edits has not been verified. Keep both inputs consistent for heat accounting.','']
    text+=['','## Declared material and geometry inputs','',
           '| Material | kx, ky, kz W/mK | Density kg/m³ | Heat capacity J/kgK |',
           '|---|---|---:|---:|']+[f'| {n} | {m["k"]} | {m["density"]} | {m["heat_capacity"]} |' for n,m in case['materials'].items()]
    text+=['',f'Geometry: `{json.dumps(case["geometry"],sort_keys=True)}`.','',
           '## Assumptions','']+['- '+a for a in case.get('assumptions',[])]+['','## Files','',
            '- [Native solver files](../solve/), including SIF, logs, mesh, restart, VTU/PVTU, scalar and line output.',
            '- [History CSV](history.csv), [resistor CSV](resistors.csv), [machine-readable report](summary.json).',
            '- ParaView state and rendered figures are in this directory.','']
    (dest/'report.md').write_text('\n'.join(text))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    if transient:
        fig,axs=plt.subplots(2,1,figsize=(10,7),sharex=True,layout='constrained')
        for ref in power['resistors']:axs[0].plot(times,values[ref+'_max_k']-273.15,label=ref,lw=.8)
        axs[0].set_ylabel('Resistor maximum (°C)');axs[0].legend(ncol=6,fontsize=7)
        axs[1].plot(times,input_power,label='Resistor heat');axs[1].plot(times,removed,label='Heat removed');axs[1].set_ylabel('Power (W)');axs[1].set_xlabel('Time (s)');axs[1].legend()
        fig.savefig(dest/'warmup-cooldown.png',dpi=160);plt.close(fig)
    else:
        fig,ax=plt.subplots(figsize=(10,4),layout='constrained')
        ax.bar([r['reference'] for r in resistors],[r['maximum_c'] for r in resistors]);ax.set_ylabel('Maximum body temperature (°C)');ax.tick_params(axis='x',rotation=60)
        fig.savefig(dest/'resistor-temperatures.png',dpi=160);plt.close(fig)
    if not summary['heat_balance_pass']:raise WorkflowError(f'Heat balance failed ({balance:.3%}); report retained at {dest}')
    return summary
