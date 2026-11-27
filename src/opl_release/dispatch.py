"""Owner-workflow dispatch with a durable controller-run correlation key."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time

from .common import ReleaseError, gh
from .config import load_release_config_from_dir


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _run_matches(run: dict, *, control_run_id: str, workflow: str, ref: str, dispatched_at: float) -> bool:
    if run.get('path') and run['path'].rsplit('/', 1)[-1] != workflow:
        return False
    if run.get('event') != 'workflow_dispatch':
        return False
    if run.get('head_branch') != ref:
        return False
    created_at = _parse_time(run.get('created_at'))
    if created_at is not None and created_at + 5 < dispatched_at:
        return False
    marker = f'opl-release:{control_run_id}'
    return marker in str(run.get('display_title', '')) or marker in str(run.get('name', ''))


def dispatch_owner_workflow(
    repository: str,
    spec: dict,
    *,
    control_run_id: str | int,
    reconcile_seconds: int = 60,
) -> dict:
    """Dispatch one explicitly admitted owner lane and bind the resulting run.

    The owner workflow must echo `opl-release:<control_run_id>` in its run-name
    and accept the configured input. An empty dispatch response is reconciled
    by this marker and never by selecting the newest run.
    """
    if spec.get('enabled') is not True:
        raise ReleaseError('Owner dispatch is not explicitly enabled')
    repository = str(repository)
    workflow = str(spec.get('workflow', ''))
    ref = str(spec.get('ref', 'main')).strip()
    input_name = str(spec.get('control_run_id_input', ''))
    if not workflow or not ref or not input_name:
        raise ReleaseError('Owner dispatch is missing workflow, ref, or control input')
    marker_id = str(control_run_id)
    inputs = dict(spec.get('inputs') or {})
    inputs[input_name] = marker_id
    dispatched_at = time.time()
    response = gh(
        f'repos/{repository}/actions/workflows/{workflow}/dispatches',
        method='POST',
        payload={'ref': ref, 'inputs': inputs},
    )
    if isinstance(response, dict):
        run_id = response.get('workflow_run_id') or response.get('id')
        if run_id:
            return {
                'status': 'dispatch_submitted',
                'repository': repository,
                'workflow': workflow,
                'ref': ref,
                'control_run_id': marker_id,
                'workflow_run_id': int(run_id),
                'run_url': response.get('run_url') or response.get('html_url'),
            }

    deadline = time.monotonic() + max(0, reconcile_seconds)
    while True:
        runs = gh(f'repos/{repository}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=50') or {}
        matches = [
            run for run in runs.get('workflow_runs', [])
            if _run_matches(run, control_run_id=marker_id, workflow=workflow, ref=ref, dispatched_at=dispatched_at)
        ]
        if len(matches) == 1:
            run = matches[0]
            return {
                'status': 'dispatch_submitted',
                'repository': repository,
                'workflow': workflow,
                'ref': ref,
                'control_run_id': marker_id,
                'workflow_run_id': run.get('id'),
                'run_url': run.get('html_url'),
            }
        if len(matches) > 1:
            raise ReleaseError('Owner dispatch correlation is ambiguous; no retry was attempted')
        if time.monotonic() >= deadline:
            raise ReleaseError('Owner dispatch result is unknown; no retry was attempted')
        time.sleep(5)


def dispatch_configured_owner(
    owner_root: Path,
    *,
    expected_repository: str,
    expected_subject_id: str,
    control_run_id: str | int,
    reconcile_seconds: int = 60,
) -> dict:
    """Dispatch an owner workflow only when its primary lane opts in."""
    cfg = load_release_config_from_dir(owner_root)
    if cfg['repository'] != expected_repository or cfg['subject_id'] != expected_subject_id:
        raise ReleaseError('Owner release config identity does not match the selected fleet subject')
    if cfg['schedule'] != 'owner_schedule':
        return {
            'status': 'not_configured',
            'repository': cfg['repository'],
            'subject_id': cfg['subject_id'],
            'reason': 'schedule is not owner_schedule',
        }
    lanes = [lane for lane in cfg['lanes'] if lane.get('daily_dispatch')]
    if not lanes:
        return {
            'status': 'not_configured',
            'repository': cfg['repository'],
            'subject_id': cfg['subject_id'],
            'reason': 'primary lane has no daily_dispatch declaration',
        }
    lane = lanes[0]
    spec = dict(lane['daily_dispatch'])
    spec['workflow'] = lane['workflow']
    return dispatch_owner_workflow(
        cfg['repository'],
        spec,
        control_run_id=control_run_id,
        reconcile_seconds=reconcile_seconds,
    )
