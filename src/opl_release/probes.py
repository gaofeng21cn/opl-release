"""Pure protocol-level channel probes (zero product-specific branching)."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .common import ReleaseError, gh

SPARKLE_NS = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'


def probe_oci(channel: dict) -> dict:
    """Probe a public OCI registry tag (`ghcr.io/<repo>:<tag>`) without product-specific logic."""
    registry = channel.get('registry', 'ghcr.io')
    oci_repo = channel.get('repository', '')
    tag = channel.get('tag', 'latest-stable')
    if not oci_repo:
        raise ReleaseError('OCI channel requires repository')
    token_url = f'https://{registry}/token?' + urllib.parse.urlencode(
        {'service': registry, 'scope': f'repository:{oci_repo}:pull'}
    )

    def fetch(url: str, headers: dict[str, str] | None = None):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.headers, resp.read()

    try:
        _, token_body = fetch(token_url)
        token = json.loads(token_body)['token']
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': ', '.join([
                'application/vnd.oci.image.manifest.v1+json',
                'application/vnd.docker.distribution.manifest.v2+json',
            ]),
        }
        meta, manifest_bytes = fetch(f'https://{registry}/v2/{oci_repo}/manifests/{urllib.parse.quote(tag, safe="")}', headers)
        digest = meta.get('Docker-Content-Digest', '')
        computed = 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest()
        if digest and digest != computed:
            raise ReleaseError('OCI manifest digest differs from registry header')
        manifest = json.loads(manifest_bytes)
        annotations = manifest.get('annotations') or {}
        result = {
            'status': 'present',
            'kind': 'oci',
            'ref': f'{registry}/{oci_repo}:{tag}',
            'digest': digest or computed,
            'version': annotations.get('org.opencontainers.image.version'),
            'source_commit': annotations.get('org.opencontainers.image.revision'),
        }
        # Inspect optional OPL package payload layer when present without assuming package layout.
        payload_layers = [
            layer for layer in manifest.get('layers', [])
            if layer.get('mediaType') == 'application/vnd.onepersonlab.package.payload.v1+json'
        ]
        if len(payload_layers) == 1:
            layer = payload_layers[0]
            _, payload_bytes = fetch(f"https://{registry}/v2/{oci_repo}/blobs/{layer['digest']}", headers)
            if 'sha256:' + hashlib.sha256(payload_bytes).hexdigest() != layer['digest']:
                raise ReleaseError('OCI payload layer digest mismatch')
            payload = json.loads(payload_bytes)
            result['version'] = payload.get('package_version') or result['version']
            result['source_commit'] = payload.get('source_commit') or result['source_commit']
        return result
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {'status': 'absent', 'kind': 'oci', 'ref': f'{registry}/{oci_repo}:{tag}', 'digest': None}
        if exc.code in (401, 403):
            return {'status': 'unauthorized', 'kind': 'oci', 'ref': f'{registry}/{oci_repo}:{tag}', 'digest': None}
        raise ReleaseError(f'OCI probe failed (HTTP {exc.code}) for {oci_repo}') from exc


def _resolve_tag_commit(repository: str, tag: str) -> dict:
    commit = gh(f'repos/{repository}/commits/{urllib.parse.quote(tag, safe="")}')
    return {
        'sha': commit['sha'],
        'parents': [p['sha'] for p in commit.get('parents', [])],
    }


def _extract_asset(release: dict, channel: dict) -> dict:
    pattern = channel.get('asset_pattern')
    if not pattern:
        return {}
    asset = next((entry for entry in release.get('assets', []) if re.fullmatch(pattern, entry['name'])), None)
    return {
        'asset_present': asset is not None,
        **({'asset': asset['name']} if asset else {}),
    }


def _version_from_tag(tag: str, tag_prefix: str = 'v') -> str:
    if tag_prefix and tag.startswith(tag_prefix):
        return tag[len(tag_prefix):]
    return tag.lstrip('v')


def probe_github_release(repository: str, channel: dict, *, tag_prefix: str = 'v') -> dict:
    try:
        release = gh(f'repos/{repository}/releases/latest')
    except ReleaseError as exc:
        if 'HTTP 404' in str(exc):
            return {'status': 'absent', 'kind': 'github_release'}
        raise
    if not release or release.get('draft'):
        return {'status': 'absent', 'kind': 'github_release'}
    tag = release['tag_name']
    commit = _resolve_tag_commit(repository, tag)
    return {
        'status': 'present',
        'kind': 'github_release',
        'tag': tag,
        'version': _version_from_tag(tag, tag_prefix),
        'source_commit': commit['sha'],
        'parent_commits': commit['parents'],
        'url': release['html_url'],
        **_extract_asset(release, channel),
    }


def probe_github_prerelease(repository: str, channel: dict, *, tag_prefix: str = 'v') -> dict:
    try:
        releases = gh(f'repos/{repository}/releases?per_page=100')
    except ReleaseError as exc:
        if 'HTTP 404' in str(exc):
            return {'status': 'absent', 'kind': 'github_prerelease'}
        raise
    pattern = channel.get('tag_pattern', '.*')
    selected = next(
        (
            item for item in releases
            if item.get('prerelease') and not item.get('draft') and re.fullmatch(pattern, item.get('tag_name', ''))
        ),
        None,
    )
    if not selected:
        return {'status': 'absent', 'kind': 'github_prerelease'}
    tag = selected['tag_name']
    commit = _resolve_tag_commit(repository, tag)
    return {
        'status': 'present',
        'kind': 'github_prerelease',
        'tag': tag,
        'version': _version_from_tag(tag, tag_prefix),
        'source_commit': commit['sha'],
        'parent_commits': commit['parents'],
        'url': selected['html_url'],
        **_extract_asset(selected, channel),
    }


def probe_sparkle_appcast(repository: str, channel: dict) -> dict:
    release_tag = channel.get('release_tag', 'macos-stable')
    asset_name = channel.get('asset', 'appcast.xml')
    encoded_tag = urllib.parse.quote(release_tag, safe='')
    try:
        release = gh(f'repos/{repository}/releases/tags/{encoded_tag}')
    except ReleaseError as exc:
        if 'HTTP 404' in str(exc):
            return {'status': 'absent', 'kind': 'sparkle_appcast'}
        raise
    asset = next((entry for entry in release.get('assets', []) if entry['name'] == asset_name), None)
    if not asset:
        return {'status': 'absent', 'kind': 'sparkle_appcast'}
    url = f'https://github.com/{repository}/releases/download/{encoded_tag}/{urllib.parse.quote(asset_name, safe="")}'
    with urllib.request.urlopen(url, timeout=20) as resp:
        feed = ET.fromstring(resp.read(262144))
    item = feed.find('./channel/item')
    enclosure = item.find('enclosure') if item is not None else None
    if enclosure is None:
        raise ReleaseError('Sparkle appcast has no enclosure item')
    version = item.findtext(SPARKLE_NS + 'shortVersionString') or enclosure.attrib.get(SPARKLE_NS + 'shortVersionString')
    build = item.findtext(SPARKLE_NS + 'version') or enclosure.attrib.get(SPARKLE_NS + 'version')
    return {
        'status': 'present',
        'kind': 'sparkle_appcast',
        'version': version,
        'build': build,
        'url': url,
    }


def probe_channel(repository: str, channel: dict, *, tag_prefix: str = 'v') -> dict:
    """Dispatch channel readback purely on `channel['kind']`."""
    kind = channel['kind']
    if kind == 'oci':
        return probe_oci(channel)
    if kind == 'github_release':
        return probe_github_release(repository, channel, tag_prefix=tag_prefix)
    if kind == 'github_prerelease':
        return probe_github_prerelease(repository, channel, tag_prefix=tag_prefix)
    if kind == 'sparkle_appcast':
        return probe_sparkle_appcast(repository, channel)
    raise ReleaseError(f'Unsupported channel protocol: {kind!r}')
