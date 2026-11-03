"""Bounded CPU steady studies with verified historical results and checkpoints."""
import copy
import json
import math
import os
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .common import ROOT, WorkflowError, digest, fingerprint, write_json
from .config import load
from .pipeline import run, valid_cache, STAGES
from .runtime import Runtime
from .solver import verify_log
from .study import compare_runs, write_study_report


class BudgetExceeded(WorkflowError):
    pass


def steady_cases(schedules):
    if not schedules or len(set(schedules)) != len(schedules):
        raise WorkflowError('Choose distinct automatic, odd or even schedules')
    if any(s not in ('automatic', 'odd', 'even') for s in schedules):
        raise WorkflowError('Unknown balancing schedule')
    return [(s, h) for h in (10, 5, 50, 20) for s in schedules]


@contextmanager
def time_limit(seconds):
    if seconds <= 0:
        raise BudgetExceeded('No simulation time remains')
    def expired(signum, frame):
        raise BudgetExceeded('Case or overall wall-clock allowance exhausted')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def physical_signature(case, sources):
    return fingerprint({
        'design_sources': sources,
        'analysis': case['analysis'],
        'geometry': case['geometry'], 'mesh': case['mesh'],
        'materials': case['materials'], 'mechanicals': case.get('mechanicals', []),
        'cooling': {k:v for k,v in case['cooling'].items() if k != 'h_w_m2k'},
        'sources': {k:v for k,v in case['sources'].items() if k != 'schedule'},
    })


def verified_record(folder, reference=None, normalization_manifest=None):
    """Validate a completed historical run, without relabeling it as a new solve."""
    folder = Path(folder)
    provenance = json.loads((folder/'provenance.json').read_text())
    case = json.loads((folder/'case.resolved.json').read_text())
    if provenance.get('status') != 'complete' or not provenance.get('source_preserved'):
        raise WorkflowError(f'Incomplete/source-unverified result: {folder}')
    if case != provenance.get('case'):
        raise WorkflowError(f'Saved configuration differs from provenance: {folder}')
    if (folder/'solve/case.override.sif').exists():
        raise WorkflowError('Automated schedule studies cannot verify physical equivalence of manual SIF overrides')
    if case['analysis'] != 'steady' or provenance.get('backend') != 'cpu':
        raise WorkflowError('This study accepts completed CPU steady results only')
    for stage in STAGES:
        saved = json.loads((folder/stage/'stage.json').read_text())
        if not valid_cache(folder/stage, saved['key']):
            raise WorkflowError(f'Changed or missing {stage} artifacts: {folder}')
    project = Path(case['project_root'])
    from .source_normalization import verify_sources
    try:
        verify_sources(project, provenance['source'], normalization_manifest)
    except ValueError as exc:
        raise WorkflowError(f"Design inputs changed or normalization verification failed: {exc}") from exc
    verify_log(folder/'solve/solver.log')
    summary = json.loads((folder/'postprocess/summary.json').read_text())
    if not summary['heat_balance_pass'] or summary['maximum_heat_balance_relative_error'] >= .01:
        raise WorkflowError(f'Heat accounting failed: {folder}')
    mesh = {name:digest(folder/'convert/mesh'/name) for name in
            ('mesh.nodes', 'mesh.elements', 'mesh.boundary')}
    signature = physical_signature(case, provenance['source'])
    record = {'case': folder.name, 'path': str(folder), 'summary': summary,
              'physical_signature': signature, 'converted_mesh_hashes': mesh,
              'verified_completed_artifacts': True}
    if reference and (signature != reference['physical_signature'] or mesh != reference['converted_mesh_hashes']):
        raise WorkflowError(f'{folder.name} differs from the automatic baseline inputs or converted mesh')
    return record


class Progress:
    def __init__(self, root, schedules, budget_minutes):
        self.root = Path(root)
        self.records = []
        self.data = {'schema': 1, 'status': 'preparing', 'schedules': schedules,
                     'time_budget_minutes': budget_minutes, 'completed': self.records,
                     'new_completed': [], 'optional_detail': {'status': 'not_requested'},
                     'scope': 'CPU steady comparisons; full transient and GPU work deferred'}
        self.started = None

    def save(self, status, active=None, error=None, report=True):
        self.data.update(status=status, active=active)
        self.data['validation_status'] = ('domain_unconverged' if self.data['optional_detail']['status'] == 'unconverged'
                                          else 'full_domain_validation_deferred')
        if error is not None: self.data['error'] = str(error)
        if self.started is not None:
            self.data['wall_seconds'] = time.monotonic()-self.started
        write_json(self.root/'study-steady.json', self.data)
        if report and self.records:
            write_study_report(self.root, 'steady', self.records, status, detail=self.data['optional_detail'])

    def add(self, record, new=False):
        self.records[:] = [r for r in self.records if r['case'] != record['case']]
        self.records.append(record)
        if new and record['case'] not in self.data['new_completed']:
            self.data['new_completed'].append(record['case'])


def study(case_path, section='steady', schedules=None, time_budget_minutes=None, detail_check=False, first_case_minutes=20):
    if section != 'steady':
        if schedules is not None or time_budget_minutes is not None or detail_check or first_case_minutes != 20:
            raise WorkflowError('Schedule selection, time budgets and limited detail checks apply to steady studies')
        return subprocess.call([os.sys.executable, str(ROOT/'scripts/run-extended-study.py'),
                                str(Path(case_path).resolve()), '--section', section])
    schedules = schedules or ['automatic', 'odd', 'even']
    if Runtime().engine == 'native':
        raise WorkflowError('Bounded studies require Podman or Docker for owned-container cleanup')
    selected = steady_cases(schedules)
    if time_budget_minutes is not None and (not math.isfinite(time_budget_minutes) or time_budget_minutes <= 2):
        raise WorkflowError('The study budget must exceed the two-minute shutdown reserve')
    if not math.isfinite(first_case_minutes) or not 0 < first_case_minutes <= 20:
        raise WorkflowError('The first-case allowance must be positive and at most 20 minutes')
    _, base, project, board, baseline = load(case_path)
    if base['analysis'] != 'steady': raise WorkflowError('Provide a steady study case')
    if base['sources']['kind'] != 'balancing':
        raise WorkflowError('Schedule studies require balancing sources; use run or sweep for fixed watts')
    root = baseline.parent; root.mkdir(parents=True, exist_ok=True)
    inputs = root/'study-inputs'; inputs.mkdir(exist_ok=True)
    progress = Progress(root, schedules, time_budget_minutes)
    progress.data['first_case_minutes'] = first_case_minutes
    if detail_check:
        progress.data['optional_detail'] = {'status': 'pending'}
    owner = uuid.uuid4().hex
    previous_owner = os.environ.get('PCB_THERMAL_BATCH_ID')
    os.environ['PCB_THERMAL_BATCH_ID'] = owner
    progress.data['owner'] = owner
    def interrupted(signum, frame): raise KeyboardInterrupt('Study interrupted')
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    total = time_budget_minutes*60 if time_budget_minutes is not None else math.inf
    started = time.monotonic(); progress.started = started
    progress.data['started_unix'] = time.time()
    reference = None
    status = 'failed'
    error = None
    exit_code = 1
    attempted = 0
    try:
        # Historical automatic cases are verified in place; they are never rerun
        # merely to refresh implementation hashes or overwrite their provenance.
        with time_limit(min(first_case_minutes*60, total-120)):
            for h in (10, 5, 50, 20):
                existing = root/f'automatic-h{h}'
                if existing.exists() and (existing/'postprocess/summary.json').exists():
                    record = verified_record(existing, reference)
                    reference = reference or record
                    progress.add(record)
            if reference and physical_signature(base, json.loads((Path(reference['path'])/'provenance.json').read_text())['source']) != reference['physical_signature']:
                raise WorkflowError('Requested baseline configuration differs from preserved automatic cases')
            progress.save('running')
        for schedule,h in selected:
            label = f'{schedule}-h{h}'
            if any(r['case'] == label for r in progress.records): continue
            remaining = total-(time.monotonic()-started)-120
            allowance = min(first_case_minutes*60-(time.monotonic()-started) if attempted == 0 else 12*60, remaining)
            if allowance <= 0: raise BudgetExceeded('Overall simulation budget exhausted')
            attempted += 1
            case = copy.deepcopy(base)
            case.update(name=label, project_root=str(project), board=str(board), output=str(root/label))
            case['sources']['schedule'] = schedule; case['cooling']['h_w_m2k'] = h
            case['execution'].update(backend='cpu', cpu_solver='hypre', mpi_ranks=2, threads_per_rank=1)
            path = inputs/(label+'.json'); write_json(path, case)
            progress.save('running', label, report=False)
            with time_limit(allowance):
                out = run(path, backend='cpu', mpi_ranks=2, threads_per_rank=1)
                record = verified_record(out, reference)
                reference = reference or record
                progress.add(record, new=True)
                progress.save('running')
        if detail_check:
            remaining = total-(time.monotonic()-started)
            progress.data['optional_detail'] = {'status': 'skipped_insufficient_time'}
            if remaining >= 25*60:
                label = 'limited-detail-20'
                progress.data['optional_detail'] = {'status': 'running', 'case': label}
                progress.save('running', label, report=False)
                case = copy.deepcopy(base)
                case.update(name=label, project_root=str(project), board=str(board), output=str(root/label))
                case['sources']['schedule'] = 'automatic'; case['cooling']['h_w_m2k'] = 10
                case['geometry']['detail_margin_mm'] = 20
                case['execution'].update(backend='cpu', cpu_solver='hypre', mpi_ranks=2, threads_per_rank=1)
                path = inputs/(label+'.json'); write_json(path, case)
                try:
                    with time_limit(min(20*60, remaining-120)):
                        out = run(path, backend='cpu', mpi_ranks=2, threads_per_rank=1)
                        verified_record(out)
                        result = compare_runs(baseline, out)
                        write_json(root/'limited-detail-comparison.json', result)
                        progress.data['optional_detail'] = {'status': 'pass' if result['pass'] else 'unconverged', 'case': label, 'comparison': result}
                except (BudgetExceeded, WorkflowError) as failure:
                    progress.data['optional_detail'] = {'status': 'incomplete', 'case': label, 'error': str(failure)}
        status = 'complete'; exit_code = 0
    except BudgetExceeded as failure:
        status = 'budget_exhausted'; error = failure; exit_code = 2
    except KeyboardInterrupt as failure:
        status = 'interrupted'; error = failure; exit_code = 130
    except Exception as failure:
        status = 'failed'; error = failure
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if progress.data['optional_detail']['status'] == 'pending':
            progress.data['optional_detail'] = {'status': 'skipped_batch_stopped'}
        try: Runtime().stop_batch(owner)
        except Exception as failure:
            status = 'failed'; error = f'Batch cleanup failed: {failure}'; exit_code = 1
        signal.signal(signal.SIGTERM, previous_term)
        if previous_owner is None: os.environ.pop('PCB_THERMAL_BATCH_ID', None)
        else: os.environ['PCB_THERMAL_BATCH_ID'] = previous_owner
        progress.save(status, error=error)
    print(f'Study {status}: {len(progress.data["new_completed"])} new cases, {len(progress.records)} completed total; {root}', flush=True)
    return exit_code
