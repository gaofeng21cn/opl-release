from __future__ import annotations

import json
from pathlib import Path
import unittest

from opl_release.config import normalize_release_config, validate_fleet_manifest

ROOT = Path(__file__).resolve().parents[1]


class SchemaAndFleetTests(unittest.TestCase):
    def test_fleet_json_is_valid_and_has_unique_subjects(self):
        fleet = json.loads((ROOT / 'fleet.json').read_text(encoding='utf-8'))
        validated = validate_fleet_manifest(fleet)
        self.assertEqual(validated['schema_version'], 'opl.release-fleet.v1')
        self.assertGreaterEqual(len(validated['products']), 8)
        self.assertIn('catalog_source', validated)

    def test_owner_dispatch_gate_rejects_string_values(self):
        fleet = json.loads((ROOT / 'fleet.json').read_text(encoding='utf-8'))
        fleet['products'][0]['enable_owner_dispatch'] = 'true'
        with self.assertRaisesRegex(Exception, 'enable_owner_dispatch'):
            validate_fleet_manifest(fleet)

    def test_schemas_are_valid_json_documents(self):
        for name in (
            'opl-release.v1.schema.json',
            'opl-release-receipt.v1.schema.json',
            'release-fleet.v1.schema.json',
        ):
            doc = json.loads((ROOT / 'schemas' / name).read_text(encoding='utf-8'))
            self.assertIn('properties', doc)

    def test_legacy_product_and_package_configs_normalize_cleanly(self):
        fw_legacy = {
            'schema_version': 'opl.product-release.v1',
            'product_id': 'example-core',
            'repository': 'gaofeng21cn/example-core',
            'strategy': 'daily_patch',
            'trigger': 'tag',
            'publisher_workflow': 'publish-core.yml',
            'channel': {'kind': 'framework_oci'},
            'version_file': {'path': 'package.json', 'kind': 'json'},
            'release_paths': ['packages'],
        }
        norm_fw = normalize_release_config(fw_legacy)
        self.assertEqual(norm_fw['schema_version'], 'opl.release.v1')
        self.assertEqual(norm_fw['lanes'][0]['channel']['kind'], 'oci')
        self.assertEqual(norm_fw['lanes'][0]['channel']['repository'], 'gaofeng21cn/example-core-framework')

        pkg_legacy = {
            'strategy': 'daily_patch',
            'release_paths': ['skills'],
            'release_workflows': ['native.yml'],
        }
        norm_pkg = normalize_release_config(
            pkg_legacy,
            fallback_subject_id='sample-pkg',
            fallback_repository='gaofeng21cn/sample-pkg',
        )
        self.assertEqual(norm_pkg['kind'], 'package')
        self.assertEqual(norm_pkg['lanes'][0]['channel']['kind'], 'oci')
        self.assertEqual(norm_pkg['lanes'][1]['workflow'], 'native.yml')

    def test_owner_dispatch_can_coexist_with_daily_patch_on_an_observed_lane(self):
        cfg = normalize_release_config({
            'schema_version': 'opl.release.v1',
            'subject_id': 'owner-product',
            'kind': 'product',
            'repository': 'gaofeng21cn/owner-product',
            'schedule': 'daily_patch',
            'lanes': [
                {'lane': 'stable', 'workflow': 'stable.yml', 'primary': True},
                {'lane': 'nightly', 'workflow': 'nightly.yml', 'daily_dispatch': True},
            ],
        })
        self.assertTrue(cfg['lanes'][1]['daily_dispatch'])

    def test_rejects_multiple_daily_dispatch_lanes(self):
        with self.assertRaisesRegex(Exception, 'Only one lane'):
            normalize_release_config({
                'schema_version': 'opl.release.v1',
                'subject_id': 'owner-product',
                'kind': 'product',
                'repository': 'gaofeng21cn/owner-product',
                'schedule': 'owner_schedule',
                'lanes': [
                    {'lane': 'stable', 'workflow': 'stable.yml', 'primary': True, 'daily_dispatch': True},
                    {'lane': 'nightly', 'workflow': 'nightly.yml', 'daily_dispatch': True},
                ],
            })


if __name__ == '__main__':
    unittest.main()
