from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opl_release import cli
from opl_release.common import ReleaseError
from opl_release.verify_dispatch import RELEASE_BOT_ID, verify_central_dispatch, verify_dispatch_from_env


class VerifyDispatchTests(unittest.TestCase):
    def setUp(self):
        self.base_kwargs = {
            'control_run_id': '4200',
            'repository': 'example/widget',
            'workflow': 'nightly.yml',
            'expected_title': 'Widget Nightly [opl-release:4200]',
            'event_name': 'workflow_dispatch',
            'ref': 'refs/heads/main',
            'run_attempt': '1',
            'actor_id': str(RELEASE_BOT_ID),
            'run_id': '9001',
            'run_number': '15',
        }
        self.control_run = {
            'id': 4200,
            'repository': {'full_name': 'gaofeng21cn/opl-release'},
            'head_repository': {'full_name': 'gaofeng21cn/opl-release'},
            'path': '.github/workflows/control-daily-releases.yml',
            'event': 'schedule',
            'head_branch': 'main',
            'run_attempt': 1,
            'created_at': '2026-11-28T01:39:31Z',
        }

    def test_accepts_unique_valid_central_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / 'event.json'
            ev.write_text(json.dumps({'sender': {'id': RELEASE_BOT_ID}}), encoding='utf-8')

            def fake_gh(endpoint, **kwargs):
                if endpoint == 'repos/gaofeng21cn/opl-release/actions/runs/4200':
                    return self.control_run
                if endpoint.startswith('repos/example/widget/actions/workflows/nightly.yml/runs?'):
                    return {
                        'total_count': 2,
                        'workflow_runs': [
                            {'id': 9001, 'run_number': 15, 'actor': {'id': RELEASE_BOT_ID}, 'display_title': 'Widget Nightly [opl-release:4200]'},
                            {'id': 8990, 'run_number': 14, 'actor': {'id': 999999}, 'display_title': 'Widget Nightly [opl-release:4200]'},
                        ],
                    }
                raise AssertionError(endpoint)

            with patch('opl_release.verify_dispatch.gh', side_effect=fake_gh):
                res = verify_central_dispatch(**self.base_kwargs, event_path=ev)
        self.assertEqual(res['status'], 'verified')
        self.assertEqual(res['control_run_id'], '4200')

    def test_rejects_bad_actor_sender_or_duplicate_dispatch(self):
        with self.assertRaisesRegex(ReleaseError, 'GitHub App actor'):
            verify_central_dispatch(**{**self.base_kwargs, 'actor_id': '12345'})

        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / 'event.json'
            ev.write_text(json.dumps({'sender': {'id': 777}}), encoding='utf-8')
            with self.assertRaisesRegex(ReleaseError, 'event sender'):
                verify_central_dispatch(**self.base_kwargs, event_path=ev)

        def duplicate_gh(endpoint, **kwargs):
            if endpoint.endswith('/runs/4200'):
                return self.control_run
            return {
                'total_count': 2,
                'workflow_runs': [
                    {'id': 9000, 'run_number': 14, 'actor': {'id': RELEASE_BOT_ID}, 'display_title': 'Widget Nightly [opl-release:4200]'},
                    {'id': 9001, 'run_number': 15, 'actor': {'id': RELEASE_BOT_ID}, 'display_title': 'Widget Nightly [opl-release:4200]'},
                ],
            }

        with patch('opl_release.verify_dispatch.gh', side_effect=duplicate_gh):
            with self.assertRaisesRegex(ReleaseError, 'already dispatched'):
                verify_central_dispatch(**self.base_kwargs)

    def test_cli_verify_dispatch_reads_github_env_and_exits_nonzero_on_bad_source(self):
        env = {
            'GITHUB_REPOSITORY': 'example/widget',
            'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_REF': 'refs/heads/main',
            'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_ACTOR_ID': str(RELEASE_BOT_ID),
            'GITHUB_RUN_ID': '9001',
            'GITHUB_RUN_NUMBER': '15',
        }
        with patch.dict('os.environ', env, clear=False), \
             patch('opl_release.verify_dispatch.gh', side_effect=[self.control_run, {'total_count': 1, 'workflow_runs': []}]):
            code = cli.main([
                'verify-dispatch',
                '--control-run-id', '4200',
                '--workflow', 'nightly.yml',
                '--expected-title', 'Widget Nightly [opl-release:4200]',
            ])
        self.assertEqual(code, 0)

        bad_source = {**self.control_run, 'event': 'workflow_dispatch'}
        with patch.dict('os.environ', env, clear=False), \
             patch('opl_release.verify_dispatch.gh', return_value=bad_source):
            code = cli.main([
                'verify-dispatch',
                '--control-run-id', '4200',
                '--workflow', 'nightly.yml',
                '--expected-title', 'Widget Nightly [opl-release:4200]',
            ])
        self.assertEqual(code, 1)


if __name__ == '__main__':
    unittest.main()
