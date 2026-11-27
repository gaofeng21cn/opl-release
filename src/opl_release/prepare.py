"""Unified release candidate planning, tag-freeze, and failed-run resumption."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time

from .common import ReleaseError, gh, git, run, version_tuple
from .config import load_release_config_from_dir
from .probes import probe_channel
from .versioning import read_declared_version, update_version_files


def tag_for(cfg: dict, version: str) -> str:
    return cfg.get('tag_prefix', 'v') + version + cfg.get('tag_suffix', '')


def delivery_changed(root: Path, base_commit: str, delivery_paths: list[str]) -> bool:
    if not delivery_paths:
        return False
    res = run(['git', 'diff', '--quiet', base_commit, 'HEAD', '--', *delivery_paths], cwd=root, check=False)
    if res.returncode not in (0, 1):
        raise ReleaseError('Cannot compare delivery paths against published commit')
    return res.returncode == 1


def plan_candidate(root: Path, cfg: dict, published: dict | None) -> dict:
    """Compute release state (`unchanged`, `needs_release`, `resume`) without mutating Git."""
    head = git(root, 'rev-parse', 'HEAD')
    current = read_declared_version(root, cfg.get('version_files', []))
    if published and published.get('source_commit'):
        pub_version = published.get('version') or '0.0.0'
        if published.get('kind') == 'github_release':
            pub_version = pub_version.removesuffix(cfg.get('tag_suffix', ''))
        version_tuple(pub_version)
        if run(['git', 'merge-base', '--is-ancestor', published['source_commit'], head], cwd=root, check=False).returncode:
            raise ReleaseError('Published source commit is not an ancestor of main')
        if not delivery_changed(root, published['source_commit'], cfg.get('delivery_paths', [])):
            return {'status': 'unchanged', 'version': pub_version, 'source_commit': published['source_commit']}
        if version_tuple(current) < version_tuple(pub_version):
            raise ReleaseError('Declared version regresses the published channel')
        if current == pub_version:
            a, b, c = version_tuple(current)
            current = f'{a}.{b}.{c + 1}'
    while True:
        tag = tag_for(cfg, current)
        existing = run(['git', 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}'], cwd=root, check=False)
        if existing.returncode:
            return {'status': 'needs_release', 'version': current, 'tag': tag, 'source_commit': head}
        frozen = existing.stdout.strip()
        if not run(['git', 'merge-base', '--is-ancestor', frozen, head], cwd=root, check=False).returncode:
            if not delivery_changed(root, frozen, cfg.get('delivery_paths', [])):
                return {'status': 'resume', 'version': current, 'tag': tag, 'source_commit': frozen}
        a, b, c = version_tuple(current)
        current = f'{a}.{b}.{c + 1}'


def wait_for_source_checks(repo: str, commit: str, *, source_workflows: list[str] | None = None, ignored_check_suite_id: int | None = None, timeout: int = 1200) -> None:
    deadline = time.monotonic() + timeout
    first = True
    while True:
        if source_workflows:
            checks = []
            for wf in source_workflows:
                runs = gh(f'repos/{repo}/actions/workflows/{wf}/runs?head_sha={commit}&per_page=100')['workflow_runs']
                matches = [
                    r for r in runs
                    if r.get('head_sha') == commit and r.get('path') == f'.github/workflows/{wf}'
                    and (ignored_check_suite_id is None or r.get('check_suite_id') != ignored_check_suite_id)
                ]
                if matches:
                    latest = max(matches, key=lambda r: r['id'])
                    checks.append({'name': wf, 'status': latest['status'], 'conclusion': latest.get('conclusion')})
        else:
            res = gh(f'repos/{repo}/commits/{commit}/check-runs?per_page=100')
            checks = [
                c for c in res['check_runs']
                if ignored_check_suite_id is None or c.get('check_suite', {}).get('id') != ignored_check_suite_id
            ]
        statuses = gh(f'repos/{repo}/commits/{commit}/status')['statuses']
        rejected = [s['context'] for s in statuses if s['state'] in ('error', 'failure')]
        if rejected:
            raise ReleaseError('Source statuses failed: ' + ', '.join(rejected))
        failed = [c['name'] for c in checks if c['status'] == 'completed' and c['conclusion'] not in ('success', 'neutral', 'skipped')]
        if failed:
            raise ReleaseError('Source checks failed: ' + ', '.join(failed))
        pending = any(c['status'] != 'completed' for c in checks) or any(s['state'] == 'pending' for s in statuses)
        if not pending and (checks or (not source_workflows and not first)):
            return
        if time.monotonic() >= deadline:
            raise ReleaseError('Source checks timed out')
        first = False
        time.sleep(10)


def merge_release_pr(root: Path, repo: str, base: str, subject_id: str, version: str) -> str:
    branch = f'release/prepare-{subject_id}-{base[:12]}'
    existing = git(root, 'ls-remote', 'origin', f'refs/heads/{branch}')
    if existing:
        git(root, 'fetch', 'origin', f'refs/heads/{branch}', '--quiet')
        selected = git(root, 'rev-parse', 'FETCH_HEAD')
        if git(root, 'rev-parse', f'{selected}^{{tree}}') != git(root, 'rev-parse', 'HEAD^{tree}'):
            raise ReleaseError('Existing release preparation branch differs; preserve for review')
        git(root, 'reset', '--hard', selected)
    git(root, 'push', 'origin', f'HEAD:refs/heads/{branch}')
    owner = repo.split('/')[0]
    pulls = gh(f'repos/{repo}/pulls?state=open&head={owner}:{branch}&base=main')
    pull = pulls[0] if pulls else gh(
        f'repos/{repo}/pulls',
        method='POST',
        payload={
            'title': f'chore: prepare {subject_id} {version}',
            'head': branch,
            'base': 'main',
            'body': f'Align {subject_id} version files to {version} for release.',
        },
    )
    head = git(root, 'rev-parse', 'HEAD')
    wait_for_source_checks(repo, head)
    if gh(f'repos/{repo}/commits/main')['sha'] != base:
        raise ReleaseError('Target main moved during release preparation')
    result = gh(f'repos/{repo}/pulls/{pull["number"]}/merge', method='PUT', payload={'sha': head, 'merge_method': 'merge'})
    git(root, 'fetch', 'origin', 'main', '--quiet')
    merged = result['sha'] if result and result.get('merged') and re.fullmatch(r'[a-f0-9]{40}', str(result.get('sha'))) else git(root, 'rev-parse', 'origin/main')
    if git(root, 'rev-parse', f'{merged}^{{tree}}') != git(root, 'rev-parse', f'{head}^{{tree}}'):
        raise ReleaseError('Merged tree differs from verified preparation tree')
    git(root, 'reset', '--hard', merged)
    return merged


def prepare_repository(root: Path, *, apply: bool = False, fallback_subject_id: str | None = None, fallback_repository: str | None = None) -> dict:
    cfg = load_release_config_from_dir(root, fallback_subject_id=fallback_subject_id, fallback_repository=fallback_repository)
    if cfg['schedule'] != 'daily_patch':
        return {'subject_id': cfg['subject_id'], 'repository': cfg['repository'], 'status': cfg['schedule']}
    if git(root, 'status', '--porcelain'):
        raise ReleaseError('Owner checkout must be clean before preparing a release')
    git(root, 'fetch', 'origin', 'main', '--tags', '--quiet')
    base = git(root, 'rev-parse', 'HEAD')
    if base != git(root, 'rev-parse', 'origin/main'):
        raise ReleaseError('Release preparation requires fresh origin/main')

    primary_lane = next((l for l in cfg['lanes'] if l.get('primary')), cfg['lanes'][0])
    channel = primary_lane.get('channel')
    pub = probe_channel(cfg['repository'], channel) if channel else None
    if pub and pub.get('status') != 'present':
        pub = None
    planned = plan_candidate(root, cfg, pub)
    if planned['status'] == 'unchanged' or not apply:
        return {'subject_id': cfg['subject_id'], 'repository': cfg['repository'], **planned, 'published_channel': pub}

    repo = cfg['repository']
    if planned['status'] == 'resume':
        runs = gh(f"repos/{repo}/actions/workflows/{primary_lane['workflow']}/runs?head_sha={planned['source_commit']}&per_page=100")['workflow_runs']
        matched = [r for r in runs if r['head_sha'] == planned['source_commit'] and r['head_branch'] == planned['tag']]
        if matched:
            latest = max(matched, key=lambda r: r['id'])
            if latest['status'] == 'completed' and latest.get('conclusion') in ('failure', 'cancelled', 'timed_out'):
                gh(f"repos/{repo}/actions/runs/{latest['id']}/rerun-failed-jobs", method='POST')
                return {'subject_id': cfg['subject_id'], 'repository': repo, **planned, 'status': 'publisher_retry_submitted', 'run_url': latest['html_url']}
            if latest['status'] == 'completed' and latest.get('conclusion') == 'success' and primary_lane.get('recheck_after_success'):
                gh(f"repos/{repo}/actions/runs/{latest['id']}/rerun", method='POST')
                return {'subject_id': cfg['subject_id'], 'repository': repo, **planned, 'status': 'qualification_recheck_submitted', 'run_url': latest['html_url']}
        return {'subject_id': cfg['subject_id'], 'repository': repo, **planned}

    wait_for_source_checks(repo, base, source_workflows=cfg.get('source_check_workflows') or None)
    git(root, 'config', 'user.name', 'OPL Release Bot')
    git(root, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    update_version_files(root, cfg.get('version_files', []), planned['version'])
    os.environ['OPL_RELEASE_VERSION'] = planned['version']
    for stage in ('prepare', 'verify'):
        for cmd in cfg.get(stage, []):
            run(cmd, cwd=root)
    source = base
    if git(root, 'status', '--porcelain'):
        git(root, 'add', '--all')
        git(root, 'commit', '-m', f"chore: prepare {cfg['subject_id']} {planned['version']}")
        source = merge_release_pr(root, repo, base, cfg['subject_id'], planned['version'])
    git(root, 'tag', '-a', planned['tag'], '-m', f"{cfg['subject_id']} {planned['version']}", source)
    git(root, 'push', 'origin', f"refs/tags/{planned['tag']}")
    return {'subject_id': cfg['subject_id'], 'repository': repo, 'status': 'tag_submitted', 'version': planned['version'], 'tag': planned['tag'], 'source_commit': source}
