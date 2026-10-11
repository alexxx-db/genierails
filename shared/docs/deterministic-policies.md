# Deterministic tier policies (rollout step 5)

With `governance_mode = "deterministic"`, governance is computed only from
`class.*` tags, column types and reviewed config (`access_tier_groups`,
`raw_exempt_principals`, `treatment_versions`, `column_overrides`). No model
output shapes a mask. Legacy envs are unchanged: none of this runs for them.

Code: `governance_policies.py` (pure), `generate_abac.write_deterministic_model`,
`scripts/derive_assignments.py`, `scripts/external_mask_inventory.py`,
`scripts/policy_generations.py`.

## What a governed column gets

- The strictest of its classes through the mask library (`mask_library.json`).
- Any never-raw class (`card_security_code`, `card_pin`, `card_track_data`,
  `secret`) makes the column never raw.
- Any class the library doesn't map makes it the fail-safe `unmapped_redact`:
  never raw, redacted for every tier. A `gr.classification_unmapped` marker is
  written too, so the coverage check keeps blocking new access until a
  mapping is promoted.
- Complex column types (ARRAY, MAP, STRUCT) and `tier_access_overrides` are
  refused rather than guessed.

## Names

| Object | Name |
|---|---|
| Treatment value (`gr_treatment`) | `<treatment>__<type>__<generation>`, e.g. `email_partial__string__3f9a1c2b` |
| Functions | `gr_mask_<value>_partial`, `gr_mask_<value>_full`, typed exactly for the column (`DECIMAL(10,2)` in, `DECIMAL(10,2)` out), in the catalog's first governed schema |
| Policies | `gr_mask_<catalog>_<value>_partial`, `gr_mask_<catalog>_<value>_full` |

Unity Catalog has no function overloading, so the column type is part of the
value. `<generation>` hashes the treatment, type and both SQL bodies: changing
a mask creates new names and never replaces a live function or policy.
Principals are not part of it, so a tier move updates a policy in place.

## Policies per value

| Policy | To | Except |
|---|---|---|
| partial (only with a middle tier, never for never-raw) | tier-2 groups | tier 1, `raw_exempt_principals`, deployer SP |
| full | `account users` | tiers 1 and 2, `raw_exempt_principals`, deployer SP; **nobody** for never-raw |

Every principal gets at most one mask: the most privileged tier wins, and
anyone outside the tiers gets the full version.

## Governance only

Deterministic envs create no business grant: `uc_tables` governs and never
grants (no `USE_CATALOG`, `USE_SCHEMA` or `SELECT`, so nothing targets
`account users`). With no agents, the workspace layer is skipped, so
GenieRails creates nothing there (no group assignment, entitlement, warehouse
or Genie resource). `table_readers` grants arrive in step 8.

## External masks

Before every rehearse, release and maintain (and plan/apply through the
exposure preflight), `scripts/external_mask_inventory.py` stops the run when a
governed table carries a column-mask policy GenieRails didn't create
(catalog, schema or table level, from the policies API) or a governed column
carries a table-attached mask (`information_schema.column_masks`). Policies of
any retained generation are recognised by name and function.

## Staged-apply hooks (the interface for step 6)

`scripts/staged_policy_apply.py` runs these argv executables, in order, under
the env lock:

| Stage | Executable | Renders into `<env>/data_access` |
|---|---|---|
| `create` | `scripts/policy_hooks/create` | desired ∪ retained policies (retained keep their principals); every column keeps its **old** value; all functions |
| `switch` | `scripts/policy_hooks/switch` | desired ∪ retained policies (same-named ones take the new principals); columns retagged to the **desired** values; all functions |
| `retire` | `scripts/policy_hooks/retire` | the retire generation's policies leave; functions stay |

Each hook plans, refuses any destroy of a policy, a treatment tag or the
function-drop resource (only `retire` may delete exactly the retire
generation's policies), then applies exactly that plan. Hooks are idempotent.

Environment:

| Variable | Meaning |
|---|---|
| `GENIERAILS_ENV_DIR` | the env directory (required) |
| `GENIERAILS_ENV_NAME` | the env name (defaults to the directory name) |
| `GENIERAILS_POLICY_FINGERPRINT` | must equal `python3 scripts/policy_generations.py fingerprint --env-dir <env>` |
| `GENIERAILS_OLD_POLICY_FINGERPRINT` | the active generation, kept live |
| `GENIERAILS_RETIRE_FINGERPRINT` | a generation an earlier release made obsolete, kept live until `retire` |

Each applied generation is snapshotted in
`<env>/data_access/.policy_generations/<fingerprint>.json`, next to the
layer's state. A hook refuses to run when a live generation has no snapshot.
The account tag sync never removes a live deterministic value; the caller runs
`sync-tags` and `wait-tag-policies` before `create`.

## Tag-policy values

Rehearse, release and apply in a deterministic env only **add** tag-policy
values (`sync_tag_policies.py --mode add-only`). Terraform ignores the values,
so an empty account plan doesn't prove the REST update is safe: when a live
value is no longer desired, the sync writes nothing, lists the values it would
remove and fails, pointing at `make apply-account`. Only `make apply-account`
(CI guard, account lock) runs `--mode replace`, which removes them. While any
env of the checkout (`envs/*/env.auto.tfvars`) is deterministic,
`make apply ENV=account` runs `make apply-account`, and a bare
`make sync-tags ENV=account` is add-only. Legacy-only checkouts keep `replace`,
as before.
