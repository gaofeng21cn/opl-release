"""Pure read-only Fleet Release Observer with zero product-specific logic."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import urllib.request

from .common import ReleaseError, gh, read_json
from .config import normalize_release_config, validate_fleet_manifest
from .observe_dispatch import observe_dispatch
from .probes import probe_channel, probe_oci
from .receipts import fetch_run_receipt


def _fetch_repo_json(repository: str, rel_path: str, ref: str = 'main') -> dict | None:
    try:
        resp = gh(f'repos/{repository}/contents/{rel_path}?ref={ref}')
    except ReleaseError as exc:
        if 'HTTP 404' in str(exc):
            return None
        raise
    content = resp.get('content', '')
    if isinstance(content, str) and content.strip():
        return json.loads(base64.b64decode(content).decode('utf-8'))
    # GitHub Contents API omits inline base64 payloads for files larger than 1 MB.
    if sha := resp.get('sha'):
        blob = gh(f'repos/{repository}/git/blobs/{sha}')
        if isinstance(blob.get('content'), str) and blob['content'].strip():
            return json.loads(base64.b64decode(blob['content']).decode('utf-8'))
    if download_url := resp.get('download_url'):
        with urllib.request.urlopen(download_url, timeout=20) as raw_resp:
            return json.loads(raw_resp.read().decode('utf-8'))
    raise ReleaseError(f'Empty content payload for {repository}/{rel_path}')


def _fetch_product_config(entry: dict) -> dict:
    repo = entry['repository']
    sid = entry['subject_id']
    for name in ('.opl-release.json', 'product-release.json'):
        raw = _fetch_repo_json(repo, name)
        if raw is not None:
            return normalize_release_config(raw, fallback_subject_id=sid, fallback_repository=repo)
    raise ReleaseError(f'Missing .opl-release.json or product-release.json in {repo}')


def _select_lane_run(repository: str, lane: dict, primary_tag: str | None, primary_commit: str | None) -> dict | None:
    event = lane.get('event')
    query = f"per_page=40{'&event=' + event if event else ''}"
    runs = gh(f"repos/{repository}/actions/workflows/{lane['workflow']}/runs?{query}")['workflow_runs']
    if title_pat := lane.get('title_pattern'):
        runs = [r for r in runs if re.search(title_pat, r.get('display_title', ''))]
    match_mode = lane.get('match')
    if match_mode == 'tag' and primary_tag:
        runs = [
            r for r in runs
            if (r.get('head_branch') == primary_tag and (not primary_commit or r.get('head_sha') == primary_commit))
            or primary_tag in r.get('display_title', '')
        ]
    elif title_from_tag := lane.get('title_from_tag'):
        if primary_tag:
            expected_title = title_from_tag.replace('{tag}', primary_tag)
            runs = [r for r in runs if r.get('display_title') == expected_title]
    return max(runs, key=lambda r: r['id']) if runs else None


def _run_stage(run: dict, lane: dict) -> str:
    if run['status'] != 'completed':
        return f"publisher_{run['status']}"
    conclusion = run.get('conclusion') or 'unknown'
    if conclusion != 'success':
        return f'publisher_{conclusion}'
    if lane.get('qualification_required'):
        return 'qualification_pending'
    return 'publisher_success'


def _observe_lane(subject_id: str, repository: str, lane: dict, primary_tag: str | None, primary_commit: str | None) -> dict:
    row: dict = {
        'subject_id': subject_id,
        'lane': lane['lane'],
        'workflow': lane['workflow'],
        'stage': 'not_observed',
    }
    try:
        if channel := lane.get('channel'):
            channel_info = probe_channel(repository, channel)
            row['published_channel'] = channel_info
    except Exception as exc:
        row['channel_error'] = f'{type(exc).__name__}: {exc}'

    try:
        run = _select_lane_run(repository, lane, primary_tag, primary_commit)
        if run:
            row['run_id'] = run['id']
            row['run_url'] = run['html_url']
            row['run_commit'] = run['head_sha']
            receipt = fetch_run_receipt(repository, run, subject_id, lane['lane']) if run['status'] == 'completed' else None
            if receipt:
                row['receipt'] = receipt
                row['stage'] = receipt['stage']
            else:
                row['stage'] = _run_stage(run, lane)
                pub = row.get('published_channel') or {}
                if row['stage'] == 'publisher_success' and pub.get('status') == 'present':
                    if pub.get('source_commit') and pub['source_commit'] == run['head_sha']:
                        row['stage'] = 'verified'
    except Exception as exc:
        row['observation_error'] = f'{type(exc).__name__}: {exc}'
    return row


def _observe_product(entry: dict) -> dict:
    sid = entry['subject_id']
    repo = entry['repository']
    try:
        cfg = _fetch_product_config(entry)
    except Exception as exc:
        return {'subject_id': sid, 'repository': repo, 'status': 'unavailable', 'error': str(exc), 'lanes': []}

    primary = next((l for l in cfg['lanes'] if l.get('primary')), cfg['lanes'][0])
    primary_row = _observe_lane(sid, repo, primary, None, None)
    pub = primary_row.get('published_channel') or {}
    primary_tag = pub.get('tag')
    primary_commit = pub.get('source_commit')

    lanes = [primary_row]
    for lane in cfg['lanes']:
        if lane['lane'] == primary['lane']:
            continue
        lanes.append(_observe_lane(sid, repo, lane, primary_tag, primary_commit))

    stages = {l['stage'] for l in lanes}
    if any(l.get('observation_error') or l.get('channel_error') for l in lanes):
        overall = 'partial'
    elif stages & {'blocked', 'qualification_failed', 'publisher_failure', 'publisher_cancelled', 'publisher_timed_out'}:
        overall = 'attention_needed'
    elif 'qualification_pending' in stages:
        overall = 'qualification_pending'
    elif stages <= {'verified', 'unchanged', 'published'}:
        overall = 'healthy'
    else:
        overall = 'pending'

    return {'subject_id': sid, 'repository': repo, 'status': overall, 'lanes': lanes}


def _observe_catalog_packages(catalog_cfg: dict | None) -> dict:
    if not catalog_cfg:
        return {'status': 'not_configured', 'packages': []}
    try:
        index = _fetch_repo_json(catalog_cfg['repository'], catalog_cfg['index_path'], catalog_cfg.get('ref', 'main'))
    except Exception as exc:
        return {'status': 'unavailable', 'error': str(exc), 'packages': []}
    if not index:
        return {'status': 'unavailable', 'packages': []}

    registry = catalog_cfg['oci_registry']
    namespace = catalog_cfg['oci_namespace']
    admitted = [
        pkg for pkg in index.get('packages', [])
        if pkg.get('manifest', {}).get('publication_channel_admission', 'admitted') == 'admitted'
    ]

    def inspect_pkg(pkg: dict) -> dict:
        pid = pkg['package_id']
        visibility = pkg.get('visibility', 'public')
        manifest = pkg.get('manifest', {})
        v2 = manifest.get('v2_release') or {}
        declared_version = v2.get('version') or pkg.get('version')
        row: dict = {
            'package_id': pid,
            'visibility': visibility,
            'declared_version': declared_version,
            'format': 'v2' if v2 else 'legacy',
        }
        if visibility != 'public':
            row['status'] = 'restricted_owner_channel'
            return row
        try:
            probe = probe_oci({'registry': registry, 'repository': f'{namespace}/{pid}', 'tag': 'latest-stable'})
            row['oci'] = probe
            if probe.get('status') == 'present':
                row['status'] = 'verified' if probe.get('version') == declared_version else 'version_drift'
            else:
                row['status'] = probe.get('status', 'absent')
        except Exception as exc:
            row['status'] = 'probe_error'
            row['error'] = str(exc)
        return row

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(inspect_pkg, pkg): pkg['package_id'] for pkg in admitted}
        for fut in as_completed(futures):
            rows.append(fut.result())
    rows.sort(key=lambda r: r['package_id'])
    return {
        'status': 'ok',
        'total_admitted': len(rows),
        'public_verified': sum(1 for r in rows if r['status'] == 'verified'),
        'restricted': sum(1 for r in rows if r['status'] == 'restricted_owner_channel'),
        'drift_or_error': sum(1 for r in rows if r['status'] not in ('verified', 'restricted_owner_channel')),
        'packages': rows,
    }


def observe_fleet(fleet_path: Path, *, subject_filter: str | None = None) -> dict:
    fleet = validate_fleet_manifest(read_json(fleet_path))
    targets = [p for p in fleet['products'] if not subject_filter or p['subject_id'] == subject_filter]
    if subject_filter and not targets:
        raise ReleaseError(f'Unknown subject_id in fleet: {subject_filter}')

    products: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_observe_product, entry): entry['subject_id'] for entry in targets}
        for fut in as_completed(futures):
            products.append(fut.result())
    order = {entry['subject_id']: idx for idx, entry in enumerate(targets)}
    products.sort(key=lambda row: order[row['subject_id']])
    control_run, dispatch_rows = observe_dispatch(fleet.get('owner'), targets)
    for product in products:
        if dispatch := dispatch_rows.get(product['subject_id']):
            product['central_dispatch'] = dispatch

    packages = {'status': 'skipped', 'packages': []} if subject_filter else _observe_catalog_packages(fleet.get('catalog_source'))
    return {
        'schema_version': 'opl.fleet-release-report.v1',
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'products': products,
        'packages': packages,
        **({'dispatch_controller': control_run} if control_run else {}),
    }


def render_markdown(report: dict) -> str:
    lines = [
        '## OPL Fleet Release Observation',
        '',
        f"Generated at `{report['generated_at']}`",
        '',
        '| Subject | Lane | Stage | Public Version | Run |',
        '| --- | --- | --- | --- | --- |',
    ]
    for prod in report.get('products', []):
        if not prod.get('lanes'):
            lines.append(f"| `{prod['subject_id']}` | — | `{prod.get('status', 'unavailable')}` | — | — |")
            continue
        for lane in prod['lanes']:
            pub = lane.get('published_channel') or {}
            pub_ver = pub.get('version') or pub.get('tag') or '—'
            if pub.get('asset_present') is False:
                pub_ver += ' (asset missing)'
            run_link = f"[Actions]({lane['run_url']})" if lane.get('run_url') else '—'
            lines.append(f"| `{prod['subject_id']}` | `{lane['lane']}` | `{lane['stage']}` | `{pub_ver}` | {run_link} |")

    if controller := report.get('dispatch_controller'):
        lines.extend(['', '### Central Daily Dispatch', ''])
        if controller.get('run_url'):
            state = controller.get('conclusion') or controller['status']
            lines.extend([
                f"[Scheduled controller run]({controller['run_url']}) created at "
                f"`{controller['created_at']}` · attempt `{controller['run_attempt']}` · `{state}`", '',
            ])
        lines.extend([
            '| Subject | Dispatch status | Receipt attempt | Owner run |',
            '| --- | --- | --- | --- |',
        ])
        for product in report['products']:
            dispatch = product.get('central_dispatch')
            if dispatch:
                owner_run = f"[Actions]({dispatch['run_url']})" if dispatch.get('run_url') else '—'
                receipt_attempt = dispatch.get('control_run_attempt', '—')
                lines.append(
                    f"| `{product['subject_id']}` | `{dispatch['status']}` | {receipt_attempt} | {owner_run} |"
                )

    pkgs = report.get('packages') or {}
    if pkgs.get('status') == 'ok':
        lines.extend([
            '',
            f"### Catalog Packages (`{pkgs['total_admitted']}` admitted · `{pkgs['public_verified']}` public verified · `{pkgs['restricted']}` restricted · `{pkgs['drift_or_error']}` drift/error)",
            '',
            '| Package | Visibility | Format | Declared | OCI Channel | Status |',
            '| --- | --- | --- | --- | --- | --- |',
        ])
        for pkg in pkgs.get('packages', []):
            oci_ver = (pkg.get('oci') or {}).get('version') or '—'
            lines.append(f"| `{pkg['package_id']}` | `{pkg['visibility']}` | `{pkg['format']}` | `{pkg['declared_version']}` | `{oci_ver}` | `{pkg['status']}` |")
    return '\n'.join(lines) + '\n'
