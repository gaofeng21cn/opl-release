from __future__ import annotations

import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from opl_release.common import ReleaseError
from opl_release.prepare import plan_candidate
from opl_release.probes import probe_channel
from opl_release.versioning import update_version_files


class PrepareAndVersioningTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> str:
        return subprocess.run(['git', *args], cwd=root, check=True, text=True, capture_output=True).stdout.strip()

    def test_candidate_planning_strips_only_the_declared_github_release_suffix(self):
        tag = 'v0.2.2-companion'
        with patch('opl_release.probes.gh', side_effect=[{'tag_name': tag, 'html_url': 'release-url'}, {'sha': 'base'}]) as api:
            published = probe_channel('owner/companion', {'kind': 'github_release'})
        api.assert_called_with(f'repos/owner/companion/commits/{tag}')

        cfg = {'tag_prefix': 'v', 'tag_suffix': '-companion', 'delivery_paths': ['app']}
        with (
            patch('opl_release.prepare.git', return_value='head'),
            patch('opl_release.prepare.read_declared_version', return_value='0.2.2'),
            patch('opl_release.prepare.delivery_changed', return_value=True),
            patch('opl_release.prepare.run', side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)]),
        ):
            result = plan_candidate(Path.cwd(), cfg, published)
        self.assertEqual((result['status'], result['version'], result['tag']), ('needs_release', '0.2.3', 'v0.2.3-companion'))
        self.assertEqual((published['tag'], published['version']), (tag, '0.2.2-companion'))

        for mismatched_cfg, mismatched_channel in [
            (dict(cfg, tag_suffix='-other'), published),
            (cfg, dict(published, kind='oci')),
        ]:
            with (
                self.subTest(tag_suffix=mismatched_cfg['tag_suffix'], channel=mismatched_channel['kind']),
                patch('opl_release.prepare.git', return_value='head'),
                patch('opl_release.prepare.read_declared_version', return_value='0.2.2'),
                self.assertRaisesRegex(ReleaseError, 'Expected stable SemVer'),
            ):
                plan_candidate(Path.cwd(), mismatched_cfg, mismatched_channel)

    def test_plan_candidate_freezes_tag_and_resumes_across_doc_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, 'init', '--quiet')
            self._git(root, 'config', 'user.name', 'Test')
            self._git(root, 'config', 'user.email', 'test@example.invalid')
            (root / 'app').mkdir()
            (root / 'app/main.py').write_text('print(1)\n')
            (root / 'product-version.json').write_text('{"version":"1.0.0"}\n')
            (root / 'README.md').write_text('v1\n')
            self._git(root, 'add', '--all')
            self._git(root, 'commit', '--quiet', '-m', 'init')
            base = self._git(root, 'rev-parse', 'HEAD')

            cfg = {
                'tag_prefix': 'v',
                'tag_suffix': '',
                'delivery_paths': ['app'],
                'version_files': [{'path': 'product-version.json', 'kind': 'json'}],
            }
            published = {'version': '1.0.0', 'source_commit': base}

            (root / 'README.md').write_text('docs only\n')
            self._git(root, 'add', '--all')
            self._git(root, 'commit', '--quiet', '-m', 'docs')
            self.assertEqual(plan_candidate(root, cfg, published)['status'], 'unchanged')

            (root / 'app/main.py').write_text('print(2)\n')
            self._git(root, 'add', '--all')
            self._git(root, 'commit', '--quiet', '-m', 'feature')
            first_plan = plan_candidate(root, cfg, published)
            self.assertEqual((first_plan['status'], first_plan['version'], first_plan['tag']), ('needs_release', '1.0.1', 'v1.0.1'))

            # Simulate candidate tag frozen before subsequent README commit.
            (root / 'product-version.json').write_text('{"version":"1.0.1"}\n')
            self._git(root, 'add', '--all')
            self._git(root, 'commit', '--quiet', '-m', 'bump')
            frozen = self._git(root, 'rev-parse', 'HEAD')
            self._git(root, 'tag', '-a', 'v1.0.1', '-m', 'freeze')
            (root / 'README.md').write_text('more docs\n')
            self._git(root, 'add', '--all')
            self._git(root, 'commit', '--quiet', '-m', 'docs 2')

            resumed = plan_candidate(root, cfg, published)
            self.assertEqual((resumed['status'], resumed['source_commit']), ('resume', frozen))

    def test_update_version_files_handles_json_toml_python_xml_and_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pkg.json').write_text('{"version":"0.1.0"}\n')
            (root / 'pyproject.toml').write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
            (root / 'mod.py').write_text('__version__ = "0.1.0"\n')
            (root / 'app.csproj').write_text('<Project><PropertyGroup><Version>0.1.0</Version></PropertyGroup></Project>\n')
            (root / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': '10'}))

            changed = update_version_files(root, [
                {'path': 'pkg.json', 'kind': 'json'},
                {'path': 'pyproject.toml', 'kind': 'toml_project'},
                {'path': 'mod.py', 'kind': 'python_version'},
                {'path': 'app.csproj', 'kind': 'xml_project'},
                {'path': 'Info.plist', 'kind': 'plist_bundle'},
            ], '0.1.1')
            self.assertEqual(len(changed), 5)
            self.assertEqual(json.loads((root / 'pkg.json').read_text())['version'], '0.1.1')
            self.assertIn('version = "0.1.1"', (root / 'pyproject.toml').read_text())
            self.assertIn('__version__ = "0.1.1"', (root / 'mod.py').read_text())
            self.assertIn('<Version>0.1.1</Version>', (root / 'app.csproj').read_text())
            plist = plistlib.loads((root / 'Info.plist').read_bytes())
            self.assertEqual((plist['CFBundleShortVersionString'], plist['CFBundleVersion']), ('0.1.1', '11'))


if __name__ == '__main__':
    unittest.main()
