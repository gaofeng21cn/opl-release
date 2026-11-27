"""Read the scheduled controller's per-subject dispatch receipts."""
from __future__ import annotations

from datetime import datetime
import re
from subprocess import TimeoutExpired

from .common import ReleaseError, gh
from .receipts import download_json_artifact

WORKFLOW = 'control-daily-releases.yml'
RECEIPT_STATUSES = {
    'dispatch_submitted', 'already_dispatched', 'dispatch_unavailable', 'unknown', 'not_configured',
}
READ_ERRORS = (ReleaseError, TimeoutExpired, ValueError, OSError, KeyError, TypeError)


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _latest_schedule(repository: str) -> dict | None:
    data = gh(f'repos/{repository}/actions/workflows/{WORKFLOW}/runs?branch=main&event=schedule&per_page=20')
    runs = data['workflow_runs']
    if not isinstance(runs, list):
        raise ReleaseError('Invalid controller workflow history')
    for run in runs:
        if (
            not _positive_id(run['id']) or not _positive_id(run['run_attempt'])
            or run['path'] != f'.github/workflows/{WORKFLOW}'
            or run['event'] != 'schedule' or run['head_branch'] != 'main'
            or run['repository']['full_name'] != repository
        ):
            raise ReleaseError('Scheduled controller run identity mismatch')
    return max(runs, key=lambda run: (datetime.fromisoformat(run['created_at']), run['id']), default=None)


def _run_rows(endpoint: str, key: str) -> list[dict]:
    rows = []
    page = 1
    separator = '&' if '?' in endpoint else '?'
    while True:
        data = gh(f'{endpoint}{separator}per_page=100&page={page}')
        items = data[key]
        if not isinstance(items, list):
            raise ReleaseError(f'Invalid GitHub {key} listing')
        rows.extend(items)
        if len(items) < 100 or len(rows) >= data['total_count']:
            return rows
        page += 1


def _receipt(repository: str, run: dict, target: dict, attempt: int, artifacts: list[dict]) -> dict | None:
    name = f"opl-release-dispatch-{target['subject_id']}-{run['id']}-{attempt}"
    matches = [artifact for artifact in artifacts if artifact['name'] == name]
    if not matches:
        return None
    if len(matches) != 1:
        raise ReleaseError('Duplicate dispatch receipt artifacts')
    artifact = matches[0]
    binding = artifact['workflow_run']
    if (
        binding['id'] != run['id'] or binding['repository_id'] != run['repository']['id']
        or binding['head_sha'] != run['head_sha'] or binding['head_branch'] != 'main'
        or artifact['expired'] or not _positive_id(artifact['id'])
        or not 0 < artifact['size_in_bytes'] <= 1024 * 1024
    ):
        raise ReleaseError('Dispatch receipt artifact expired or identity mismatch')
    data = download_json_artifact(repository, artifact['id'], 'owner-dispatch-result.json')
    if (
        data.get('repository') != target['repository'] or data.get('subject_id') != target['subject_id']
        or data.get('control_run_id') != str(run['id']) or type(data.get('control_run_attempt')) is not int
        or data['control_run_attempt'] != attempt or data.get('status') not in RECEIPT_STATUSES
    ):
        raise ReleaseError('Dispatch receipt identity or status mismatch')
    owner_run_id = data.get('workflow_run_id')
    if data['status'] in {'dispatch_submitted', 'already_dispatched'} and not _positive_id(owner_run_id):
        raise ReleaseError('Dispatch receipt has no valid owner run ID')
    result = {**data, 'receipt_artifact_id': artifact['id']}
    result.pop('run_url', None)
    if _positive_id(owner_run_id):
        result['run_url'] = f"https://github.com/{target['repository']}/actions/runs/{owner_run_id}"
    return result


def observe_dispatch(owner: str | None, targets: list[dict]) -> tuple[dict | None, dict[str, dict]]:
    enabled = [target for target in targets if target.get('enable_owner_dispatch') is True and not target.get('observe_only')]
    if not enabled:
        return None, {}
    try:
        if not owner or not re.fullmatch(r'[A-Za-z0-9_.-]+', owner):
            raise ReleaseError('Fleet owner is required to observe the controller')
        repository = f'{owner}/opl-release'
        run = _latest_schedule(repository)
        if not run:
            return {'status': 'not_observed'}, {target['subject_id']: {'status': 'not_observed'} for target in enabled}
        run_id, attempt = run['id'], run['run_attempt']
        controller = {key: run[key] for key in ('id', 'run_attempt', 'created_at', 'status', 'conclusion')}
    except READ_ERRORS as exc:
        return {'status': 'unknown', 'error': str(exc)}, {target['subject_id']: {'status': 'unknown'} for target in enabled}

    controller['run_url'] = f'https://github.com/{repository}/actions/runs/{run_id}'
    try:
        artifacts = _run_rows(f'repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
        jobs = _run_rows(f'repos/{repository}/actions/runs/{run_id}/jobs?filter=all', 'jobs')
        dispatch_jobs = {}
        for job in jobs:
            job_attempt = job['run_attempt']
            if (
                job['run_id'] != run_id or job['head_sha'] != run['head_sha']
                or not _positive_id(job_attempt) or job_attempt > attempt
            ):
                raise ReleaseError('Controller job identity mismatch')
            if job['name'].startswith('Dispatch '):
                key = (job['name'], job_attempt)
                if key in dispatch_jobs:
                    raise ReleaseError('Duplicate controller dispatch jobs')
                dispatch_jobs[key] = job
    except READ_ERRORS as exc:
        return {**controller, 'error': str(exc)}, {target['subject_id']: {'status': 'unknown'} for target in enabled}

    rows = {}
    for target in enabled:
        subject = target['subject_id']
        job_name = f'Dispatch {subject}'
        job = dispatch_jobs.get((job_name, attempt))
        try:
            receipt_attempt = attempt
            if not job and run['status'] == 'completed':
                receipt_attempt = max(
                    (job_attempt for name, job_attempt in dispatch_jobs if name == job_name),
                    default=attempt,
                )
                job = dispatch_jobs.get((job_name, receipt_attempt))
            row = _receipt(repository, run, target, receipt_attempt, artifacts)
            terminal = run['status'] == 'completed' or (job is not None and job['status'] == 'completed')
            rows[subject] = row or {'status': 'missing' if terminal else 'pending'}
        except READ_ERRORS as exc:
            rows[subject] = {'status': 'unknown', 'error': str(exc)}
    return controller, rows
