"""Unified `.opl-release.json` parser with transparent legacy normalization."""
from __future__ import annotations

import re
from pathlib import Path

from .common import ReleaseError, read_json, validate_pathspec

WORKFLOW_FILE_RE = re.compile(r'^[A-Za-z0-9_.-]+\.ya?ml$')
SUBJECT_ID_RE = re.compile(r'^[a-z0-9][a-z0-9.-]*$')
LANE_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
REPO_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
ALLOWED_CHANNEL_KINDS = {'oci', 'github_release', 'github_prerelease', 'sparkle_appcast'}


def _normalize_channel(raw_channel: dict | None, *, repository: str) -> dict | None:
    if not raw_channel:
        return None
    if not isinstance(raw_channel, dict):
        raise ReleaseError('Channel definition must be an object')
    kind = raw_channel.get('kind')
    if kind == 'framework_oci':
        oci_repo = raw_channel.get('repository') or (repository + '-framework')
        return {
            'kind': 'oci',
            'registry': raw_channel.get('registry', 'ghcr.io'),
            'repository': oci_repo,
            'tag': raw_channel.get('tag', 'latest-stable'),
        }
    if kind == 'sparkle':
        normalized = {'kind': 'sparkle_appcast'}
        for key in ('asset', 'release_tag'):
            if key in raw_channel:
                normalized[key] = raw_channel[key]
        return normalized
    if kind not in ALLOWED_CHANNEL_KINDS:
        raise ReleaseError(f'Unsupported channel kind: {kind!r}')
    return dict(raw_channel)


def normalize_release_config(raw: dict, *, fallback_subject_id: str | None = None, fallback_repository: str | None = None) -> dict:
    """Normalize either `opl.release.v1` or legacy `opl.product-release.v1` / `release-automation.json`."""
    if not isinstance(raw, dict):
        raise ReleaseError('Release config must be a JSON object')
    schema = raw.get('schema_version')
    if schema == 'opl.release.v1':
        return _validate_v1(raw)
    if schema == 'opl.product-release.v1':
        return _from_legacy_product_release(raw)
    if fallback_subject_id and fallback_repository:
        return _from_legacy_package_automation(raw, fallback_subject_id, fallback_repository)
    raise ReleaseError(f'Unsupported release schema version: {schema!r}')


def _validate_v1(cfg: dict) -> dict:
    subject_id = cfg.get('subject_id', '')
    repo = cfg.get('repository', '')
    if not SUBJECT_ID_RE.fullmatch(subject_id):
        raise ReleaseError(f'Invalid subject_id: {subject_id!r}')
    if not REPO_RE.fullmatch(repo):
        raise ReleaseError(f'Invalid repository: {repo!r}')
    if cfg.get('kind') not in ('product', 'package'):
        raise ReleaseError('kind must be product or package')
    if cfg.get('schedule') not in ('daily_patch', 'owner_schedule', 'manual'):
        raise ReleaseError('Invalid schedule')
    lanes = cfg.get('lanes')
    if not isinstance(lanes, list) or not lanes:
        raise ReleaseError('Release config must declare at least one lane')
    seen_lanes: set[str] = set()
    normalized_lanes: list[dict] = []
    for item in lanes:
        lane_id = item.get('lane', '')
        workflow = item.get('workflow', '')
        if not LANE_RE.fullmatch(lane_id) or lane_id in seen_lanes:
            raise ReleaseError(f'Invalid or duplicate lane: {lane_id!r}')
        if not WORKFLOW_FILE_RE.fullmatch(workflow):
            raise ReleaseError(f'Invalid lane workflow: {workflow!r}')
        seen_lanes.add(lane_id)
        entry = dict(item)
        if 'channel' in entry and entry['channel'] is not None:
            entry['channel'] = _normalize_channel(entry['channel'], repository=repo)
        normalized_lanes.append(entry)
    out = dict(cfg)
    out['lanes'] = normalized_lanes
    return out


def _from_legacy_product_release(raw: dict) -> dict:
    subject_id = raw.get('product_id', '')
    repo = raw.get('repository', '')
    strategy = raw.get('strategy')
    if not SUBJECT_ID_RE.fullmatch(subject_id) or not REPO_RE.fullmatch(repo):
        raise ReleaseError('Invalid legacy product-release.json identity')
    if strategy not in ('daily_patch', 'owner_schedule'):
        raise ReleaseError(f'Unsupported legacy product strategy: {strategy!r}')

    primary_lane_id = raw.get('primary_lane', 'stable')
    primary_lane: dict = {
        'lane': primary_lane_id,
        'workflow': raw['publisher_workflow'],
        'primary': True,
    }
    if raw.get('publisher_title_from_tag'):
        primary_lane['title_from_tag'] = raw['publisher_title_from_tag']
    if raw.get('publication_stage') == 'candidate_until_device_qualification':
        primary_lane['qualification_required'] = True
    if raw.get('recheck_after_success'):
        primary_lane['recheck_after_success'] = True
    if raw.get('channel'):
        primary_lane['channel'] = _normalize_channel(raw['channel'], repository=repo)

    lanes = [primary_lane]
    for obs in raw.get('observed_publishers', []):
        lane_entry: dict = {
            'lane': obs['lane'],
            'workflow': obs['workflow'],
            'primary': False,
        }
        for field in ('event', 'match', 'title_pattern', 'title_from_tag'):
            if field in obs:
                lane_entry[field] = obs[field]
        if obs.get('receipt'):
            lane_entry['receipt_required'] = True
        if obs.get('channel'):
            lane_entry['channel'] = _normalize_channel(obs['channel'], repository=repo)
        lanes.append(lane_entry)

    version_files = list(raw.get('version_files') or ([raw['version_file']] if raw.get('version_file') else []))
    normalized = {
        'schema_version': 'opl.release.v1',
        'subject_id': subject_id,
        'kind': 'product',
        'repository': repo,
        'schedule': strategy,
        'trigger': raw.get('trigger', 'tag' if strategy == 'daily_patch' else 'owner_schedule'),
        'tag_prefix': raw.get('tag_prefix', 'v'),
        'tag_suffix': raw.get('tag_suffix', ''),
        'delivery_paths': list(raw.get('release_paths', [])),
        'version_files': version_files,
        'source_check_workflows': list(raw.get('source_check_workflows', [])) if raw.get('source_check_workflows') is not None else [],
        'prepare': list(raw.get('prepare', [])),
        'verify': list(raw.get('verify', [])),
        'lanes': lanes,
    }
    return _validate_v1(normalized)


def _from_legacy_package_automation(raw: dict, subject_id: str, repository: str) -> dict:
    strategy = raw.get('strategy', 'daily_patch')
    schedule = 'manual' if strategy == 'manual' else 'daily_patch'
    owner_org = repository.split('/')[0]
    lanes: list[dict] = [
        {
            'lane': 'oci',
            'workflow': 'publish-package.yml',
            'primary': True,
            'channel': {
                'kind': 'oci',
                'registry': 'ghcr.io',
                'repository': f'{owner_org}/one-person-lab-packages/{subject_id}',
                'tag': 'latest-stable',
            },
        }
    ]
    for index, wf in enumerate(raw.get('release_workflows', []), start=1):
        lanes.append({'lane': f'native-{index}', 'workflow': wf, 'primary': False, 'match': 'tag'})
    normalized = {
        'schema_version': 'opl.release.v1',
        'subject_id': subject_id,
        'kind': 'package',
        'repository': repository,
        'schedule': schedule,
        'trigger': 'tag' if schedule == 'daily_patch' else 'manual',
        'tag_prefix': 'v',
        'tag_suffix': '',
        'delivery_paths': list(raw.get('release_paths', [])),
        'version_files': list(raw.get('version_files', [])),
        'prepare': list(raw.get('prepare', [])),
        'verify': list(raw.get('verify', [])),
        'lanes': lanes,
    }
    return _validate_v1(normalized)


def load_release_config_from_dir(root: Path, *, fallback_subject_id: str | None = None, fallback_repository: str | None = None) -> dict:
    """Load `.opl-release.json` (or legacy fallback) from a local checkout and validate paths."""
    if (root / '.opl-release.json').is_file():
        cfg = normalize_release_config(read_json(root / '.opl-release.json'))
    elif (root / 'product-release.json').is_file():
        cfg = normalize_release_config(read_json(root / 'product-release.json'))
    elif (root / 'release-automation.json').is_file():
        cfg = normalize_release_config(
            read_json(root / 'release-automation.json'),
            fallback_subject_id=fallback_subject_id,
            fallback_repository=fallback_repository,
        )
    else:
        raise ReleaseError(f'Missing .opl-release.json (or legacy release config) in {root}')
    for entry in cfg.get('delivery_paths', []):
        validate_pathspec(root, entry)
    return cfg


def validate_fleet_manifest(fleet: dict) -> dict:
    if not isinstance(fleet, dict) or fleet.get('schema_version') != 'opl.release-fleet.v1':
        raise ReleaseError('Fleet manifest must declare schema_version opl.release-fleet.v1')
    products = fleet.get('products')
    if not isinstance(products, list) or not products:
        raise ReleaseError('Fleet manifest must list at least one product')
    seen: set[str] = set()
    for row in products:
        sid = row.get('subject_id', '')
        repo = row.get('repository', '')
        if not SUBJECT_ID_RE.fullmatch(sid) or sid in seen:
            raise ReleaseError(f'Invalid or duplicate subject_id in fleet.json: {sid!r}')
        if not REPO_RE.fullmatch(repo):
            raise ReleaseError(f'Invalid repository in fleet.json: {repo!r}')
        seen.add(sid)
    return fleet
