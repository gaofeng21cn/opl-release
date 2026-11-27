from __future__ import annotations

import hashlib
import io
import json
import unittest
from unittest.mock import patch

from opl_release.common import ReleaseError
from opl_release.probes import probe_channel


class _FakeResp:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._buf = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ProbeTests(unittest.TestCase):
    def test_probe_oci_reads_manifest_and_package_payload(self):
        payload = json.dumps({
            'package_id': 'demo-pkg',
            'package_version': '1.2.3',
            'source_commit': 'a' * 40,
        }).encode('utf-8')
        payload_digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
        manifest = json.dumps({
            'annotations': {'org.opencontainers.image.version': '1.2.3', 'org.opencontainers.image.revision': 'a' * 40},
            'layers': [{
                'mediaType': 'application/vnd.onepersonlab.package.payload.v1+json',
                'digest': payload_digest,
                'size': len(payload),
            }],
        }).encode('utf-8')
        manifest_digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()

        def fake_urlopen(req, timeout=20):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if '/token?' in url:
                return _FakeResp(b'{"token":"tok"}')
            if '/manifests/' in url:
                return _FakeResp(manifest, {'Docker-Content-Digest': manifest_digest})
            if '/blobs/' in url:
                return _FakeResp(payload)
            raise AssertionError(url)

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            res = probe_channel('gaofeng21cn/demo', {
                'kind': 'oci',
                'registry': 'ghcr.io',
                'repository': 'gaofeng21cn/one-person-lab-packages/demo-pkg',
                'tag': 'latest-stable',
            })
        self.assertEqual(res['status'], 'present')
        self.assertEqual(res['version'], '1.2.3')
        self.assertEqual(res['digest'], manifest_digest)

    def test_probe_github_release_and_prerelease(self):
        def fake_gh(endpoint, **kwargs):
            if endpoint.endswith('/releases/latest'):
                return {'tag_name': 'v2.0.1', 'draft': False, 'html_url': 'https://github.com/o/r/releases/tag/v2.0.1', 'assets': []}
            if '/releases?per_page=' in endpoint:
                return [{'tag_name': 'v2.0.2-nightly', 'prerelease': True, 'draft': False, 'html_url': 'https://github.com/o/r/releases/tag/v2.0.2-nightly', 'assets': [{'name': 'app.dmg'}]}]
            if '/commits/' in endpoint:
                return {'sha': 'b' * 40, 'parents': [{'sha': 'c' * 40}]}
            raise AssertionError(endpoint)

        with patch('opl_release.probes.gh', side_effect=fake_gh):
            rel = probe_channel('o/r', {'kind': 'github_release'})
            pre = probe_channel('o/r', {'kind': 'github_prerelease', 'tag_pattern': 'v.*-nightly', 'asset_pattern': '.*\\.dmg'})
        self.assertEqual((rel['version'], rel['source_commit']), ('2.0.1', 'b' * 40))
        self.assertTrue(pre['asset_present'])
        self.assertEqual(pre['tag'], 'v2.0.2-nightly')

    def test_probe_sparkle_appcast_parses_version_and_build(self):
        xml = b'''<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
          <channel><item>
            <sparkle:shortVersionString>0.9.32</sparkle:shortVersionString>
            <sparkle:version>25</sparkle:version>
            <enclosure url="https://example.invalid/app.dmg" length="10" type="application/octet-stream"/>
          </item></channel>
        </rss>'''
        with patch('opl_release.probes.gh', return_value={'assets': [{'name': 'appcast.xml'}]}), \
             patch('urllib.request.urlopen', return_value=_FakeResp(xml)):
            res = probe_channel('o/r', {'kind': 'sparkle_appcast', 'release_tag': 'macos-stable', 'asset': 'appcast.xml'})
        self.assertEqual((res['status'], res['version'], res['build']), ('present', '0.9.32', '25'))

    def test_probe_rejects_unknown_channel_kind(self):
        with self.assertRaises(ReleaseError):
            probe_channel('o/r', {'kind': 'ftp'})


if __name__ == '__main__':
    unittest.main()
