import tempfile
import unittest
from pathlib import Path
from pcb_thermal.pipeline import source_inputs

class SourceInputsTests(unittest.TestCase):
    def test_editor_state_is_ignored_but_design_inputs_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);board=root/'board.kicad_pcb';project=root/'board.kicad_pro'
            board.write_text('(kicad_pcb (model "${KIPRJMOD}/part.step"))')
            project.write_text('{}');(root/'part.step').write_text('solid')
            baseline=source_inputs({},root,board)
            for name in ('board.kicad_prl','_autosave-board.kicad_pcb','~board.kicad_pcb.lck','board.kicad_pcb-bak'):
                (root/name).write_text('local state')
            self.assertEqual(source_inputs({},root,board),baseline)
            for path in (project,board,root/'part.step'):
                original=path.read_text();path.write_text(original+' ')
                self.assertNotEqual(source_inputs({},root,board),baseline)
                path.write_text(original)
            self.assertEqual(source_inputs({},root,board),baseline)
