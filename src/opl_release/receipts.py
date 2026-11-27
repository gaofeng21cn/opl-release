"""Standard `opl.release-receipt.v1` emission, validation, and GitHub artifact readback."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import subprocess
from urllib.parse import urlencode
from zipfile import BadZipFile, ZipFile

from .common import ReleaseError, gh

VALID_STAGES = {
    'unchanged',
    'candidate_built',
    'qualification_pending',
    'qualification_failed',
    'published',
    'verified',
    'blocked',
}


def receipt_artifact_name(subject_id: str, lane: str, run_id: str | int) -> str:
    return f'opl-release-receipt-{subject_id}-{lane}-{run_id}'


def build_receipt(
    *,
    subject_id: str,
    lane: str,
    stage: str,
    candidate_version: str | None = None,
    candidate_tag: str | None = None,
    candidate_commit: str | None = None,
    published_version: str | None = None,
    published_tag: str | None = None,
    published_ref: str | None = None,
    published_digest: str | None = None,
    qualification_required: bool = False,
    qualification_status: str = 'not_required',
    qualification_evidence_url: str | None = None,
    run_url: str | None = None,
    blocker_code: str | None = None,
    blocker_message: str | None = None,
) -> dict:
    if stage not in VALID_STAGES:
        raise ReleaseError(f'Invalid release receipt stage: {stage!r}')
    receipt: dict = {
        'schema': 'opl.release-receipt.v1',
        'subject_id': subject_id,
        'lane': lane,
        'stage': stage,
        'recorded_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if any((candidate_version, candidate_tag, candidate_commit)):
        receipt['candidate'] = {
            k: v for k, v in (
                ('version', candidate_version),
                ('tag', candidate_tag),
                ('commit', candidate_commit),
            ) if v
        }
    if any((published_version, published_tag, published_ref, published_digest)):
        receipt['published'] = {
            k: v for k, v in (
                ('version', published_version),
                ('tag', published_tag),
                ('ref', published_ref),
                ('digest', published_digest),
            ) if v
        }
    if qualification_required or qualification_status != 'not_required' or qualification_evidence_url:
        qual: dict = {
            'required': bool(qualification_required),
            'status': qualification_status,
        }
        if qualification_evidence_url:
            qual['evidence_url'] = qualification_evidence_url
        receipt['qualification'] = qual
    if run_url:
        receipt['run_url'] = run_url
    if blocker_code and blocker_message:
        receipt['blocker'] = {'code': blocker_code, 'message': blocker_message}
    return receipt


def validate_receipt(receipt: dict, *, expected_subject_id: str | None = None, expected_lane: str | None = None) -> dict:
    if not isinstance(receipt, dict) or receipt.get('schema') != 'opl.release-receipt.v1':
        raise ReleaseError('Receipt must declare schema opl.release-receipt.v1')
    if receipt.get('stage') not in VALID_STAGES:
        raise ReleaseError('Receipt has invalid stage')
    if expected_subject_id and receipt.get('subject_id') != expected_subject_id:
        raise ReleaseError('Receipt subject_id mismatch')
    if expected_lane and receipt.get('lane') != expected_lane:
        raise ReleaseError('Receipt lane mismatch')
    return receipt


def fetch_run_receipt(repository: str, run: dict, subject_id: str, lane: str) -> dict | None:
    """Download and validate a standard `opl.release-receipt.v1` artifact if emitted by `run`."""
    name = receipt_artifact_name(subject_id, lane, run['id'])
    artifacts = gh(f"repos/{repository}/actions/runs/{run['id']}/artifacts?{urlencode({'name': name})}")['artifacts']
    matches = [row for row in artifacts if row['name'] == name and not row.get('expired')]
    if len(matches) != 1 or matches[0]['size_in_bytes'] > 1024 * 1024:
        return None
    try:
        data = download_json_artifact(repository, matches[0]['id'], 'release-receipt.json')
        return validate_receipt(data, expected_subject_id=subject_id, expected_lane=lane)
    except ReleaseError:
        return None


def download_json_artifact(repository: str, artifact_id: int, filename: str) -> dict:
    dl = subprocess.run(
        ['gh', 'api', f'repos/{repository}/actions/artifacts/{artifact_id}/zip'],
        capture_output=True,
        timeout=20,
    )
    if dl.returncode != 0:
        raise ReleaseError('Unable to download receipt artifact')
    try:
        with ZipFile(BytesIO(dl.stdout)) as zf:
            members = [entry for entry in zf.infolist() if entry.filename == filename]
            if len(members) != 1 or members[0].file_size > 1024 * 1024:
                raise ReleaseError('Receipt artifact has missing, duplicate, or oversized JSON')
            data = json.loads(zf.read(members[0]))
    except (BadZipFile, ValueError) as exc:
        raise ReleaseError('Invalid receipt artifact') from exc
    if not isinstance(data, dict):
        raise ReleaseError('Receipt JSON must be an object')
    return data
