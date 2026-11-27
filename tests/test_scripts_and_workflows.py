from __future__ import annotations

from pathlib import Path
import subprocess
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
        self.assertGreaterEqual(len(workflows), 8)
        for wf in workflows:
            text = wf.read_text(encoding='utf-8')
            self.assertNotIn('e73718a2a', text, f'Legacy pre-v2 pin leaked into {wf.name}')
            self.assertNotIn('src/adapters/', text, f'Framework internal src path leaked into {wf.name}')
            self.assertNotIn('opl-flow', text, f'Circular opl-flow dependency leaked into {wf.name}')

        observe_wf = (ROOT / '.github/workflows/observe-daily-releases.yml').read_text(encoding='utf-8')
        self.assertNotIn('secrets.', observe_wf, 'Read-only observer workflow must hold zero secrets')
        self.assertNotIn('contents: write', observe_wf)
        self.assertNotIn('actions: write', observe_wf)


if __name__ == '__main__':
    unittest.main()
