# Migration & Zero-Downtime Cutover Checklist

`opl-release` v1 ships ready for immediate read-only observation without disrupting existing `one-person-lab` daily jobs.

## Phase A — Active Now (Read-Only Parallel Validation)

- `observe-daily-releases.yml` reads existing `product-release.json` configs (normalized transparently by `opl_release.config`) and `opl-catalog/catalog-index.json`.
- `control-daily-releases.yml` schedule is gated by `vars.ENABLE_DAILY_CONTROL == 'true'` so it will not contend with legacy `one-person-lab` daily crons.

## Phase B — Prerequisites Before Switching Control

1. Complete Framework Wave 3 `opl.package.v2` migration for active Catalog packages so the pinned pre-v2 commit (`PACKAGE_PUBLISHER_TOOLING_COMMIT`) is no longer required.
2. Configure in `gaofeng21cn/opl-release`:
   - Variable: `PACKAGE_RELEASE_APP_ID`
   - Secret: `PACKAGE_RELEASE_APP_PRIVATE_KEY`
   - Set Variable `ENABLE_DAILY_CONTROL=true` when ready to hand daily candidate preparation from `one-person-lab` to `opl-release`.

## Phase C — Owner Adoption & Legacy Cleanup

1. Update caller workflows in product repositories (`opl-netfleet`, `opl-fleet-agent`, etc.) from `gaofeng21cn/one-person-lab/.github/workflows/reusable-macos-sparkle-release.yml@main` to `gaofeng21cn/opl-release/.github/workflows/reusable-macos-sparkle-release.yml@v1`.
2. Optionally add `uses: gaofeng21cn/opl-release/.github/workflows/reusable-emit-receipt.yml@v1` to emit `opl.release-receipt.v1` from asynchronous qualification jobs (e.g., macOS clean-VM verification or Cloud K8s production deploy).
3. Migrate `product-release.json` / `release-automation.json` to `.opl-release.json` incrementally (`opl_release.config` supports both during migration).
4. Once `control-daily-releases.yml` runs cleanly end-to-end, delete the legacy orchestration scripts (`scripts/package_release/`, `scripts/product-*.py`, `scripts/package-*.py`, `scripts/macos-sparkle-release/`), `contracts/opl-framework/product-release-owners.json`, and legacy daily workflows from `one-person-lab`.
