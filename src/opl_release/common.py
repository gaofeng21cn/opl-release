"""Shared process, Git, GitHub API, and GitHub App token utilities."""
from __future__ import annotations

import atexit
import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class ReleaseError(RuntimeError):
    """Typed failure raised when a release contract or remote readback fails."""


_DEFAULT_PERMISSIONS = {
    'contents': 'write',
    'actions': 'write',
    'pull_requests': 'write',
    'workflows': 'write',
    'checks': 'read',
    'statuses': 'read',
}

_TOKEN_STATE: dict[str, object] = {
    'app_id': None,
    'private_key': None,
    'owner': None,
    'permissions': dict(_DEFAULT_PERMISSIONS),
    'minted_at': None,
    'refresh_after_seconds': 2700,
    'self_minted_tokens': [],
    'atexit_registered': False,
}


def reset_token_refresher() -> None:
    _TOKEN_STATE.update(
        app_id=None,
        private_key=None,
        owner=None,
        permissions=dict(_DEFAULT_PERMISSIONS),
        minted_at=None,
        refresh_after_seconds=2700,
        self_minted_tokens=[],
    )


def is_token_refresh_configured() -> bool:
    return bool(_TOKEN_STATE['app_id'] and _TOKEN_STATE['private_key'] and _TOKEN_STATE['owner'])


def _revoke_self_minted_tokens() -> None:
    tokens = list(_TOKEN_STATE.get('self_minted_tokens') or [])
    _TOKEN_STATE['self_minted_tokens'] = []
    for token in tokens:
        try:
            req = urllib.request.Request(
                'https://api.github.com/installation/token',
                method='DELETE',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Accept': 'application/vnd.github+json',
                    'X-GitHub-Api-Version': '2022-11-28',
                },
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _sign_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    ts = int(time.time()) if now is None else int(now)
    header = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode('utf-8'))
    payload = _b64url(json.dumps({'iat': ts - 60, 'exp': ts + 540, 'iss': str(app_id)}, separators=(',', ':')).encode('utf-8'))
    signing_input = f'{header}.{payload}'
    script = (
        "const fs = require('node:fs');"
        "const crypto = require('node:crypto');"
        "const key = fs.readFileSync(0, 'utf8');"
        "const signer = crypto.createSign('RSA-SHA256');"
        "signer.update(process.argv[1]);"
        "process.stdout.write(signer.sign(key).toString('base64url'));"
    )
    res = subprocess.run(
        ['node', '-e', script, signing_input],
        input=private_key_pem,
        text=True,
        capture_output=True,
        timeout=15,
        check=True,
    )
    return f'{signing_input}.{res.stdout.strip()}'


def _github_json_request(url: str, *, bearer: str, method: str = 'GET', payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {bearer}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    if body is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read().decode('utf-8')
        return json.loads(raw) if raw.strip() else {}


def mint_installation_token(
    app_id: str,
    private_key_pem: str,
    owner: str,
    permissions: dict[str, str] | None = None,
) -> str:
    jwt_token = _sign_app_jwt(app_id, private_key_pem)
    installation = None
    for endpoint in (
        f'https://api.github.com/users/{owner}/installation',
        f'https://api.github.com/orgs/{owner}/installation',
    ):
        try:
            installation = _github_json_request(endpoint, bearer=jwt_token)
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    if not installation or 'id' not in installation:
        raise RuntimeError(f'Could not resolve GitHub App installation for {owner}')
    created = _github_json_request(
        f'https://api.github.com/app/installations/{installation["id"]}/access_tokens',
        bearer=jwt_token,
        method='POST',
        payload={'permissions': permissions or dict(_DEFAULT_PERMISSIONS)},
    )
    token = created.get('token')
    if not isinstance(token, str) or not token:
        raise RuntimeError('GitHub App access_tokens response omitted token')
    return token


def configure_token_refresh_from_env(*, refresh_after_seconds: int = 2700) -> bool:
    app_id = os.environ.get('PACKAGE_RELEASE_APP_ID', '').strip()
    private_key = os.environ.pop('PACKAGE_RELEASE_APP_PRIVATE_KEY', '').strip()
    owner = (
        os.environ.get('PACKAGE_RELEASE_APP_OWNER', '').strip()
        or os.environ.get('GITHUB_REPOSITORY_OWNER', '').strip()
    )
    if not app_id or not private_key or not owner:
        return False
    _TOKEN_STATE.update(
        app_id=app_id,
        private_key=private_key,
        owner=owner,
        refresh_after_seconds=refresh_after_seconds,
        minted_at=time.monotonic() if os.environ.get('GH_TOKEN') else None,
    )
    if not _TOKEN_STATE['atexit_registered']:
        atexit.register(_revoke_self_minted_tokens)
        _TOKEN_STATE['atexit_registered'] = True
    return True


def ensure_fresh_token(*, force: bool = False) -> str | None:
    if not is_token_refresh_configured():
        return None
    minted_at = _TOKEN_STATE['minted_at']
    threshold = int(_TOKEN_STATE['refresh_after_seconds'])
    if not force and minted_at is not None and (time.monotonic() - float(minted_at)) < threshold:
        return None
    token = mint_installation_token(
        str(_TOKEN_STATE['app_id']),
        str(_TOKEN_STATE['private_key']),
        str(_TOKEN_STATE['owner']),
        dict(_TOKEN_STATE['permissions']),  # type: ignore[arg-type]
    )
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print(f'::add-mask::{token}', flush=True)
    os.environ['GH_TOKEN'] = token
    _TOKEN_STATE['minted_at'] = time.monotonic()
    tokens = _TOKEN_STATE['self_minted_tokens']
    if isinstance(tokens, list):
        tokens.append(token)
    return token


def _child_env() -> dict[str, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1')
    env.pop('PACKAGE_RELEASE_APP_PRIVATE_KEY', None)
    return env


def run(argv: list[str] | tuple[str, ...], *, cwd: Path | str | None = None, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    ensure_fresh_token()
    env = _child_env()
    if env.get('GH_TOKEN'):
        env.update(
            GIT_CONFIG_COUNT='1',
            GIT_CONFIG_KEY_0='credential.helper',
            GIT_CONFIG_VALUE_0='!gh auth git-credential',
        )
    result = subprocess.run(
        list(map(str, argv)),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode:
        raise ReleaseError(f"{argv[0]} failed: {(result.stderr or result.stdout)[-3000:]}")
    return result


def gh(endpoint: str, *, method: str = 'GET', payload: dict | None = None, timeout: int = 20):
    for attempt in range(2):
        ensure_fresh_token()
        argv = ['gh', 'api', '--method', method, endpoint]
        if payload is not None:
            result = subprocess.run(
                argv + ['--input', '-'],
                input=json.dumps(payload),
                env=_child_env(),
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode:
                detail = result.stderr[-1000:]
                if attempt == 0 and ('HTTP 401' in detail or 'Bad credentials' in detail) and is_token_refresh_configured():
                    ensure_fresh_token(force=True)
                    continue
                raise ReleaseError(f'GitHub {method} {endpoint}: {detail}')
        else:
            try:
                result = run(argv, timeout=timeout)
            except ReleaseError as exc:
                detail = str(exc)
                if attempt == 0 and ('HTTP 401' in detail or 'Bad credentials' in detail) and is_token_refresh_configured():
                    ensure_fresh_token(force=True)
                    continue
                raise
        return json.loads(result.stdout) if result.stdout.strip() else None


def read_json(path: Path | str):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path: Path | str, value) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def git(root: Path | str, *args: str) -> str:
    return run(['git', *args], cwd=root).stdout.strip()


def safe_path(root: Path | str, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or '..' in candidate.parts or '\\' in str(candidate):
        raise ReleaseError(f'Path must remain relative to owner checkout: {relative}')
    root_resolved = Path(root).resolve()
    target = Path(root) / candidate
    if not target.resolve().is_relative_to(root_resolved) or target.is_symlink():
        raise ReleaseError(f'Path escapes owner checkout: {relative}')
    return target


def validate_pathspec(root: Path | str, spec: str) -> None:
    """Validate a release path or Git exclude pathspec safely within the repo."""
    if not isinstance(spec, str) or not spec:
        raise ReleaseError('Release path entry must be a non-empty string')
    if spec.startswith(':'):
        match = re.fullmatch(r':\(exclude(?:,glob)?\)([^\\\0]+)', spec)
        if not match or '..' in Path(match[1]).parts or match[1].startswith('/'):
            raise ReleaseError(f'Invalid exclude pathspec: {spec}')
        return
    safe_path(root, spec)


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ReleaseError(f'Expected stable SemVer: {value!r}')
    return tuple(map(int, value.split('.')))  # type: ignore[return-value]
