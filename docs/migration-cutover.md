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

## Switching an Owner Schedule

1. Land the owner receiver and its `daily_dispatch: true` declaration. Keep its existing schedule active while verifying the receiver contract. Central dispatch is off for subjects without `enable_owner_dispatch: true` in `fleet.json`. Existing `daily_patch` preparation continues independently.
2. In the interval between daily runs, coordinate the owner schedule switch with adding `enable_owner_dispatch: true` to that subject in `fleet.json`. Remove `observe_only` for that subject if set. Confirm both changes before either scheduler's next run.
3. Verify the next central schedule's `opl-release-dispatch-<subject>-<run-id>-<attempt>` artifact and the linked owner run. Confirm the owner's publication or qualification from its own receipt. Restore the prior owner schedule and remove the Fleet opt-in if the handoff needs to be rolled back.
