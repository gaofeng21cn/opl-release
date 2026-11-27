"""Daily owner-workflow dispatch and GitHub run reconciliation."""
from __future__ import annotations

from pathlib import Path
import re
from subprocess import TimeoutExpired
import time
from urllib.parse import urlencode

from .common import ReleaseError, gh
from .config import load_release_config_from_dir

DISPATCH_ACTOR_ID = 356454848
DISPATCH_INPUT = 'opl_release_control_run_id'
DISPATCH_REF = 'main'
DISPATCH_API_VERSION = '2026-03-10'
API_FAILURES = (ReleaseError, TimeoutExpired, ValueError, OSError)


def _matches(run: dict, workflow: str, control_run_id: str) -> bool:
    return (
        run.get('path') == f'.github/workflows/{workflow}'
        and run.get('event') == 'workflow_dispatch'
        and run.get('head_branch') == DISPATCH_REF
        and (run.get('actor') or {}).get('id') == DISPATCH_ACTOR_ID
        and f'[opl-release:{control_run_id}]' in (run.get('display_title') or '')
        and type(run.get('id')) is int and run['id'] > 0
    )


def _readback(repository: str, workflow: str, control_run_id: str, created_at: str) -> list[dict]:
    endpoint = f'repos/{repository}/actions/workflows/{workflow}/runs'
    filters = urlencode({
        'branch': DISPATCH_REF,
        'event': 'workflow_dispatch',
        'created': f'>={created_at}',
        'per_page': 100,
    })
    matches = {}
    for page in range(1, 11):
        response = gh(f'{endpoint}?{filters}&page={page}')
        runs = response.get('workflow_runs') if isinstance(response, dict) else None
        if not isinstance(runs, list):
            raise ReleaseError('GitHub workflow history response has no workflow_runs array')
        for run in runs:
            if _matches(run, workflow, control_run_id):
                matches[run['id']] = run
        if len(matches) > 1 or len(runs) < 100 or response.get('total_count') == page * 100:
            return list(matches.values())
    raise ReleaseError('GitHub workflow history exceeded the filtered 1,000-run limit')


def _binding(status: str, run: dict) -> dict:
    return {'status': status, 'workflow_run_id': run['id'], 'run_url': run.get('html_url')}


def _dispatch_once(
    repository: str,
    workflow: str,
    control_run_id: str,
    *,
    attempt: int,
    created_at: str,
    scoped_token: bool,
    reconcile_seconds: int,
) -> dict:
    try:
        existing = _readback(repository, workflow, control_run_id, created_at)
    except API_FAILURES as exc:
        return {'status': 'unknown', 'reason': f'Owner run preflight failed: {exc}'}
    if len(existing) > 1:
        return {'status': 'unknown', 'reason': 'Multiple owner runs match the controller run'}
    if existing:
        return _binding('already_dispatched', existing[0])
    if attempt > 1:
        return {'status': 'unknown', 'reason': 'No matching owner run found on read-only controller rerun'}
    if not scoped_token:
        return {'status': 'dispatch_unavailable', 'reason': 'Scoped package-release GitHub App token unavailable'}

    dispatch_error = None
    reported_id = None
    try:
        response = gh(
            f'repos/{repository}/actions/workflows/{workflow}/dispatches',
            method='POST',
            payload={'ref': DISPATCH_REF, 'inputs': {DISPATCH_INPUT: control_run_id}},
            api_version=DISPATCH_API_VERSION,
            retry_auth=False,
        )
        if isinstance(response, dict) and type(response.get('workflow_run_id')) is int:
            reported_id = response['workflow_run_id']
    except API_FAILURES as exc:
        dispatch_error = str(exc)

    deadline = time.monotonic() + reconcile_seconds
    while True:
        try:
            if reported_id is not None:
                run = gh(f'repos/{repository}/actions/runs/{reported_id}')
                if isinstance(run, dict) and run.get('id') == reported_id and _matches(run, workflow, control_run_id):
                    return _binding('dispatch_submitted', run)
            else:
                matches = _readback(repository, workflow, control_run_id, created_at)
                if len(matches) > 1:
                    return {'status': 'unknown', 'reason': 'Multiple owner runs match the controller run'}
                if matches:
                    return _binding('dispatch_submitted', matches[0])
        except API_FAILURES as exc:
            dispatch_error = dispatch_error or str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                'status': 'unknown',
                'reason': 'Owner run could not be confirmed after dispatch',
                **({'api_error': dispatch_error} if dispatch_error else {}),
                **({'reported_run_id': reported_id} if reported_id is not None else {}),
            }
        time.sleep(min(5, remaining))


def dispatch_configured_owner(
    owner_root: Path,
    *,
    expected_repository: str,
    expected_subject_id: str,
    control_run_id: str,
    control_created_at: str,
    attempt: int,
    event: str,
    scoped_token: bool,
    reconcile_seconds: int = 60,
) -> dict:
    """Dispatch the declared lane, preserving a readback on controller reruns."""
    if not re.fullmatch(r'[1-9][0-9]*', control_run_id) or attempt < 1:
        raise ReleaseError('Owner dispatch requires a numeric GitHub run ID and attempt')
    cfg = load_release_config_from_dir(owner_root)
    if cfg['repository'] != expected_repository or cfg['subject_id'] != expected_subject_id:
        raise ReleaseError('Owner release config identity does not match the selected fleet subject')
    receipt = {
        'repository': cfg['repository'],
        'subject_id': cfg['subject_id'],
        'control_run_id': control_run_id,
        'control_run_attempt': attempt,
    }
    lane = next((lane for lane in cfg['lanes'] if lane.get('daily_dispatch')), None)
    if lane is None:
        return {**receipt, 'status': 'not_configured'}
    receipt.update(lane=lane['lane'], workflow=lane['workflow'], ref=DISPATCH_REF)
    if event != 'schedule':
        return {**receipt, 'status': 'not_scheduled'}
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', control_created_at):
        return {**receipt, 'status': 'dispatch_unavailable', 'reason': 'Original controller creation time unavailable'}
    result = _dispatch_once(
        cfg['repository'], lane['workflow'], control_run_id, attempt=attempt,
        created_at=control_created_at, scoped_token=scoped_token, reconcile_seconds=reconcile_seconds,
    )
    return {**receipt, **result}
