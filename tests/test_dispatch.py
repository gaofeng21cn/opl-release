from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opl_release.dispatch import dispatch_configured_owner, dispatch_owner_workflow


class DispatchTests(unittest.TestCase):
    def test_configured_owner_reads_primary_lane_and_dispatches_its_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.opl-release.json').write_text(json.dumps({
                'schema_version': 'opl.release.v1',
                'subject_id': 'owner-product',
                'kind': 'product',
                'repository': 'gaofeng21cn/owner-product',
                'schedule': 'owner_schedule',
                'lanes': [{
                    'lane': 'stable',
                    'workflow': 'stable.yml',
                    'primary': True,
                    'daily_dispatch': {
                        'enabled': True,
                        'ref': 'main',
                        'control_run_id_input': 'control',
                        'inputs': {'mode': 'daily'},
                    },
                }],
            }), encoding='utf-8')
            with patch('opl_release.dispatch.dispatch_owner_workflow', return_value={'status': 'dispatch_submitted'}) as dispatch:
                result = dispatch_configured_owner(
                    root,
                    expected_repository='gaofeng21cn/owner-product',
                    expected_subject_id='owner-product',
                    control_run_id=17,
                )
            self.assertEqual(result['status'], 'dispatch_submitted')
            dispatch.assert_called_once_with(
                'gaofeng21cn/owner-product',
                {
                    'enabled': True,
                    'ref': 'main',
                    'control_run_id_input': 'control',
                    'inputs': {'mode': 'daily'},
                    'workflow': 'stable.yml',
                },
                control_run_id=17,
                reconcile_seconds=60,
            )

    def test_configured_owner_without_opt_in_is_a_read_only_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.opl-release.json').write_text(json.dumps({
                'schema_version': 'opl.release.v1',
                'subject_id': 'owner-product',
                'kind': 'product',
                'repository': 'gaofeng21cn/owner-product',
                'schedule': 'owner_schedule',
                'lanes': [{'lane': 'stable', 'workflow': 'stable.yml', 'primary': True}],
            }), encoding='utf-8')
            with patch('opl_release.dispatch.dispatch_owner_workflow') as dispatch:
                result = dispatch_configured_owner(
                    root,
                    expected_repository='gaofeng21cn/owner-product',
                    expected_subject_id='owner-product',
                    control_run_id=17,
                )
            self.assertEqual(result['status'], 'not_configured')
            dispatch.assert_not_called()

    def test_binds_api_returned_run_and_passes_control_marker(self):
        calls = []

        def fake_gh(endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            return {'workflow_run_id': 42, 'run_url': 'https://github.com/o/r/actions/runs/42'}

        with patch('opl_release.dispatch.gh', side_effect=fake_gh):
            result = dispatch_owner_workflow(
                'gaofeng21cn/owner-product',
                {
                    'enabled': True,
                    'workflow': 'stable.yml',
                    'ref': 'main',
                    'control_run_id_input': 'opl_release_control_run_id',
                },
                control_run_id=7,
            )

        self.assertEqual(result['workflow_run_id'], 42)
        self.assertEqual(calls[0][1]['payload'], {
            'ref': 'main',
            'inputs': {
                'opl_release_control_run_id': '7',
            },
        })

    def test_empty_response_reconciles_by_marker_and_never_picks_latest(self):
        def fake_gh(endpoint, **kwargs):
            if endpoint.endswith('/dispatches'):
                return None
            return {'workflow_runs': [
                {
                    'id': 11,
                    'path': '.github/workflows/stable.yml',
                    'event': 'workflow_dispatch',
                    'head_branch': 'main',
                    'created_at': '2026-11-28T00:00:00Z',
                    'display_title': 'unrelated run',
                },
                {
                    'id': 12,
                    'path': '.github/workflows/stable.yml',
                    'event': 'workflow_dispatch',
                    'head_branch': 'main',
                    'created_at': '2026-11-28T00:00:00Z',
                    'display_title': 'stable [opl-release:8]',
                    'html_url': 'https://github.com/o/r/actions/runs/12',
                },
            ]}

        with patch('opl_release.dispatch.gh', side_effect=fake_gh), patch('opl_release.dispatch.time.time', return_value=1795824000), patch('opl_release.dispatch.time.sleep'):
            result = dispatch_owner_workflow(
                'gaofeng21cn/owner-product',
                {'enabled': True, 'workflow': 'stable.yml', 'ref': 'main', 'control_run_id_input': 'control'},
                control_run_id=8,
                reconcile_seconds=0,
            )
        self.assertEqual(result['workflow_run_id'], 12)

    def test_unknown_empty_response_fails_without_retry(self):
        calls = []

        def fake_gh(endpoint, **kwargs):
            calls.append(endpoint)
            return None if endpoint.endswith('/dispatches') else {'workflow_runs': []}

        with patch('opl_release.dispatch.gh', side_effect=fake_gh), patch('opl_release.dispatch.time.sleep'):
            with self.assertRaisesRegex(Exception, 'unknown'):
                dispatch_owner_workflow(
                    'gaofeng21cn/owner-product',
                    {'enabled': True, 'workflow': 'stable.yml', 'ref': 'main', 'control_run_id_input': 'control'},
                    control_run_id=9,
                    reconcile_seconds=0,
                )
        self.assertEqual(len([call for call in calls if call.endswith('/dispatches')]), 1)


if __name__ == '__main__':
    unittest.main()
