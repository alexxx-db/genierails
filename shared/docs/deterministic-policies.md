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
  A string column whose classes keep different partial treatments (a
  free-text note tagged email, name and phone) gets `redact`, and a
  `gr.classification_mixed_partial` marker makes the coverage check fail if
  it ever keeps a partial version.
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

**Coverage check on a staged render.** Each hook re-runs the coverage check
on its own render. In a native-classification env (`enable_classification =
true`) the live refresh records the treatment tags derive-assignments
derived, which are the change's. The phases before the switch (`create`,
`tighten`, `restore`, `abandon`, and a rebind, judged as `restore`) keep the
active generation's values live instead. For those phases the check accepts
a render whose tags differ from the refresh only when all of these hold:

- the render is exactly the refreshed config with every treatment tag at the
  value the active generation recorded (after an attempted switch, except
  for abandon, newly governed columns keep their new value);
- the journal moves from that generation to the generated config's (for
  abandon the config may have moved on: abandon keeps that generation's
  recorded values whatever the config says now; a rebind needs the config
  to be the change with only recreated principals);
- the generation's snapshot still hashes to its fingerprint.

A legacy active generation also needs its adopted migration record
([legacy-migration.md](legacy-migration.md)). The pass records the
generation kept (`kept_from` in `data_access/.coverage_gate.json`) and the
columns the refresh governs that the phase leaves untagged
(`uncovered_columns`, for example a newly classified column before the
switch), and prints both. Those phases create no business grant, so an
untagged column gains no reader through them. A phase that can open access
(the `grant` re-check, both rebind phases) must tag every column the refresh
governs. `switch` and `retire` must match the refresh exactly. Anything else
fails closed, as before.

**Journal.** `<env>/data_access/.policy_generations/journal.json`, next to the
layer's state (with `GENIERAILS_STATE_DIR` set, the same path under
`$GENIERAILS_STATE_DIR/<cloud>/<env>/records/`, so a fresh checkout sees it),
records the active generation, the one waiting to retire and
any unfinished transition with its completed phases. Writes are atomic.

- A failed or crashed run is **resumed** by re-running the same command on the
  same commit. A run whose generated config differs from the unfinished one is
  **refused** before anything is applied (`make apply`, `rehearse`,
  `release-plan`, and `release MANIFEST=` of the change's own manifest). When
  abandon can drop the change, the refusal says so: "run `make
  policy-abandon ENV=<env>`, then re-run".
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
  the live check to what it applies. Under the lock, make first re-reads
  live tags and DDL (`derive-assignments`, or the DDL alone, as `rehearse`
  does), which records the live refresh the coverage check needs, then
  re-splits the result into `data_access/` (`promote`, as `make plan`
  does). A failed refresh stops abandon before it plans anything. The render
  takes its non-treatment tag assignments from the split, while the refresh
  records the generated config's. The re-split normally makes them agree, so
  a split edited or left stale before abandon is reconciled, not refused;
  abandon refuses before it plans, naming the entries, only when they still
  differ after the re-split (or when `staged_policy_apply.py abandon` runs
  without make's re-split).

  Abandon also works after the generated config **drifted** from the change
  (live `class.*` tags changed after it started, or its treatment settings
  were edited): the abandon render is built from the journal's snapshots of
  the change and the previous generation, not from the config, so drift
  changes nothing it deletes, keeps or checks. It then prints the drifted
  columns; the next apply stages them as a new change from the previous
  generation. With drift, abandon first checks that the previous
  generation's snapshot still hashes to its fingerprint, and refuses before
  any plan, naming the file, when it is edited or unreadable.

  1. It saves a refreshed plan of the abandon render (the previous
     generation without the change's policies). That plan may only delete
     the policies the unfinished generation created, as the journal's
     generations record them; the enforcement wait those deletes restart is
     the one other change allowed, besides the one-time masking re-key
     (below). Any other change refuses before anything
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
  config's principals, under the env lock, in two phases. Like abandon, it
  first re-reads live tags and DDL under the lock for the access phase's
  coverage check and re-splits them; a failed refresh stops rebind before it
  plans anything, and so does a split whose non-treatment assignments still
  differ from the generated config's after that re-split. A generated config whose treatment values
  differ from the change's (live `class.*` tags changed since it started,
  or its treatment settings were edited) refuses before any plan and names
  the columns.

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

### Changes that can't be dropped

Abandon and rebind never untag or retag a column, so one shape has no
GenieRails way out yet. It refuses before anything is applied, names the
columns, and leaves live protection as it is:

- **A rolled-back switch that kept newly governed columns.** The restore
  leaves a column the change newly governs on its new value, masked by the
  change's policies. Abandon would delete those policies, so abandon and
  rebind both refuse ("N column(s) keep the new generation's treatment").
  The way out is to resume the change: re-run the command that started it
  with the generated config on that generation. If principals it names were
  deleted and recreated under new IDs, the config can't be that generation
  again, and no command can finish or drop the change yet.
How to un-govern such columns, or supersede the change, is an open design
decision.

**Live `class.*` tags changed after the change started** (or its treatment
settings were edited) is not one of these shapes. Resume refuses ("N
column(s) have another treatment in the generated config than in generation
..."), naming the columns, because the config is no longer the change; when
abandon can drop it, the refusal says "run `make policy-abandon ENV=<env>`,
then re-run". Abandon drops it like any other change it may drop (above).
A rebind still refuses, since it only re-points principals: put the tags
back and rebind, or, if no principal was deleted, abandon and re-run.

### Masking functions and checkout paths

`terraform_data.masking_functions` is replaced, which re-runs `CREATE OR
REPLACE FUNCTION` and drops nothing, only when what it deploys changes: the
normalized SQL hash, the warehouse, the workspace host or the deployer's
client ID. It records no file paths, so a plan from another checkout of the
same state (a teammate's clone, a CI runner, a moved repo,
`GENIERAILS_STATE_DIR`) changes nothing. Edits to
`deploy_masking_functions.py` itself don't replace it either; they apply on
the next SQL change.

`terraform_data.masking_functions_drop`, whose destroy-time provisioner drops
the functions, records its SQL and auth files relative to the env dir and its
script relative to `shared/`. The destroy resolves them in the checkout that
runs it: the script under that run's module, the files under
`GENIERAILS_MASKING_BASE_DIR`, which `scripts/terraform_layer.sh` exports (the
env dir). A raw `terraform destroy` without that variable fails before
dropping anything rather than guess; run it through `make destroy`. Only a
real destroy drops the functions; a path change is never even an update.

The drop also records the normalized SQL hash. Before connecting, the
destroy hashes the SQL it found and refuses if the hash differs: that SQL
belongs to another env (a wrong `GENIERAILS_MASKING_BASE_DIR`) or has
changed since the last apply. Restore the SQL, or apply it, then destroy.
All state from an earlier version records absolute paths and no hash, so
its destroy refuses before it loads credentials or connects
(`masking_functions_drop records no sql_hash ...`). It refuses whether those
paths still exist or not: a reused or replaced checkout can hold another
env's SQL at the same path. Run `make apply` once from the current checkout,
then `make destroy`. That apply is the re-key below. It records relative
paths and the hash, and it drops nothing.

**Once, after upgrading**, state written by an earlier version still records
absolute paths, so the first data_access plan shows:

```
# module.data_access.terraform_data.masking_functions must be replaced
      ~ triggers_replace = {
          - auth_file    = "/old/checkout/.../auth.auto.tfvars"
          - script       = "/old/checkout/shared/deploy_masking_functions.py"
          - sql_file     = "/old/checkout/.../masking_functions.sql"
            # (4 unchanged attributes hidden)
        }
# module.data_access.terraform_data.masking_functions_drop will be updated in-place
# module.data_access.time_sleep.wait_for_policy_enforcement must be replaced
```

followed by `Note: this replacement only stops recording checkout paths`. That
is a re-key: the same functions are redeployed on the same warehouse and
nothing is dropped. It is safe to apply, including in prod. The staged
controller treats it as one: abandon allows it (`masking_rekey` in
`scripts/policy_generations.py`) and every stage prints `Policy stage <stage>:
terraform_data.masking_functions is replaced only to stop recording checkout
paths ...`. Rebind already allows a redeploy with the same SQL hash. It is
only a re-key when `sql_hash`, `warehouse_id`, `host` and `client_id` are all
recorded and unchanged and the only other changes are the path keys. The
drop's update must keep its warehouse and host, and no stage may delete or
replace `masking_functions_drop`. Anything else is refused as before. After
that one apply, plans from any checkout show no masking change.

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
policy change is one manifest. The workspace (Genie CAN_RUN) is planned with
the last phase, except when that phase's `grant` opens `SELECT` the live agent
coverage check still lacks (a first release): then the workspace waits for one
more manifest, planned once that `SELECT` is live, so CAN_RUN never opens before
its masks and `SELECT`. A failed tighten or switch still restores the previous
protection at once.

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

The account tag policy (for example `gr_treatment`) is shared by every
workspace on the metastore, so its live values can include values another
GenieRails repo, another team or a legacy env still uses. Every tag sync, in
every governance mode (rehearse, release, apply, `make sync-tags`, and a
legacy-only checkout's `make apply ENV=account`), only **adds** values
(`sync_tag_policies.py` has no replace mode): it adds the values this checkout
needs and leave every other live value in place, printing them as a warning.
Terraform ignores the values, so an empty account plan doesn't prove a REST
update is safe; add-only never writes a removal, so it never needs one.

Removing a value is a separate, approved account operation:

```bash
make account-plan ALLOW_TAG_VALUE_REMOVAL=gr_treatment:redact,gr_treatment:ssn_last4 WAREHOUSE_ID=<id>
# review the printed summary ("!! TAG VALUES THIS MANIFEST REMOVES ...") and approve the manifest
make apply-account
```

- Only values named in `ALLOW_TAG_VALUE_REMOVAL` (`<key>:<value>`, comma
  separated; sorted and deduplicated) are removed. Each must be live, no longer
  desired by the account `abac.auto.tfvars`, and not a deterministic
  generation (the staged retirement removes those).
- Each must be unused where the deployer can see: no assignment in
  `system.information_schema.{catalog,schema,table,column,volume}_tags`
  (every catalog the deployer can read, through `WAREHOUSE_ID`), and no ABAC
  policy whose condition names the key and value on any catalog, schema or
  table the deployer can list. Every table is read, not only tagged ones: a
  table policy can name a value no securable carries yet. Unbound and
  browse-only catalogs are listed too. A value in use is refused, and so is a
  value whose use can't be checked (no warehouse, a failed or truncated query,
  a catalog, schema, table or policy listing that fails anywhere): no manifest
  is written.
- The manifest records the allowed list, the planned removals and what was
  checked; `account-plan` and `apply-account` print them first. Approving the
  manifest approves exactly those removals.
- `apply-account` recomputes the diff and refuses if the planned additions or
  removals no longer match live, or if `ALLOW_TAG_VALUE_REMOVAL` is set to a
  different list. Under the account lock it re-checks usage just before
  removing. Live values that appear later and aren't named are left in place
  and don't invalidate the manifest.

Limits of the check: it sees only what the deployer can list. A securable it
can't list at all isn't inspected; one it lists but can't read refuses. Name a
value only when no other team depends on it.

Terraform ignores the values, but deleting a tag policy drops all of them, with
no usage check. So `make account-plan` and a legacy-only checkout's direct
`make apply ENV=account` refuse a plan that deletes or replaces a
`databricks_tag_policy`: keep the key in `tag_policies` and remove the values
you no longer use as above. `wait-tag-policies` never deletes a key it can't
see; it only creates keys that don't exist.

`make sync-tags TAG_SYNC_MODE=replace` refuses in every checkout (it would
remove every undesired value with no usage check). While any env of the
checkout (`envs/*/env.auto.tfvars`) is deterministic, `make apply ENV=account`
routes to `apply-account` and `make destroy ENV=account` refuses. A legacy-only
checkout's `make apply ENV=account` still applies the account layer directly,
but its tag sync is add-only like every other; it removes values through
`account-plan` and `apply-account` too.

Migrating a legacy env: [legacy-migration.md](legacy-migration.md).
