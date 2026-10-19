# Deterministic tier policies (rollout step 5)

With `governance_mode = "deterministic"`, governance is computed only from
`class.*` tags, column types and reviewed config (`access_tier_groups`,
`raw_exempt_principals`, `treatment_versions`, `column_overrides`). No model
output shapes a mask. `make generate` builds no prompt and calls no model, so
it needs no model endpoint (design decision 5): the column masks and
`gr_treatment` tags come from the tags and the mask library, the Genie config
from the Genie API import, and the rest (groups, row filters, table tags) from
the reviewed draft in `generated/`. Legacy envs are unchanged: none of this
runs for them.

Code: `governance_policies.py` (pure), `generate_abac.write_deterministic_model`,
`scripts/derive_assignments.py`, `scripts/external_mask_inventory.py`,
`scripts/policy_generations.py`, `scripts/staged_policy_apply.py`,
`scripts/staged_policy_verify.py`.

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

`uc_tables` governs and never grants. `SELECT` (with its `USE_CATALOG` and
`USE_SCHEMA`) comes only from `table_readers`, and nothing targets `account
users`; without `table_readers` GenieRails grants nothing. With no agents, the
workspace layer is skipped, so GenieRails creates nothing there (no group
assignment, entitlement, warehouse or Genie resource). See
[access.md](access.md) for `table_readers`, `acl_groups`, the live agent
coverage check that gates `CAN_RUN`, and `group_map`.

## External masks

Before every rehearse, release and maintain (and plan/apply through the
exposure preflight), `scripts/external_mask_inventory.py` stops the run when a
governed table carries a column-mask policy GenieRails didn't create
(catalog, schema or table level, from the policies API) or a governed column
carries a table-attached mask (`information_schema.column_masks`). Policies of
any retained generation are recognised by name and function. GenieRails
supports only governance it generates (design decision 9).

## Staged policy changes (rollout step 6)

A deterministic env's data_access layer is never applied in one Terraform
apply. `make apply`, `apply-governance`, `rehearse`, `release` and `maintain`
apply it through `scripts/staged_policy_apply.py`, under the env lock (reused
when the enclosing `release` or `maintain` holds it). Legacy envs are never
staged: their data_access apply is unchanged and the controller changes
nothing for them.

| Phase | What happens |
|---|---|
| `create` | new functions and policies; retained policies keep their principals; no column is retagged and nothing is removed |
| `verify-created` | the live policies, functions and treatment tags equal the render (policies API, `include_inherited` on every governed table); verify-access proves every column still gives each tier its **old** output (old tier settings) |
| `tighten`, `verify-tightened` | tier moves only: each moved policy takes the union of its old and new `TO` and the intersection of its `EXCEPT`, so nobody masked before or after is unmasked; only principals being moved can briefly hit `MULTIPLE_MASKS` |
| `switch` | columns are retagged and principals take their new values |
| `verify-switched` | live check again, and verify-access with the **new** tier settings |
| `retire`, `verify-retired`, `drop-functions` | only for a generation an **earlier** release made obsolete and a successful release then confirmed: its policies leave, then the functions only it used are dropped once no live policy calls them |
| `grant` | last, in every run: new `SELECT` from `table_readers`. Every earlier phase plans with new grants withheld (grants already applied stay), so access opens only after its protection is applied and verified; this plan may change no policy or tag |

Before each apply the hook checks its render: every tagged value has a
policy, and nobody gets two masks except principals a tier move is moving.
Each plan is refused if it replaces anything or deletes a policy or tag the
phase doesn't remove, or creates business access before the switch. The
data_access root refuses, at plan time, any change to a live deterministic mask
(removal, new principals, a new function, a retag) outside the controller's
phases (`policy_stage`), so a raw `terraform apply` can't make one in one shot.

**Journal.** `<env>/data_access/.policy_generations/journal.json`, next to the
layer's state (with `GENIERAILS_STATE_DIR` set, the same path under
`$GENIERAILS_STATE_DIR/<cloud>/<env>/records/`, so a fresh checkout sees it),
records the active generation, the one waiting to retire and
any unfinished transition with its completed phases. Writes are atomic.

- A failed or crashed run is **resumed** by re-running the same command on the
  same commit. A run whose generated config differs from the unfinished one is
  **refused** before anything is applied.
- A failure in `tighten`, `switch` or their verification **restores** the
  previous protection at once (through `tighten` again when principals may
  already be moved), so columns go back to their old value and principals;
  newly governed columns keep their new mask.
- A failure in `create`, `retire` or `drop-functions` leaves everything live;
  the journal keeps the generation waiting to retire.
- The generation a run switched away from is retired only by a **later** run,
  after the release that switched (`rehearse`, `release`, `maintain`,
  `apply`, `apply-governance`) completed and confirmed it
  (`staged_policy_apply.py confirm`). A new policy change waits until the
  previous one is confirmed.
- A state that differs from the journal (applied outside the controller, or a
  stale journal) is refused. With no journal, a state that exactly matches
  the generated config is adopted; any other is refused.
- `make policy-status ENV=<env>` prints the journal. `make policy-abandon
  ENV=<env>` drops an unfinished change (removing the policies it created),
  so a corrected config can start a new one. It abandons a change that never
  retagged a column or moved a principal, or one whose switch was **rolled
  back**: the automatic restore after a failed `switch` or `verify-switched`
  completed and was verified live (the journal's `restored` marker), the last
  applied stage is still that restore, and it puts every column on exactly
  the previous generation's value (a newly governed column keeping its new
  mask doesn't qualify) and every retained policy on its previous principals.
  A journal written before the marker existed qualifies only in the exact
  shape a verified restore left (`completed` is just `create`, the failure
  was in `switch` or `verify-switched`, no restore in progress or failed);
  abandon says when it relies on that, and every other check still runs.
  This is the way out when the config must change after a rolled-back switch
  (for example a recreated deployer or tier groups), which the unfinished
  change would otherwise refuse.

  Abandon holds the env lock from its first check to its last, and binds
  the live check to what it applies:

  1. It saves a refreshed plan of the abandon render (the previous
     generation without the change's policies). That plan may only delete
     the policies the unfinished generation created, as the journal's
     generations record them; the enforcement wait those deletes restart is
     the one other change allowed. Any other change refuses before anything
     is applied: a treatment tag update (a column retagged to the new value
     since the restore shows up as one), a policy's `TO`, `EXCEPT` or match,
     a function, a grant, or a create. Before planning, it re-runs the
     coverage check on the abandon render, as every other stage does on its
     own: removing the change's policies changes the inputs the last check
     (for example rebind's) judged, so kept business SELECT needs a pass for
     exactly the set abandon leaves live. A column that render leaves
     unmasked fails the check, and inputs that change after it (grants,
     tags, DDL) still fail the plan as stale. A standalone `make
     coverage-gate` judges the generated config, not this render, so it
     doesn't stand in.
  2. For a rolled-back switch it then runs the live check (the
     `verify-created`/`verify-switched` check: policies, `TO`/`EXCEPT`
     principals, functions and treatment tags) against the restore. A
     difference, or a check that can't run, refuses.
  3. It applies exactly the saved plan, refusing if the plan file's hash
     differs from the one recorded when it was made (the saved-plan pattern
     `make release` uses).
  4. It runs the live check on the result. If that fails, abandon says so
     (`ABANDON INCOMPLETE`), and the journal keeps the change, marked
     incomplete rather than abandoned. Put back what the check lists, then
     re-run `make policy-abandon`.

  Unity Catalog has no transaction across tags and policies, so a retag
  between step 2 and the apply can't be prevented. Step 4 catches it, and
  the change stays recorded until a check passes.
- `make policy-rebind ENV=<env>` is the step before abandon when principals
  of a rolled-back change were deleted and recreated (a sandbox SCIM cleanup,
  say). Unity Catalog then shows each deleted principal as `unknown` in the
  policies' `TO`/`EXCEPT` and drops its grants, so abandon's plan would
  re-point policies and recreate grants, which it refuses. Rebind takes the
  same eligibility as abandon of a rolled-back switch (the `restored` marker
  or the pre-marker shape; the last applied stage is still the restore), and
  the generated config may differ from the unfinished change only by
  replacing deleted principals with recreated ones: the same policies,
  conditions, functions and treatment values; in each policy's `TO` and
  `EXCEPT`, at most one principal swapped for one the env names (a tier
  group, a `raw_exempt_principals` entry or the committed deployer) that the
  change never named, the same swap everywhere; and no retained generation's
  policy naming a principal the config replaced (rebind can re-point those
  only at principals recreated under the same name). Adding an `EXCEPT` or
  dropping a `TO` refuses. It re-applies the restore render with the
  config's principals, under the env lock, in two phases:

  1. **Protection.** Every policy depends on the grants in Terraform, so
     `-target` can't leave them out. Instead rebind plans the whole render,
     then saves a plan that holds back every grant it would create
     (`held_access_grants`). That plan may only update policies' `TO` and
     `EXCEPT`, redeploy the masking functions (the same SQL hash, with no
     destroy-time drop: the drop lives in `masking_functions_drop`, which
     may only be updated in place) and restart the enforcement wait. Its
     policy updates may not loosen a live mask: a `TO` entry may leave only
     as a deleted principal (`unknown`) being replaced, an `EXCEPT` entry
     may be added only in place of a deleted one and only where the
     verified restore excepted it, and nobody the verified restore masked
     (the env's tier groups, exempt principals, deployer, and every
     principal named) may end up unmasked. A treatment tag change, a policy
     create or delete, or a change to a policy's condition or function
     refuses before anything is applied.
     Then the live check (every column on the previous generation's value,
     the policies with their new principals) and the live agent coverage
     check (every classified column of the agents' tables masked).
  2. **Access**, only after both pass: a second saved plan, after the
     coverage check on the same render, that may create exactly the grants
     the protect plan held back, nothing else. Terraform's refresh drops a
     grant a deleted principal lost from state, so the journal keeps the
     held list for a re-run, bound to a copy of the protect plan that held
     it back (its sha256, a digest, and the plan's own
     `held_access_grants`); a record that doesn't match its plan refuses.
     Then the live check again and verify-access with the previous
     generation's tier settings.

  Each plan's sha256 is recorded when it is made and re-checked before its
  apply. A failure after the first apply marks the change
  `rebind_incomplete` with the next step (`make policy-status` shows it), and
  abandon refuses until a rebind finishes; re-running rebind re-plans from
  what is live. Afterwards `make policy-abandon` plans only the change's
  policy deletes and the restarted wait. Rebind refuses in CI, even with
  `GENIERAILS_ALLOW_CI_APPLY=1`, and, in this version, on a promoted env
  (prod): a prod apply comes only from a release-plan manifest, and rebind
  doesn't make one.

  `policy_stage`, `held_access_grants` and `rebind_table_grants` belong to
  the staged controller, like the runner's `env_dir` and state-file
  variables: `scripts/terraform_layer.sh` refuses them from a caller's
  `-var` (so `APPLY_FLAGS`/`DESTROY_FLAGS` too), `TF_VAR_*`, a var file,
  the env's tfvars and `TF_CLI_ARGS*`. Only the controller's own Terraform
  calls pass, with a token derived from the env lock they run under; make's
  own `-var=policy_stage=preview` (plan) and `=destroy` (destroy) are the
  exceptions.
- `make plan` prints the phases the next apply will run, then plans the end
  state.

In `rehearse`, `apply` and `maintain` these phases run inside the applying
command. A prod release is two-phase (rollout step 9, [pipelines.md](pipelines.md#release-gating-deterministic-envs)):
`make release-plan` runs the controller's `plan --out-dir`, which saves the
plan of each remaining apply phase in order (a phase that changes nothing is
recorded and planning goes on) and stops after the first phase that changes
something, with the verifications right after it; `make release`
runs `apply-saved`, which applies exactly those saved plans
(a saved plan that changes nothing isn't applied) and runs those
verifications. It refuses when the journal, the generated config or a stage's
render moved since the plan. A change that replaces policies or moves tiers
is therefore one manifest and approval per changing phase; a release with no
policy change is one manifest. A failed tighten or switch still restores the
previous protection at once.

Hook environment (also `GENIERAILS_POLICY_PLAN_OUT`, `GENIERAILS_POLICY_PLAN_BASIS`
and `GENIERAILS_POLICY_SAVED_PLAN` for the saved-plan modes; see
`scripts/policy_generations.py`):

| Variable | Meaning |
|---|---|
| `GENIERAILS_ENV_DIR` | the env directory (required) |
| `GENIERAILS_ENV_NAME` | the env name (defaults to the directory name) |
| `GENIERAILS_POLICY_FINGERPRINT` | must equal `python3 scripts/policy_generations.py fingerprint --env-dir <env>` |
| `GENIERAILS_OLD_POLICY_FINGERPRINT` | the active generation, kept live |
| `GENIERAILS_RETIRE_FINGERPRINT` | a generation an earlier release made obsolete, kept live until `retire` |
| `GENIERAILS_POLICY_KEEP_NEW_TAGS` | `1` once a switch was attempted: newly governed columns keep their value |

Each applied generation is snapshotted in
`<env>/data_access/.policy_generations/<fingerprint>.json` with the tier
settings it was verified with, and the last applied stage in `stage.json`.
A hook refuses to run when a live generation has no snapshot.
The account tag sync never removes a live deterministic value; the caller runs
`sync-tags` and `wait-tag-policies` before `create`.

## Tag-policy values

Rehearse, release and apply in a deterministic env only **add** tag-policy
values (`sync_tag_policies.py --mode add-only`). Terraform ignores the values,
so an empty account plan doesn't prove the REST update is safe: when a live
value is no longer desired, the sync writes nothing, lists the values it would
remove and fails, pointing at the account operation. Only
`make apply-account` (CI guard, approved single-use account manifest, account
lock) runs `--mode replace`, which removes them. While any env of the checkout
(`envs/*/env.auto.tfvars`) is deterministic, `make apply ENV=account` routes
to that same manifest-gated operation, `_apply-account-for-workspace` stays
add-only even for a legacy env in that mixed checkout, explicit
`make sync-tags ENV=account TAG_SYNC_MODE=replace` refuses without the live
account-lock nonce, and `make destroy ENV=account` refuses. Legacy-only
checkouts keep `replace`, as before.

Migrating a legacy env: [legacy-migration.md](legacy-migration.md).
