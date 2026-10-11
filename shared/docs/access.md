# Access in deterministic envs (rollout step 8)

In an env with `governance_mode = "deterministic"`, GenieRails grants exactly
two kinds of access, each from one setting (design decision 7):

| Access | Comes only from | Owned by |
|---|---|---|
| `SELECT` on a governed table (plus the `USE CATALOG` / `USE SCHEMA` it needs) | `table_readers` | governance, per env |
| `CAN_RUN` on a Genie agent | that agent's `acl_groups` | the agent, per env |

Agents never grant `SELECT`, and nothing is ever granted to `account users`.
Legacy envs are unchanged.

## `table_readers`

```hcl
table_readers = {
  "cat.sch.customers" = ["payments_ops", "regional_analysts"]
  "cat.sch.*"         = ["payments_ops"]   # same key syntax as uc_tables
}
```

- A table's readers are the **union** of every entry that matches it: the
  exact table and its `catalog.schema.*`. `make plan`, `make generate` and
  `make promote-to` print the resolved readers per table, and any applied
  grant the change would revoke.
- Every reader must be in `access_tier_groups` or `raw_exempt_principals`;
  anything else (and `account users`) is refused, by `make` and by Terraform.
- A table listed in `table_readers` is governed. A `catalog.schema.*` entry
  reaches every governed table in that schema.
- New `SELECT` waits for a passing coverage check, as before. Removing an
  entry revokes the `SELECT` it granted, with no check needed.
- `table_readers` is env-owned: the first `make promote-to` seeds prod from
  dev (catalogs remapped); later promotions keep prod's value and print how
  it differs from dev.
- No `table_readers` means GenieRails grants no `SELECT`.

## `acl_groups` and the agent check

`acl_groups` on each agent are the only source of `CAN_RUN` (a drafted or
derived ACL in `generated/` never counts). An agent without `acl_groups`
stops the run; `[]` means nobody. `make generate`, `make rehearse` and
`make release` refuse while any `acl_groups` group can't read one of the
agent's tables through `table_readers`, naming the table and the group.

The first time `make generate` imports an agent, it adds a `table_readers`
entry, filled with the agent's `acl_groups`, for each of the agent's tables
that no entry (exact or wildcard) covers yet. It never edits an existing
entry, and it proposes for an agent only once (recorded in
`generated/table_readers_proposed.json`), so an entry you remove stays
removed. Review the proposal before committing.

## The live agent coverage check (gate for `CAN_RUN`)

`CAN_RUN` no longer depends on the data_access layer's Terraform state.
Before every workspace plan and apply, `make` runs
`scripts/agent_coverage.py`, which checks Unity Catalog live, whoever made
the masks:

1. every classified column (`class.*` tag) of each agent's tables has an
   effective column mask (a table-attached mask, or a column-mask policy in
   effect on the table whose condition names one of the column's tags);
2. every `acl_groups` group can `SELECT` each table (directly, through a
   group it is nested in, or `account users`, with `USE CATALOG` and
   `USE SCHEMA`), or owns it.

It writes `envs/<env>/.agent_coverage.json`: a fingerprint of the agents,
groups and tables Terraform computed, the check time and a verdict per
agent. The workspace layer opens new `CAN_RUN` only for an agent that passed
in a result for exactly the current inputs and no older than
`coverage_gate_max_age` (at most 24h). A missing, failed, stale or
hand-edited result withholds new groups; removing or keeping `CAN_RUN`
always applies, and `make` exits non-zero naming what it withheld.

The check proves a mask exists on every classified column and that the
groups can read. It can't prove a mask another tool made is the right one
for each tier.

## `governance = "external"`

```hcl
governance_mode = "deterministic"
governance      = "external"   # default "genierails"
```

For agents on tables another tool governs:

- GenieRails derives no treatments, masks or policies, grants no `SELECT`
  (`table_readers` must be empty), skips the account and data_access layers
  and the external-mask refusal, and `make generate` only imports agents.
- `CAN_RUN` still opens only after the live agent coverage check passed.
- `make release` applies the agents under the env lock; `make maintain`
  re-runs the live check and fails when an agent no longer passes.

`governance` is env-owned: the first promotion seeds it, later promotions
keep the destination's.

## Upgrading an existing deterministic env

Before this step a deterministic env granted no `SELECT`, so its agents'
`acl_groups` couldn't read anything through GenieRails. Nothing is revoked
by surprise on upgrade:

1. `make release` (and rehearse) refuses, naming every agent table and
   `acl_groups` group that has no `table_readers` entry, before taking the
   lock or applying anything.
2. `make generate ENV=dev` proposes `table_readers` for every agent table
   (each agent counts as newly imported, since no proposal record exists).
   Review, commit, `make rehearse`, `make promote-to ENV=prod` (seeds prod's
   `table_readers`), release.
3. If an env's data_access state already holds `SELECT` grants (for example
   from an earlier mode), Terraform refuses to plan while `table_readers` is
   unset instead of revoking them. Set `table_readers` to the readers to keep,
   or `table_readers = {}` to revoke deliberately; `make plan` lists what
   would be revoked.
