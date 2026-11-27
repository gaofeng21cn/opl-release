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

## Daily Owner Dispatch

An owner can declare `daily_dispatch: true` on one lane in `.opl-release.json`. For legacy `product-release.json`, put the field on the primary publisher declaration at the top level or in one `observed_publishers` entry. The lane's `workflow` selects the receiver. This setting is independent of the product's version preparation strategy and primary publication lane. During cutover, a subject requires `enable_owner_dispatch: true` in `fleet.json` to enter the dispatch matrix. Fleet entries marked `observe_only` are excluded. See the [migration sequence](migration-cutover.md#switching-an-owner-schedule).

On the central GitHub schedule, a separate job dispatches the owner workflow on `main` with the single string input `opl_release_control_run_id`. The owner receiver includes `[opl-release:<id>]` in its run name and verifies the scoped `opl-package-publisher[bot]` actor (ID `356454848`) and the original scheduled controller run before applying its own release rules.

The controller checks for the matching owner run before issuing a POST. It verifies the same workflow, ref, dispatch event, actor and exact marker after the POST. If GitHub returns an empty response or a transport error, it checks existing runs. Reruns of the controller only reconcile the existing request. Each declared lane produces a dispatch receipt, including an `unknown` result when GitHub cannot confirm the run. Publication remains with the owner workflow.

The observation report displays dispatch evidence for enabled subjects from the most recent scheduled controller run on `main`. It distinguishes a pending dispatch, a terminal job with a missing receipt, and an unknown outcome. The report shows the receipt's original attempt when a subject's job was outside a controller rerun. Product qualification and published-channel readback remain independent.
