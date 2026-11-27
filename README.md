# OPL Release (`gaofeng21cn/opl-release`)

Unified horizontal release platform, reusable GitHub Actions workflows, and zero-secret read-only fleet observer for One Person Lab products and packages.

## Separation of Duties

| Repository | Role | Authority |
| --- | --- | --- |
| [`one-person-lab`](https://github.com/gaofeng21cn/one-person-lab) | OPL Framework v2 runtime | Local `opld`, `opl.db`, Pack/Workspace/Connect/Runway/Ledger, and its own `publish-framework.yml` |
| [`opl-catalog`](https://github.com/gaofeng21cn/opl-catalog) | Read-only software catalog | Static consumer package index (`catalog-index.json`), allowlists, public keys, SLSA provenance attestation |
| [`opl-release`](https://github.com/gaofeng21cn/opl-release) | Horizontal release platform | Unified `.opl-release.json`, standard `opl.release-receipt.v1`, reusable workflows, candidate control, and read-only fleet dashboard |

## Core Principles

1. **Zero product hardcoding**: Channel probes and lane summaries dispatch purely by protocol (`oci`, `github_release`, `github_prerelease`, `sparkle_appcast`) and standard `opl.release-receipt.v1` artifacts—never by `product_id`.
2. **Control vs. Observation isolation**: Only `control-daily-releases.yml` uses scoped repository write credentials; `observe-daily-releases.yml` is strictly read-only (`contents: read`, `actions: read`) with zero secrets.
3. **Consume released CLI & Catalog**: `opl.package.v2` publication uses the released `opl` CLI rather than checking out Framework internal source trees or pinning pre-v2 tooling commits.

## Quick CLI Usage

```bash
PYTHONPATH=src python3 -m opl_release validate --fleet fleet.json
PYTHONPATH=src python3 -m opl_release observe --fleet fleet.json --output fleet-report.json
PYTHONPATH=src python3 -m opl_release emit-receipt --subject-id opl-netfleet --lane macos-candidate --stage candidate_built --version 0.9.32 --tag v0.9.32 --output release-receipt.json
```

See [docs/architecture.md](docs/architecture.md) and [docs/migration-cutover.md](docs/migration-cutover.md).
