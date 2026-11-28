"""Authenticated verification helper for centrally dispatched owner workflows."""
from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import urlencode

from .common import ReleaseError, gh, read_json

RELEASE_BOT_ID = 356454848
CONTROLLER_REPOSITORY = 'gaofeng21cn/opl-release'
CONTROLLER_WORKFLOW = '.github/workflows/control-daily-releases.yml'


def verify_central_dispatch(
    *,
    control_run_id: str,
    repository: str,
    workflow: str,
    expected_title: str,
    event_name: str,
    ref: str,
    run_attempt: str | int,
    actor_id: str | int,
    run_id: str | int | None = None,
    run_number: str | int | None = None,
    event_path: Path | None = None,
    controller_repository: str = CONTROLLER_REPOSITORY,
) -> dict:
    """Verify via authenticated GitHub API that this workflow was dispatched uniquely by the central schedule."""
    if not re.fullmatch(r'[1-9][0-9]*', str(control_run_id or '')):
        raise ReleaseError('control_run_id must be a positive decimal Actions run ID')
    if event_name != 'workflow_dispatch':
        raise ReleaseError('Central dispatch requires workflow_dispatch event')
    if ref != 'refs/heads/main':
        raise ReleaseError('Central dispatch requires refs/heads/main')
    if str(run_attempt) != '1':
        raise ReleaseError('Central dispatch requires the first workflow run attempt')
    if str(actor_id) != str(RELEASE_BOT_ID):
        raise ReleaseError('Central dispatch requires the OPL Package Publisher GitHub App actor')
    if not repository or '/' not in repository:
        raise ReleaseError('Expected owner/repo repository identity for dispatch verification')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', workflow or ''):
        raise ReleaseError('Invalid workflow filename for dispatch verification')
    if not expected_title:
        raise ReleaseError('expected_title is required for dispatch uniqueness verification')

    if event_path and event_path.is_file():
        sender_id = read_json(event_path).get('sender', {}).get('id')
        if sender_id is not None and str(sender_id) != str(RELEASE_BOT_ID):
            raise ReleaseError('Central dispatch event sender must match the OPL Package Publisher GitHub App')

    source = gh(f'repos/{controller_repository}/actions/runs/{control_run_id}')
    if (
        not isinstance(source, dict)
        or str(source.get('id')) != str(control_run_id)
        or (source.get('repository') or {}).get('full_name') != controller_repository
        or (source.get('head_repository') or {}).get('full_name') != controller_repository
        or source.get('path') != CONTROLLER_WORKFLOW
        or source.get('event') != 'schedule'
        or source.get('head_branch') != 'main'
        or source.get('run_attempt') != 1
        or not source.get('created_at')
    ):
        raise ReleaseError('Central dispatch source must be the first attempt of the central main schedule')

    created_at = source['created_at']
    current_num = int(run_number) if run_number is not None and str(run_number).isdecimal() else None
    current_id = int(run_id) if run_id is not None and str(run_id).isdecimal() else None
    for page in range(1, 11):
        query = urlencode({
            'branch': 'main',
            'event': 'workflow_dispatch',
            'created': f'>={created_at}',
            'per_page': 100,
            'page': page,
        })
        resp = gh(f'repos/{repository}/actions/workflows/{workflow}/runs?{query}')
        runs = resp.get('workflow_runs') if isinstance(resp, dict) else None
        if not isinstance(runs, list):
            raise ReleaseError('GitHub workflow runs response has no workflow_runs array')
        for candidate in runs:
            if (candidate.get('actor') or {}).get('id') != RELEASE_BOT_ID:
                continue
            if candidate.get('display_title') != expected_title:
                continue
            if current_num is not None and isinstance(candidate.get('run_number'), int):
                if candidate['run_number'] < current_num:
                    raise ReleaseError('This central schedule run ID has already dispatched this workflow')
            elif current_id is not None and isinstance(candidate.get('id'), int):
                if candidate['id'] < current_id:
                    raise ReleaseError('This central schedule run ID has already dispatched this workflow')
        total = resp.get('total_count')
        if len(runs) < 100 or (isinstance(total, int) and page * 100 >= total):
            break

    return {
        'status': 'verified',
        'control_run_id': str(control_run_id),
        'control_created_at': created_at,
        'repository': repository,
        'workflow': workflow,
    }


def verify_dispatch_from_env(
    environ: dict[str, str] | None = None,
    *,
    control_run_id: str,
    workflow: str,
    expected_title: str,
    repository: str | None = None,
    event_name: str | None = None,
    ref: str | None = None,
    run_attempt: str | None = None,
    actor_id: str | None = None,
    run_id: str | None = None,
    run_number: str | None = None,
    event_path: Path | None = None,
) -> dict:
    env = os.environ if environ is None else environ
    raw_event_path = event_path or (Path(env['GITHUB_EVENT_PATH']) if env.get('GITHUB_EVENT_PATH') else None)
    return verify_central_dispatch(
        control_run_id=control_run_id,
        repository=repository or env.get('GITHUB_REPOSITORY', ''),
        workflow=workflow,
        expected_title=expected_title,
        event_name=event_name or env.get('GITHUB_EVENT_NAME', ''),
        ref=ref or env.get('GITHUB_REF', ''),
        run_attempt=run_attempt or env.get('GITHUB_RUN_ATTEMPT', ''),
        actor_id=actor_id or env.get('GITHUB_ACTOR_ID', ''),
        run_id=run_id if run_id is not None else env.get('GITHUB_RUN_ID'),
        run_number=run_number if run_number is not None else env.get('GITHUB_RUN_NUMBER'),
        event_path=raw_event_path,
    )
