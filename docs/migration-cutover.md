# Migration & Zero-Downtime Cutover Checklist

`opl-release` `@v1` serves as the horizontal release control plane and unified fleet observer for the OPL family.

## Phase A — Completed (Central Control & Read-Only Fleet Observer)

- `observe-daily-releases.yml` executes read-only observation across all `fleet.json` products (normalizing `.opl-release.json` and `product-release.json` via `opl_release.config`) and `opl-catalog/catalog-index.json`.
- `control-daily-releases.yml` is enabled on `main` (`ENABLE_DAILY_CONTROL=true`, `PACKAGE_RELEASE_APP_ID` + `PACKAGE_RELEASE_APP_PRIVATE_KEY`) and prepares `daily_patch` subjects cleanly without overriding `owner_schedule` products.

## Phase B — Completed (Catalog Wave 3 & Framework Cleanup)

1. All 22 admitted `opl-catalog` packages completed migration to `opl.package.v2`.
2. Removed pre-v2 `PACKAGE_PUBLISHER_TOOLING_COMMIT` pins, `.package-publisher` checkouts, `opl-flow` reverse clones, `scripts/package-carrier-source-paths.mjs`, and legacy v1 package fallbacks from `one-person-lab`.
3. `one-person-lab` retains `.github/workflows/publish-package.yml` and `daily-package-channel*.yml` strictly as the GHCR-bound publisher for `one-person-lab-packages/*`, and emits `opl.release-receipt.v1` artifacts (`opl-release-receipt-<package_id>-oci-<run_id>`) for public OCI publications.

## Phase C — Completed Owner Adoption & Active Cutover

1. Product Sparkle pipelines (`opl-fleet-agent`, `opl-codex-models`, `opl-netfleet`) and asynchronous qualification/deploy receipts (`one-person-lab-cloud`, `opl-netfleet`) use `gaofeng21cn/opl-release` `@v1` workflows and `opl.release-receipt.v1`.
2. Central daily schedule cutover (`OPL_RELEASE_CENTRAL_DAILY=true` in owner repositories paired with `enable_owner_dispatch: true` and no `observe_only` in `fleet.json`) is active for `opl-framework` (`daily-package-channel.yml`), `opl-app` (`release-nightly.yml`), and `opl-dsh` (`stable-release.yml`).
3. Legacy cross-repo orchestration workflows (`daily-product-channel.yml`, `reusable-macos-sparkle-release.yml`), `contracts/opl-framework/product-release-owners.json`, and `scripts/product-*.py` / `scripts/package_release/product_status.py` are retired from `one-person-lab`.

## Switching Additional Owner Schedules

1. Land the owner receiver (`opl_release_control_run_id`, `[opl-release:<id>]` run-name, and `vars.OPL_RELEASE_CENTRAL_DAILY != 'true'` schedule guard) alongside its `daily_dispatch: true` declaration. Keep its local schedule active while verifying the receiver contract. Central dispatch stays off until `enable_owner_dispatch: true` is set in `fleet.json`.
2. Between daily runs, set `enable_owner_dispatch: true` (removing `observe_only` if present) in `fleet.json` and enable `OPL_RELEASE_CENTRAL_DAILY=true` in the owner repository.
3. Verify the next central schedule's `opl-release-dispatch-<subject>-<run-id>-<attempt>` artifact, the linked owner workflow run, and its `opl.release-receipt.v1` / published channel readback. To roll back, unset `OPL_RELEASE_CENTRAL_DAILY` and remove `enable_owner_dispatch` in `fleet.json`.
