from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from textwrap import dedent
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ScriptsAndWorkflowsTests(unittest.TestCase):
    def test_sparkle_scripts_pass_bash_syntax_check(self):
        scripts = sorted((ROOT / 'scripts/macos-sparkle-release').glob('*.sh'))
        self.assertEqual(len(scripts), 4)
        for script in scripts:
            res = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f'{script.name}: {res.stderr}')

    def test_workflows_do_not_reference_legacy_framework_pins_or_internal_src(self):
        workflows = sorted((ROOT / '.github/workflows').glob('*.yml'))
        self.assertGreaterEqual(len(workflows), 6)
        for wf in workflows:
            text = wf.read_text(encoding='utf-8')
            self.assertNotIn('e73718a2a', text, f'Legacy pre-v2 pin leaked into {wf.name}')
            self.assertNotIn('src/adapters/', text, f'Framework internal src path leaked into {wf.name}')
            self.assertNotIn('opl-flow', text, f'Circular opl-flow dependency leaked into {wf.name}')

        observe_wf = (ROOT / '.github/workflows/observe-daily-releases.yml').read_text(encoding='utf-8')
        self.assertNotIn('secrets.', observe_wf, 'Read-only observer workflow must hold zero secrets')
        self.assertNotIn('contents: write', observe_wf)
        self.assertNotIn('actions: write', observe_wf)

        control_wf = (ROOT / '.github/workflows/control-daily-releases.yml').read_text(encoding='utf-8')
        self.assertIn('repositories: ${{ matrix.subject.repository_name }}', control_wf)
        self.assertNotIn('repositories: ${{ matrix.subject.repository }}', control_wf)
        for wf in workflows:
            self.assertNotIn('app-id:', wf.read_text(encoding='utf-8'), f'Deprecated app-id input found in {wf.name}')

        verify_action = (ROOT / '.github/actions/verify-dispatch/action.yml').read_text(encoding='utf-8')
        self.assertIn('opl_release verify-dispatch', verify_action)

        sparkle_wf = (ROOT / '.github/workflows/reusable-macos-sparkle-release.yml').read_text(encoding='utf-8')
        self.assertIn('emit_receipt:', sparkle_wf)
        self.assertIn('receipt_stage:', sparkle_wf)
        self.assertIn('overwrite: true', (ROOT / '.github/workflows/reusable-emit-receipt.yml').read_text(encoding='utf-8'))

        for retired in ('reusable-publish-package-v2.yml', 'reusable-publish-oci.yml'):
            self.assertFalse(
                (ROOT / '.github/workflows' / retired).exists(),
                f'Retired zero-caller workflow {retired} must remain removed',
            )

    def test_owner_dispatch_inventory_requires_opt_in_without_changing_prepare_inventory(self):
        control_wf = (ROOT / '.github/workflows/control-daily-releases.yml').read_text(encoding='utf-8')
        inventory = re.search(r"python3 -B - <<'PY'\n(.*?)\n +PY", control_wf, re.DOTALL)
        self.assertIsNotNone(inventory)
        products = [
            {'subject_id': 'absent', 'repository': 'example/absent'},
            {'subject_id': 'disabled', 'repository': 'example/disabled', 'enable_owner_dispatch': False},
            {'subject_id': 'enabled', 'repository': 'example/enabled', 'enable_owner_dispatch': True},
            {
                'subject_id': 'observed', 'repository': 'example/observed',
                'enable_owner_dispatch': True, 'observe_only': True,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'fleet.json').write_text(json.dumps({
                'schema_version': 'opl.release-fleet.v1', 'owner': 'example', 'products': products,
            }), encoding='utf-8')
            output = root / 'output'
            result = subprocess.run(
                ['python3', '-B', '-c', dedent(inventory[1])],
                cwd=root, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'GITHUB_OUTPUT': str(output), 'SUBJECT_ID': ''},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            matrix = dict(line.split('=', 1) for line in output.read_text(encoding='utf-8').splitlines())
        self.assertEqual([row['subject_id'] for row in json.loads(matrix['subjects'])], [
            'absent', 'disabled', 'enabled', 'observed',
        ])
        self.assertEqual([row['subject_id'] for row in json.loads(matrix['dispatch_subjects'])], ['enabled'])


if __name__ == '__main__':
    unittest.main()
