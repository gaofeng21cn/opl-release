from __future__ import annotations

import json
import os
from pathlib import Path
from subprocess import TimeoutExpired
import tempfile
import unittest
from unittest.mock import patch

from opl_release.cli import main
from opl_release.dispatch import dispatch_configured_owner


class DispatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = {
            'schema_version': 'opl.product-release.v1',
            'product_id': 'example-product',
            'repository': 'example/example-product',
            'strategy': 'daily_patch',
            'publisher_workflow': 'publish-core.yml',
            'observed_publishers': [
                {'lane': 'package', 'workflow': 'detect-package.yml', 'daily_dispatch': True},
            ],
        }
        self.config_path = self.root / 'product-release.json'
        self.write_config()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')

    def dispatch(self, *, attempt=1, event='schedule', scoped_token=True):
        return dispatch_configured_owner(
            self.root,
            expected_repository='example/example-product',
            expected_subject_id='example-product',
            control_run_id='17',
            control_created_at='2026-11-28T00:00:00Z',
            attempt=attempt,
            event=event,
            scoped_token=scoped_token,
            reconcile_seconds=0,
        )

    def run_row(self, **overrides):
        return {
            'id': 42,
            'path': '.github/workflows/detect-package.yml',
            'event': 'workflow_dispatch',
            'head_branch': 'main',
            'actor': {'id': 356454848},
            'display_title': 'Daily package [opl-release:17]',
            'html_url': 'https://github.com/example/example-product/actions/runs/42',
            **overrides,
        }

    def test_dispatches_legacy_observed_lane_independent_of_primary_schedule(self):
        with patch('opl_release.dispatch.gh', side_effect=[
            {'workflow_runs': []}, {'workflow_run_id': 42}, self.run_row(),
        ]) as gh:
            receipt = self.dispatch()
        self.assertEqual(receipt['status'], 'dispatch_submitted')
        self.assertEqual(receipt['lane'], 'package')
        self.assertEqual(receipt['workflow_run_id'], 42)
        self.assertIn('created=%3E%3D2026-11-28T00%3A00%3A00Z', gh.call_args_list[0].args[0])
        self.assertEqual(gh.call_args_list[1].args, (
            'repos/example/example-product/actions/workflows/detect-package.yml/dispatches',
        ))
        self.assertEqual(gh.call_args_list[1].kwargs, {
            'method': 'POST',
            'payload': {'ref': 'main', 'inputs': {'opl_release_control_run_id': '17'}},
            'api_version': '2026-03-10',
            'retry_auth': False,
        })
        self.assertTrue(gh.call_args_list[2].args[0].endswith('/actions/runs/42'))

    def test_preflight_binds_existing_run_without_post(self):
        with patch('opl_release.dispatch.gh', return_value={'workflow_runs': [self.run_row()]}) as gh:
            receipt = self.dispatch()
        self.assertEqual(receipt['status'], 'already_dispatched')
        self.assertEqual(receipt['workflow_run_id'], 42)
        gh.assert_called_once()

    def test_api_run_id_must_match_exact_identity(self):
        with patch('opl_release.dispatch.gh', side_effect=[
            {'workflow_runs': []},
            {'workflow_run_id': 42},
            self.run_row(actor={'id': 12345}),
        ]):
            receipt = self.dispatch()
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(receipt['reported_run_id'], 42)
        self.assertNotIn('workflow_run_id', receipt)

    def test_timeout_reconciles_by_exact_actor_marker_path_ref_and_event(self):
        wrong_identity = [
            self.run_row(id=41, actor={'id': 12345}),
            self.run_row(id=43, display_title='Daily package [opl-release:170]'),
            self.run_row(id=44, path='.github/workflows/other.yml'),
            self.run_row(id=45, head_branch='dev'),
            self.run_row(id=46, event='schedule'),
        ]
        with patch('opl_release.dispatch.gh', side_effect=[
            {'workflow_runs': wrong_identity},
            TimeoutExpired('gh api', 20),
            {'workflow_runs': [*wrong_identity, self.run_row()]},
        ]) as gh:
            receipt = self.dispatch()
        self.assertEqual(receipt['workflow_run_id'], 42)
        self.assertEqual(receipt['status'], 'dispatch_submitted')
        self.assertEqual(sum(call.kwargs.get('method') == 'POST' for call in gh.call_args_list), 1)

    def test_empty_response_with_multiple_matching_runs_is_unknown(self):
        with patch('opl_release.dispatch.gh', side_effect=[
            {'workflow_runs': []},
            None,
            {'workflow_runs': [self.run_row(), self.run_row(id=101)]},
        ]) as gh:
            receipt = self.dispatch()
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(receipt['lane'], 'package')
        self.assertEqual(sum(call.kwargs.get('method') == 'POST' for call in gh.call_args_list), 1)

    def test_rerun_with_no_exact_match_or_first_attempt_without_token_stays_read_only(self):
        for attempt, token, outcome in [(2, True, 'unknown'), (1, False, 'dispatch_unavailable')]:
            with self.subTest(attempt=attempt):
                with patch('opl_release.dispatch.gh', return_value={'workflow_runs': []}) as gh:
                    self.assertEqual(self.dispatch(attempt=attempt, scoped_token=token)['status'], outcome)
                gh.assert_called_once()
                self.assertNotIn('method', gh.call_args.kwargs)

    def test_manual_apply_or_no_owner_opt_in_never_dispatches(self):
        with patch('opl_release.dispatch.gh') as gh:
            self.assertEqual(self.dispatch(event='workflow_dispatch')['status'], 'not_scheduled')
            self.config['observed_publishers'][0].pop('daily_dispatch')
            self.write_config()
            self.assertEqual(self.dispatch()['status'], 'not_configured')
        gh.assert_not_called()

    def test_cli_retains_unknown_receipt_and_exits_nonzero(self):
        output = self.root / 'receipt.json'
        args = [
            'dispatch-owner', '--owner-root', str(self.root),
            '--repository', 'example/example-product', '--subject-id', 'example-product',
            '--output', str(output),
        ]
        env = {
            'GITHUB_RUN_ID': '17',
            'GITHUB_RUN_ATTEMPT': '2',
            'GITHUB_EVENT_NAME': 'schedule',
            'CONTROL_CREATED_AT': '2026-11-28T00:00:00Z',
        }
        with patch.dict(os.environ, env), patch('opl_release.dispatch.gh', return_value={'workflow_runs': []}):
            self.assertEqual(main(args), 1)
        receipt = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(receipt['lane'], 'package')
        self.assertEqual(receipt['control_run_attempt'], 2)
