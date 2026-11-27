from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from opl_release.observe import observe_fleet, render_markdown
from opl_release.receipts import build_receipt, receipt_artifact_name, validate_receipt

ROOT = Path(__file__).resolve().parents[1]


class ReceiptAndObserveTests(unittest.TestCase):
    def test_zero_hardcoded_product_ids_in_engine_source(self):
        """Anti-backsliding guard: engine Python code must never branch on specific product IDs."""
        forbidden = (
            'opl-app',
            'opl-dsh',
            'opl-cloud',
            'opl-netfleet',
            'u60-pro-companion',
            'opl-codex-models',
            'opl-fleet-cockpit',
            'one-person-lab-app',
            'one-person-lab-cloud',
            'opl-nightly-publication',
            'cloud-daily-release-result',
        )
        for py_file in sorted((ROOT / 'src/opl_release').rglob('*.py')):
            text = py_file.read_text(encoding='utf-8')
            for needle in forbidden:
                self.assertNotIn(needle, text, f'Forbidden product-specific token {needle!r} found in {py_file.name}')

    def test_receipt_roundtrip_and_generic_observe_with_receipts(self):
        receipt = build_receipt(
            subject_id='widget-alpha',
            lane='macos-candidate',
            stage='qualification_pending',
            candidate_version='1.4.2',
            candidate_tag='v1.4.2',
            candidate_commit='d' * 40,
            qualification_required=True,
            qualification_status='pending',
            run_url='https://github.com/gaofeng21cn/widget-alpha/actions/runs/99',
        )
        validate_receipt(receipt, expected_subject_id='widget-alpha', expected_lane='macos-candidate')

        buf = io.BytesIO()
        with ZipFile(buf, 'w') as zf:
            zf.writestr('release-receipt.json', json.dumps(receipt))
        zip_bytes = buf.getvalue()

        fleet = {
            'schema_version': 'opl.release-fleet.v1',
            'owner': 'gaofeng21cn',
            'catalog_source': {
                'repository': 'gaofeng21cn/opl-catalog',
                'ref': 'main',
                'index_path': 'catalog-index.json',
                'oci_registry': 'ghcr.io',
                'oci_namespace': 'gaofeng21cn/one-person-lab-packages',
            },
            'products': [{'subject_id': 'widget-alpha', 'repository': 'gaofeng21cn/widget-alpha'}],
        }
        widget_cfg = {
            'schema_version': 'opl.release.v1',
            'subject_id': 'widget-alpha',
            'kind': 'product',
            'repository': 'gaofeng21cn/widget-alpha',
            'schedule': 'daily_patch',
            'lanes': [{
                'lane': 'macos-candidate',
                'workflow': 'macos.yml',
                'primary': True,
                'channel': {'kind': 'sparkle_appcast', 'release_tag': 'macos-stable', 'asset': 'appcast.xml'},
            }],
        }
        catalog_idx = {
            'packages': [
                {'package_id': 'pub-pkg', 'visibility': 'public', 'version': '0.1.0', 'manifest': {'v2_release': {'version': '0.1.1'}}},
                {'package_id': 'priv-pkg', 'visibility': 'authorized_only', 'version': '0.2.0', 'manifest': {}},
                {'package_id': 'old-pkg', 'visibility': 'public', 'version': '0.0.1', 'manifest': {'publication_channel_admission': 'retired'}},
            ]
        }

        def fake_gh(endpoint, **kwargs):
            if endpoint.startswith('repos/gaofeng21cn/widget-alpha/contents/.opl-release.json'):
                return {'content': base64.b64encode(json.dumps(widget_cfg).encode()).decode()}
            if endpoint.startswith('repos/gaofeng21cn/opl-catalog/contents/catalog-index.json'):
                # Simulate GitHub Contents API omitting inline content for >1MB files.
                return {'content': '', 'encoding': 'none', 'sha': 'e' * 40}
            if endpoint == f"repos/gaofeng21cn/opl-catalog/git/blobs/{'e' * 40}":
                return {'content': base64.b64encode(json.dumps(catalog_idx).encode()).decode()}
            if '/workflows/macos.yml/runs' in endpoint:
                return {'workflow_runs': [{'id': 99, 'status': 'completed', 'conclusion': 'success', 'html_url': 'https://github.com/o/r/actions/runs/99', 'head_sha': 'd' * 40, 'display_title': 'run'}]}
            if '/actions/runs/99/artifacts' in endpoint:
                return {'artifacts': [{'id': 501, 'name': receipt_artifact_name('widget-alpha', 'macos-candidate', 99), 'expired': False, 'size_in_bytes': len(zip_bytes)}]}
            raise AssertionError(f'Unexpected gh call: {endpoint}')

        class FakeSub:
            returncode = 0
            stdout = zip_bytes

        fleet_path = ROOT / 'tmp-test-fleet.json'
        try:
            fleet_path.write_text(json.dumps(fleet), encoding='utf-8')
            with patch('opl_release.observe.gh', side_effect=fake_gh), \
                 patch('opl_release.receipts.gh', side_effect=fake_gh), \
                 patch('opl_release.observe.probe_channel', return_value={'status': 'present', 'version': '1.4.1'}), \
                 patch('opl_release.observe.probe_oci', return_value={'status': 'present', 'version': '0.1.1'}), \
                 patch('subprocess.run', return_value=FakeSub()):
                report = observe_fleet(fleet_path)
        finally:
            fleet_path.unlink(missing_ok=True)

        self.assertEqual(report['products'][0]['status'], 'qualification_pending')
        self.assertEqual(report['products'][0]['lanes'][0]['stage'], 'qualification_pending')
        self.assertEqual(report['packages']['total_admitted'], 2)
        self.assertEqual(report['packages']['public_verified'], 1)
        self.assertEqual(report['packages']['restricted'], 1)
        md = render_markdown(report)
        self.assertIn('widget-alpha', md)
        self.assertIn('pub-pkg', md)


if __name__ == '__main__':
    unittest.main()
