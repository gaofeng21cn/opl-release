from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opl_release.observe import observe_fleet, render_markdown
from opl_release.observe_dispatch import observe_dispatch


class ObserveDispatchTests(unittest.TestCase):
    target = {
        'subject_id': 'widget', 'repository': 'example/widget', 'enable_owner_dispatch': True,
    }

    def run_record(self, **overrides):
        return {
            'id': 42,
            'run_attempt': 2,
            'repository': {'id': 5, 'full_name': 'example/opl-release'},
            'event': 'schedule',
            'head_branch': 'main',
            'head_sha': 'a' * 40,
            'path': '.github/workflows/control-daily-releases.yml',
            'created_at': '2026-11-28T18:43:00Z',
            'updated_at': '2026-11-28T18:46:00Z',
            'status': 'completed',
            'conclusion': 'success',
            **overrides,
        }

    def artifact(self, attempt, **overrides):
        return {
            'id': 543,
            'name': f'opl-release-dispatch-widget-42-{attempt}',
            'size_in_bytes': 300,
            'expired': False,
            'workflow_run': {'id': 42, 'repository_id': 5, 'head_sha': 'a' * 40, 'head_branch': 'main'},
            **overrides,
        }

    def receipt(self, attempt, **overrides):
        return {
            'subject_id': 'widget', 'repository': 'example/widget', 'lane': 'nightly',
            'control_run_id': '42', 'control_run_attempt': attempt, 'status': 'dispatch_submitted',
            'workflow_run_id': 73, 'run_url': 'https://example.com/unbound',
            **overrides,
        }

    def job(self, **overrides):
        return {
            'name': 'Dispatch widget', 'run_id': 42, 'run_attempt': 2,
            'head_sha': 'a' * 40, 'status': 'completed', **overrides,
        }

    def api(self, runs=None, artifacts=(), jobs=()):
        fixtures = {
            'repos/example/opl-release/actions/workflows/control-daily-releases.yml/runs?branch=main&event=schedule&per_page=20': {
                'workflow_runs': [self.run_record()] if runs is None else runs,
            },
            'repos/example/opl-release/actions/runs/42/artifacts?per_page=100&page=1': {'artifacts': list(artifacts)},
            'repos/example/opl-release/actions/runs/42/jobs?filter=all&per_page=100&page=1': {'jobs': list(jobs)},
        }
        return patch('opl_release.observe_dispatch.gh', side_effect=fixtures.__getitem__)

    def test_exact_latest_schedule_and_receipt_render_alongside_publication(self):
        older = self.run_record(id=41, created_at='2026-11-27T18:43:00Z', updated_at='2026-11-29T00:00:00Z')
        with tempfile.TemporaryDirectory() as directory:
            fleet = Path(directory) / 'fleet.json'
            fleet.write_text(json.dumps({
                'schema_version': 'opl.release-fleet.v1', 'owner': 'example', 'products': [self.target],
            }))
            product = {
                'subject_id': 'widget', 'repository': 'example/widget', 'status': 'qualification_pending',
                'lanes': [{
                    'lane': 'nightly', 'stage': 'qualification_pending',
                    'published_channel': {'status': 'present', 'version': '1.2.3'},
                }],
            }
            with self.api(runs=[older, self.run_record()], artifacts=[self.artifact(2)], jobs=[self.job()]), \
                 patch('opl_release.observe_dispatch.download_json_artifact', return_value=self.receipt(2)) as download, \
                 patch('opl_release.observe._observe_product', return_value=product):
                report = observe_fleet(fleet, subject_filter='widget')
        download.assert_called_once_with('example/opl-release', 543, 'owner-dispatch-result.json')
        self.assertEqual(report['dispatch_controller']['id'], 42)
        self.assertEqual(report['products'][0]['central_dispatch']['run_url'], 'https://github.com/example/widget/actions/runs/73')
        markdown = render_markdown(report)
        self.assertIn('`qualification_pending` | `1.2.3`', markdown)
        self.assertIn('`dispatch_submitted` | 2 | [Actions](https://github.com/example/widget/actions/runs/73)', markdown)

    def test_rerun_only_reuses_older_receipt_when_subject_job_was_not_rerun(self):
        first_job = self.job(run_attempt=1)
        for status, later_jobs, outcome in [
            ('completed', [], 'dispatch_submitted'),
            ('completed', [self.job()], 'missing'),
            ('in_progress', [self.job(status='in_progress')], 'pending'),
            ('in_progress', [], 'pending'),
        ]:
            with self.subTest(outcome=outcome), \
                 self.api(runs=[self.run_record(status=status)], artifacts=[self.artifact(1)], jobs=[first_job, *later_jobs]), \
                 patch('opl_release.observe_dispatch.download_json_artifact', return_value=self.receipt(1)) as download:
                _, subjects = observe_dispatch('example', [self.target])
                self.assertEqual(subjects['widget']['status'], outcome)
                if outcome == 'dispatch_submitted':
                    self.assertEqual(subjects['widget']['control_run_attempt'], 1)
                else:
                    download.assert_not_called()

    def test_partial_rerun_does_not_hide_missing_receipt_from_newer_subject_job(self):
        with self.api(
            runs=[self.run_record(run_attempt=3)], artifacts=[self.artifact(1)],
            jobs=[self.job(run_attempt=1), self.job(run_attempt=2)],
        ), patch('opl_release.observe_dispatch.download_json_artifact') as download:
            self.assertEqual(observe_dispatch('example', [self.target])[1]['widget']['status'], 'missing')
        download.assert_not_called()

    def test_pending_missing_and_unknown_are_distinct(self):
        with self.api(runs=[self.run_record(status='queued', conclusion=None)]):
            self.assertEqual(observe_dispatch('example', [self.target])[1], {'widget': {'status': 'pending'}})
        with self.api(jobs=[self.job()]):
            self.assertEqual(observe_dispatch('example', [self.target])[1], {'widget': {'status': 'missing'}})
        with self.api(artifacts=[self.artifact(2)]), \
             patch('opl_release.observe_dispatch.download_json_artifact', return_value=self.receipt(2, status='unknown', workflow_run_id=None)):
            self.assertEqual(observe_dispatch('example', [self.target])[1]['widget']['status'], 'unknown')

    def test_wrong_or_duplicate_identity_does_not_fall_back(self):
        for artifacts, receipt in [
            ([self.artifact(2), self.artifact(2, id=544)], self.receipt(2)),
            ([self.artifact(2)], self.receipt(1)),
            ([self.artifact(2, workflow_run={'id': 41})], self.receipt(2)),
        ]:
            with self.subTest(artifacts=artifacts, receipt=receipt), \
                 self.api(artifacts=[*artifacts, self.artifact(1)]), \
                 patch('opl_release.observe_dispatch.download_json_artifact', return_value=receipt):
                row = observe_dispatch('example', [self.target])[1]['widget']
                self.assertEqual(row['status'], 'unknown')
                self.assertNotIn('run_url', row)

    def test_only_scheduled_run_and_enabled_subject_may_be_observed(self):
        with self.api(runs=[]):
            self.assertEqual(observe_dispatch('example', [self.target])[1], {'widget': {'status': 'not_observed'}})
        with self.api(runs=[self.run_record(event='workflow_dispatch')]):
            self.assertEqual(observe_dispatch('example', [self.target])[1], {'widget': {'status': 'unknown'}})
        with patch('opl_release.observe_dispatch.gh') as gh:
            self.assertEqual(observe_dispatch('example', [{**self.target, 'observe_only': True}]), (None, {}))
            gh.assert_not_called()
