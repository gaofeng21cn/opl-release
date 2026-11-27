# OPL Release Architecture

## Three Orthogonal Responsibilities

1. **`one-person-lab` (OPL Framework v2)**: Owns the local runtime (`opld`, `opl.db`, Pack/Workspace/Connect/Runway/Ledger) and its own `publish-framework.yml`. It is a peer product inside `fleet.json`, not the family DevOps controller.
2. **`opl-catalog` (Software Catalog Trust Root)**: Owns consumer-facing package metadata (`catalog-index.json`, descriptors, allowlists, signing public keys, and SLSA build provenance). It carries zero cross-repo write secrets.
3. **`opl-release` (Horizontal Release Platform)**: Owns unified release configuration (`opl.release.v1`), standard lane receipts (`opl.release-receipt.v1`), reusable release workflows, candidate freeze/resume automation, and read-only fleet observation.

## Zero-Hardcoding Protocol Model

`opl_release.observe` never branches on `subject_id` or `product_id`. Every lane resolves through two uniform abstractions:

1. **Standard Receipt (`opl.release-receipt.v1`)**: Any owner workflow (macOS candidate build, OpenWrt build, Cloud production deployment, clean-VM qualification) emits a uniform `release-receipt.json` artifact named `opl-release-receipt-<subject_id>-<lane>-<run_id>`.
2. **Protocol Probes (`opl_release.probes`)**: Channel verification dispatches strictly on `channel.kind`:
   - `oci`: GHCR anonymous/token OCI manifest + digest check (`latest-stable` or declared tag)
   - `github_release`: GitHub `/releases/latest` + tag commit + optional `asset_pattern`
   - `github_prerelease`: GitHub `/releases` matching `tag_pattern` + optional `asset_pattern`
   - `sparkle_appcast`: GitHub release tag asset `appcast.xml` parsed for Sparkle `shortVersionString` and `version`

## Security Boundary

- **Read-only Observer (`observe-daily-releases.yml`)**: Requires only `contents: read` and `actions: read`. Holds zero GitHub App private keys, zero Apple certificates, and zero GHCR write scopes.
- **Control Plane (`control-daily-releases.yml`)**: Isolated to `release-control` environment / `ENABLE_DAILY_CONTROL` opt-in guard. Private package repos execute `reusable-publish-package-v2.yml` from inside their own repositories with their own `GITHUB_TOKEN` and signing environments.
