"""Explicit resistor loads and alternating channels in a configured resistor chain."""
import math
from .common import WorkflowError

def validate_sources(cfg):
    """Validate source inputs without requiring extracted board geometry."""
    if not isinstance(cfg, dict):
        raise WorkflowError('sources must be an object')
    refs = cfg.get('refs')
    if (not isinstance(refs, list) or not refs or
            any(not isinstance(r, str) or not r.strip() for r in refs) or len(set(refs)) != len(refs)):
        raise WorkflowError('sources.refs must be a nonempty unique list of references')
    if cfg.get('kind') not in ('watts', 'balancing'):
        raise WorkflowError('sources.kind must explicitly be balancing or watts')

    def number(value, name, zero=False):
        try:
            if isinstance(value, bool): raise ValueError()
            n = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkflowError(f'{name} must be numeric') from exc
        if not math.isfinite(n) or (n < 0 if zero else n <= 0):
            raise WorkflowError(f'{name} must be finite and {"nonnegative" if zero else "positive"}')
        return n

    if cfg['kind'] == 'watts':
        watts = cfg.get('watts')
        if not isinstance(watts, dict) or any(ref not in watts for ref in refs):
            raise WorkflowError('sources.watts must supply a power for every sources.refs entry')
        total = sum(number(watts[r], 'sources.watts.' + r, zero=True) for r in refs)
        if not math.isfinite(total) or total <= 0:
            raise WorkflowError('Heater powers must have finite positive total input')
        return

    nodes = cfg.get('nodes')
    if not isinstance(nodes, list) or len(nodes) < 2:
        raise WorkflowError('Balancing requires sources.nodes: at least two ordered nodes with ref, balance_net, tap_net and expected_value')
    for node in nodes:
        if not isinstance(node, dict) or any(
                not isinstance(node.get(k), str) or not node[k].strip()
                for k in ('ref', 'balance_net', 'tap_net', 'expected_value')):
            raise WorkflowError('Each sources.nodes entry requires ref, balance_net, tap_net and expected_value strings')
    node_refs = [n['ref'] for n in nodes]
    if len(set(node_refs)) != len(nodes) or set(node_refs) != set(refs):
        raise WorkflowError('sources.nodes must cover sources.refs exactly once')
    balance = [n['balance_net'] for n in nodes]
    taps = [n['tap_net'] for n in nodes]
    if len(set(balance)) != len(nodes) or len(set(taps)) != len(nodes) or set(balance) & set(taps):
        raise WorkflowError('Balancing and tap nets must be unique and disjoint')
    if cfg.get('schedule', 'automatic') not in ('automatic', 'odd', 'even'):
        raise WorkflowError('Only automatic, odd and even schedules are supported')
    for key in ('cell_voltage_v', 'resistance_ohm', 'switch_resistance_ohm', 'phase_s'):
        if key not in cfg:
            raise WorkflowError('Balancing requires explicit sources.' + key)
        number(cfg[key], 'sources.' + key, zero=key == 'switch_resistance_ohm')

def derive(board, case):
    cfg=case['sources']; components={x['ref']:x for x in board['components']}
    validate_sources(cfg)
    selected={}
    for ref in cfg['refs']:
        if ref not in components:raise WorkflowError(f'Heat-source reference not found: {ref}')
        c=components[ref]
        if c.get('dnp'):raise WorkflowError(f'Heat-source reference is DNP: {ref}')
        selected[ref]=c
    if cfg['kind']=='watts':
        powers={ref:float(cfg['watts'][ref]) for ref in selected}
        if any(not math.isfinite(p) or p<0 for p in powers.values()) or sum(powers.values())<=0:
            raise WorkflowError('Heater powers must be finite and nonnegative, with positive total input')
        return {'schema':1,'kind':'watts','resistors':{r:{'odd_w':p,'even_w':p,'average_w':p} for r,p in powers.items()},'total_w':sum(powers.values()),'switch_loss_w':0.0}
    nodes={}
    for n,node in enumerate(cfg['nodes']):
        ref=node['ref'];c=selected[ref]
        actual=[pad['net'] for pad in c['pads']]
        expected=[node['balance_net'],node['tap_net']]
        if sorted(actual)!=sorted(expected):
            raise WorkflowError(f'{ref}: pad nets {actual} do not match configured nets {expected}')
        if c['value']!=node['expected_value']:
            raise WorkflowError(f'{ref}: value {c["value"]!r} does not match expected_value {node["expected_value"]!r}')
        nodes[n]=ref
    r=float(cfg['resistance_ohm']);rf=float(cfg['switch_resistance_ohm']);v=float(cfg['cell_voltage_v'])
    current=v/(2*r+rf); p=current*current*r
    result={ref:{'cb_node':n,'odd_w':0.0,'even_w':0.0} for n,ref in nodes.items()}
    channels=[]
    for channel in range(1,len(nodes)):
        phase='odd' if channel%2 else 'even'
        for n in (channel-1,channel):result[nodes[n]][phase+'_w']+=p
        channels.append({'channel':channel,'phase':phase,'refs':[nodes[channel-1],nodes[channel]],'current_a':current,'resistor_power_w':2*p,'switch_power_w':current*current*rf,'cell_power_w':v*current})
    schedule=cfg.get('schedule','automatic')
    for x in result.values(): x['average_w']=(x['odd_w']+x['even_w'])/2 if schedule=='automatic' else x[schedule+'_w']
    active=(len(channels)/2 if schedule=='automatic' else sum(c['phase']==schedule for c in channels))
    return {'schema':1,'kind':'balancing','schedule':schedule,'phase_s':float(cfg['phase_s']),
            'cell_voltage_v':v,'resistance_ohm':r,'switch_resistance_ohm':rf,
            'resistors':result,'channels':channels,'total_w':sum(x['average_w'] for x in result.values()),
            'switch_loss_w':active*current*current*rf,'assumption':'Configured constant cell voltage and equal resistor values; no diagnostic/thermal pauses; only external resistor losses heat the FEM model.'}

def at_time(power,ref,t,on_s):
    if t>=on_s:return 0.0
    x=power['resistors'][ref]
    if power.get('schedule')=='automatic':return x['odd_w'] if int(t/power['phase_s'])%2==0 else x['even_w']
    return x['average_w']
