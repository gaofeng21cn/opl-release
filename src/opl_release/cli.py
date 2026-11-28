"""Command-line interface for opl-release."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .common import ReleaseError, configure_token_refresh_from_env, read_json, write_json
from .config import load_release_config_from_dir, validate_fleet_manifest
from .dispatch import dispatch_configured_owner
from .observe import observe_fleet, render_markdown
from .prepare import prepare_repository
from .probes import probe_channel
from .receipts import build_receipt
from .verify_dispatch import verify_dispatch_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='opl-release', description='OPL horizontal release toolchain and read-only observer')
    sub = parser.add_subparsers(dest='command', required=True)

    val = sub.add_parser('validate', help='Validate fleet.json and/or an owner release config')
    val.add_argument('--fleet', type=Path)
    val.add_argument('--owner-root', type=Path)

    emit = sub.add_parser('emit-receipt', help='Generate a standard opl.release-receipt.v1 JSON file')
    emit.add_argument('--subject-id', required=True)
    emit.add_argument('--lane', required=True)
    emit.add_argument('--stage', required=True)
    emit.add_argument('--version')
    emit.add_argument('--tag')
    emit.add_argument('--commit')
    emit.add_argument('--published-version')
    emit.add_argument('--published-tag')
    emit.add_argument('--published-ref')
    emit.add_argument('--published-digest')
    emit.add_argument('--qualification-required', action='store_true')
    emit.add_argument('--qualification-status', default='not_required')
    emit.add_argument('--qualification-evidence-url')
    emit.add_argument('--run-url')
    emit.add_argument('--blocker-code')
    emit.add_argument('--blocker-message')
    emit.add_argument('--output', type=Path, required=True)

    prb = sub.add_parser('probe', help='Probe a publication channel by protocol')
    prb.add_argument('--repository', required=True)
    prb.add_argument('--kind', required=True, choices=['oci', 'github_release', 'github_prerelease', 'sparkle_appcast'])
    prb.add_argument('--oci-repository')
    prb.add_argument('--tag', default='latest-stable')
    prb.add_argument('--tag-pattern', default='.*')
    prb.add_argument('--asset', default='appcast.xml')
    prb.add_argument('--release-tag', default='macos-stable')

    prep = sub.add_parser('prepare', help='Plan or apply candidate release preparation on a local owner checkout')
    prep.add_argument('--owner-root', type=Path, required=True)
    prep.add_argument('--apply', action='store_true')
    prep.add_argument('--output', type=Path)

    dispatch = sub.add_parser('dispatch-owner', help='Dispatch one explicitly admitted owner-scheduled workflow')
    dispatch.add_argument('--owner-root', type=Path, required=True)
    dispatch.add_argument('--repository', required=True)
    dispatch.add_argument('--subject-id', required=True)
    dispatch.add_argument('--output', type=Path, required=True)

    obs = sub.add_parser('observe', help='Read-only fleet observation across products and opl-catalog')
    obs.add_argument('--fleet', type=Path, required=True)
    obs.add_argument('--subject-id', default='')
    obs.add_argument('--output', type=Path, required=True)
    obs.add_argument('--markdown-output', type=Path)

    vdisp = sub.add_parser('verify-dispatch', help='Verify a centrally dispatched owner workflow run')
    vdisp.add_argument('--control-run-id', required=True)
    vdisp.add_argument('--workflow', required=True)
    vdisp.add_argument('--expected-title', required=True)
    vdisp.add_argument('--repository')
    vdisp.add_argument('--event-name')
    vdisp.add_argument('--ref')
    vdisp.add_argument('--run-attempt')
    vdisp.add_argument('--actor-id')
    vdisp.add_argument('--run-id')
    vdisp.add_argument('--run-number')
    vdisp.add_argument('--event-path', type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'validate':
            if not args.fleet and not args.owner_root:
                raise ReleaseError('Specify --fleet and/or --owner-root')
            if args.fleet:
                validate_fleet_manifest(read_json(args.fleet))
            if args.owner_root:
                load_release_config_from_dir(args.owner_root)
            print('Validation passed.')
            return 0

        if args.command == 'emit-receipt':
            receipt = build_receipt(
                subject_id=args.subject_id,
                lane=args.lane,
                stage=args.stage,
                candidate_version=args.version,
                candidate_tag=args.tag,
                candidate_commit=args.commit,
                published_version=args.published_version,
                published_tag=args.published_tag,
                published_ref=args.published_ref,
                published_digest=args.published_digest,
                qualification_required=args.qualification_required,
                qualification_status=args.qualification_status,
                qualification_evidence_url=args.qualification_evidence_url,
                run_url=args.run_url,
                blocker_code=args.blocker_code,
                blocker_message=args.blocker_message,
            )
            write_json(args.output, receipt)
            return 0

        if args.command == 'probe':
            channel: dict = {'kind': args.kind}
            if args.kind == 'oci':
                channel.update({'registry': 'ghcr.io', 'repository': args.oci_repository or args.repository, 'tag': args.tag})
            elif args.kind == 'github_prerelease':
                channel['tag_pattern'] = args.tag_pattern
            elif args.kind == 'sparkle_appcast':
                channel.update({'asset': args.asset, 'release_tag': args.release_tag})
            print(json.dumps(probe_channel(args.repository, channel), indent=2))
            return 0

        if args.command == 'prepare':
            configure_token_refresh_from_env()
            result = prepare_repository(args.owner_root, apply=args.apply)
            if args.output:
                write_json(args.output, result)
            print(json.dumps(result, indent=2))
            return 0

        if args.command == 'dispatch-owner':
            run_attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
            if not run_attempt.isascii() or not run_attempt.isdecimal():
                raise ReleaseError('Owner dispatch requires GITHUB_RUN_ATTEMPT')
            result = dispatch_configured_owner(
                args.owner_root,
                expected_repository=args.repository,
                expected_subject_id=args.subject_id,
                control_run_id=os.environ.get('GITHUB_RUN_ID', ''),
                control_created_at=os.environ.get('CONTROL_CREATED_AT', ''),
                attempt=int(run_attempt),
                event=os.environ.get('GITHUB_EVENT_NAME', ''),
                scoped_token=os.environ.get('OWNER_DISPATCH_SCOPED_TOKEN') == 'true',
            )
            write_json(args.output, result)
            print(json.dumps(result, indent=2))
            return int(result['status'] in ('unknown', 'dispatch_unavailable'))

        if args.command == 'observe':
            report = observe_fleet(args.fleet, subject_filter=args.subject_id or None)
            write_json(args.output, report)
            md = render_markdown(report)
            if args.markdown_output:
                args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
                args.markdown_output.write_text(md, encoding='utf-8')
            if summary := os.getenv('GITHUB_STEP_SUMMARY'):
                with open(summary, 'a', encoding='utf-8') as fh:
                    fh.write(md)
            print(md)
            return 0

        if args.command == 'verify-dispatch':
            result = verify_dispatch_from_env(
                control_run_id=args.control_run_id,
                workflow=args.workflow,
                expected_title=args.expected_title,
                repository=args.repository,
                event_name=args.event_name,
                ref=args.ref,
                run_attempt=args.run_attempt,
                actor_id=args.actor_id,
                run_id=args.run_id,
                run_number=args.run_number,
                event_path=args.event_path,
            )
            print(json.dumps(result, indent=2))
            return 0
    except ReleaseError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0
