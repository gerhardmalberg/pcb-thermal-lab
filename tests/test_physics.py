import copy
import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from pcb_thermal.power import derive
from pcb_thermal.pipeline import valid_cache,complete
from pcb_thermal.common import WorkflowError

def network(channels=4, resistance=12., voltage=3., switch=0.75):
    nodes=[{'ref':f'R{n+1}', 'tap_net':f'tap-{n}', 'balance_net':f'sw-{n}',
            'expected_value':f'{resistance:g}R test'} for n in range(channels+1)]
    components=[{'ref':n['ref'], 'value':n['expected_value'],
                 'pads':[{'net':n['tap_net']},{'net':n['balance_net']}]} for n in nodes]
    sources={'refs':[n['ref'] for n in reversed(nodes)], 'nodes':nodes, 'kind':'balancing',
             'schedule':'automatic', 'resistance_ohm':resistance, 'switch_resistance_ohm':switch,
             'cell_voltage_v':voltage, 'phase_s':2.}
    return {'components':components}, {'sources':sources}

class Physics(unittest.TestCase):
    def test_power_against_independent_nodal_circuit(self):
        for count,r,v,rf in [(1,12.,3.,.75),(3,8.,2.5,.4),(4,15.,3.5,1.2),(6,10.,2.,.5)]:
            board,case=network(count,r,v,rf);power=derive(board,case)
            tap=np.arange(count+1)*v;phase_totals={};switch_totals={}
            for phase,channels in [('odd',range(1,count+1,2)),('even',range(2,count+1,2))]:
                a=np.eye(count+1)/r;b=tap/r
                for n in channels:
                    conductance=1/rf
                    a[n,n]+=conductance;a[n-1,n-1]+=conductance
                    a[n,n-1]-=conductance;a[n-1,n]-=conductance
                internal=np.linalg.solve(a,b);loss=(tap-internal)**2/r
                phase_totals[phase]=loss.sum()
                switch_totals[phase]=sum((internal[n]-internal[n-1])**2/rf for n in channels)
                for n in range(count+1):
                    self.assertAlmostEqual(loss[n],power['resistors'][f'R{n+1}'][phase+'_w'],places=11)
            for schedule in ('automatic','odd','even'):
                with self.subTest(channels=count,schedule=schedule):
                    case['sources']['schedule']=schedule;power=derive(board,case)
                    expected=lambda totals:sum(totals.values())/2 if schedule=='automatic' else totals[schedule]
                    self.assertAlmostEqual(power['total_w'],expected(phase_totals),places=11)
                    self.assertAlmostEqual(power['switch_loss_w'],expected(switch_totals),places=11)
                    for x in power['resistors'].values():
                        self.assertAlmostEqual(x['average_w'],(x['odd_w']+x['even_w'])/2 if schedule=='automatic' else x[schedule+'_w'])

    def test_zero_switch_resistance_and_phase_boundaries(self):
        from pcb_thermal.power import at_time
        board,case=network(3,10.,2.,0.)
        power=derive(board,case)
        self.assertEqual(power['switch_loss_w'],0.)
        self.assertAlmostEqual(power['total_w'],.3)
        self.assertAlmostEqual(at_time(power,'R1',0,10),.1)
        self.assertEqual(at_time(power,'R1',2,10),0.)
        self.assertAlmostEqual(at_time(power,'R1',4,10),.1)
        self.assertEqual(at_time(power,'R1',10,10),0.)

    def test_reject_wrong_board_connectivity_value_and_population(self):
        for mutation in ('net','value','missing','dnp','extra_pad'):
            board,case=network()
            c=board['components'][0]
            if mutation=='net':c['pads'][0]['net']='wrong-tap'
            elif mutation=='value':c['value']='different resistor'
            elif mutation=='missing':board['components'].pop(0)
            elif mutation=='dnp':c['dnp']=True
            else:c['pads'].append({'net':'extra'})
            with self.subTest(mutation=mutation),self.assertRaises(WorkflowError):derive(board,case)

    def test_explicit_source_configuration_and_nodes(self):
        for key in ('kind','nodes','cell_voltage_v','resistance_ohm','switch_resistance_ohm','phase_s'):
            board,case=network();del case['sources'][key]
            with self.subTest(missing=key),self.assertRaisesRegex(WorkflowError,key):derive(board,case)
        for nodes in (None,[],[{}],'automatic'):
            board,case=network();case['sources']['nodes']=nodes
            with self.subTest(nodes=nodes),self.assertRaises(WorkflowError):derive(board,case)
        for mutation in ('duplicate_ref','missing_ref','extra_ref','duplicate_tap','duplicate_balance','short','missing_value'):
            board,case=network();nodes=case['sources']['nodes']
            if mutation=='duplicate_ref':nodes[0]['ref']=nodes[1]['ref']
            elif mutation=='missing_ref':nodes.pop()
            elif mutation=='extra_ref':nodes[-1]['ref']='Rextra'
            elif mutation=='duplicate_tap':nodes[0]['tap_net']=nodes[1]['tap_net']
            elif mutation=='duplicate_balance':nodes[0]['balance_net']=nodes[1]['balance_net']
            elif mutation=='short':nodes[0]['tap_net']=nodes[0]['balance_net']
            else:del nodes[0]['expected_value']
            with self.subTest(mutation=mutation),self.assertRaises(WorkflowError):derive(board,case)

    def test_invalid_electrical_parameters(self):
        for key in ('cell_voltage_v','resistance_ohm','switch_resistance_ohm','phase_s'):
            for value in (-1,float('nan'),float('inf'),None,True,'invalid'):
                board,case=network();case['sources'][key]=value
                with self.subTest(key=key,value=value),self.assertRaises(WorkflowError):derive(board,case)
        for key in ('cell_voltage_v','resistance_ohm','phase_s'):
            board,case=network();case['sources'][key]=0
            with self.assertRaises(WorkflowError):derive(board,case)
        board,case=network();case['sources']['schedule']='all'
        with self.assertRaises(WorkflowError):derive(board,case)

    def test_fixed_watts(self):
        board,_=network(1)
        case={'sources':{'kind':'watts','refs':['R1','R2'],'watts':{'R1':.15,'R2':0}}}
        power=derive(board,case)
        self.assertEqual(power['total_w'],.15)
        self.assertEqual(power['switch_loss_w'],0.)
        self.assertEqual(power['resistors']['R1'],{'odd_w':.15,'even_w':.15,'average_w':.15})
        for watts in ({'R1':.1},{'R1':0,'R2':0},{'R1':-.1,'R2':.2},{'R1':float('nan'),'R2':.2}):
            case['sources']['watts']=watts
            with self.assertRaises(WorkflowError):derive(board,case)

    def test_cache_detects_mutated_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'field').write_text('valid');complete(p,'key');self.assertTrue(valid_cache(p,'key'))
            (p/'field').write_text('altered');self.assertFalse(valid_cache(p,'key'));self.assertFalse(valid_cache(p,'different'))

class ExecutionContracts(unittest.TestCase):
    def test_explicit_cpu_never_probes_gpu(self):
        from pcb_thermal.runtime import Runtime
        class CPUOnly(Runtime):
            def image_exists(self,kind):
                self.assert_kind=kind
                return kind=='cpu'
            def gpu_check(self,*args):raise AssertionError('CPU attempted a GPU probe')
        self.assertEqual(CPUOnly().select('cpu')[0],'cpu')

    def test_failed_native_log_not_accepted(self):
        from pcb_thermal.solver import verify_log
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'solver.log'
            for content in ['HeatSolve: starting','ERROR:: HeatSolver failure\nALL DONE','failed to converge\nALL DONE']:
                p.write_text(content)
                with self.assertRaises(WorkflowError):verify_log(p)

if __name__=='__main__':unittest.main()
