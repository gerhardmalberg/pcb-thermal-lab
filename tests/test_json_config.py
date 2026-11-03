"""The public case format needs only the Python standard library."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from pcb_thermal.common import WorkflowError, read_config


class JsonConfig(unittest.TestCase):
    def test_json_case_without_site_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'case.json'
            expected = {'schema':1, 'name':'PCB °C', 'execution':{'backend':'cpu'}}
            path.write_text(json.dumps(expected), encoding='utf-8')
            self.assertEqual(read_config(path), expected)
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
            result = subprocess.run([sys.executable, '-S', '-c',
                'import json,sys; from pcb_thermal.common import read_config; print(json.dumps(read_config(sys.argv[1])))',
                str(path)], env=env, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), expected)

    def test_non_json_syntax_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'case.json'
            for text in ['schema: 1\nname: board\n', '{"name":"board",}', '{"schema":']:
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaisesRegex(WorkflowError, 'Invalid JSON'):
                        read_config(path)

    def test_case_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'case.json'
            for value in [[], 'board', 1, None]:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaisesRegex(WorkflowError, 'mapping'):
                        read_config(path)

    def test_missing_case_has_a_workflow_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(WorkflowError, 'Cannot read'):
                read_config(Path(directory)/'missing.json')


class SourceAndPackageConfig(unittest.TestCase):
    def fixture(self, root):
        from pcb_thermal.common import ROOT
        case=json.loads((ROOT/'examples/layered-via/case.json').read_text())
        case.update(project_root='.',board='board.kicad_pcb',output='results')
        (root/'board.kicad_pcb').write_text('(kicad_pcb)')
        return case

    def test_package_dimensions_are_explicit_and_physical(self):
        from pcb_thermal.config import load
        import copy
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=self.fixture(root);path=root/'case.json'
            for key,value in [('resistor_body_mm',None),('terminal_width_mm',None),
                              ('resistor_body_mm',[1,2]),('resistor_body_mm',[1,0,1]),
                              ('resistor_body_mm',[1,2,float('nan')]),('terminal_width_mm',2),
                              ('terminal_width_mm',False)]:
                case=copy.deepcopy(base)
                if value is None:del case['geometry'][key]
                else:case['geometry'][key]=value
                path.write_text(json.dumps(case))
                with self.subTest(key=key,value=value),self.assertRaises(WorkflowError):load(path)

    def test_fixed_watts_has_no_implicit_phase_constraint(self):
        from pcb_thermal.config import load
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=self.fixture(root);path=root/'case.json'
            case.update(analysis='transient',transient={'on_s':12.,'off_s':6.,'step_s':3.,'field_interval_s':6.})
            path.write_text(json.dumps(case))
            self.assertEqual(load(path)[1]['transient']['step_s'],3.)
            del case['sources']['kind'];path.write_text(json.dumps(case))
            with self.assertRaisesRegex(WorkflowError,'sources.kind'):load(path)

    def test_balancing_phase_must_align_with_timestep(self):
        from pcb_thermal.config import load
        from test_physics import network
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case=self.fixture(root);path=root/'case.json'
            _,fixture=network();case['sources']=fixture['sources']
            case.update(analysis='transient',transient={'on_s':12.,'off_s':6.,'step_s':3.,'field_interval_s':6.})
            path.write_text(json.dumps(case))
            with self.assertRaisesRegex(WorkflowError,'Timestep'):load(path)
            case['sources']['phase_s']=6.;path.write_text(json.dumps(case))
            self.assertEqual(load(path)[1]['sources']['phase_s'],6.)
