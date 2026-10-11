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
  dev (catalogs remapped, groups through `group_map`); later promotions keep prod's value and print how
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
`scripts/agent_coverage.py`, which checks Unity Catalog live against
GenieRails' own governance:

1. every classified column (`class.*` tag) of each agent's tables has a
   GenieRails mask in effect (a GenieRails column-mask policy whose condition
   names one of the column's tags). GenieRails supports only governance it
   generates: a table-attached mask or a policy made in the UI or by another
   tool doesn't count, and the external-mask inventory refuses rehearse,
   release and maintain while one is on a governed table;
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

The check catches masks or grants changed outside GenieRails since the last
apply; it doesn't re-verify each tier's output (that is `verify-access`).

## Group names in prod: `group_map`

`access_tier_groups` is governance: `make promote-to` copies dev's tiers to
prod on every promotion, so prod uses exactly the rehearsed tiers. Where
prod's group names differ, set an env-owned map in `envs/prod/env.auto.tfvars`:

```hcl
group_map = { dev_analysts = "prod_analysts", dev_viewers = "prod_viewers" }
```

- `promote-to` maps every group it writes: `access_tier_groups`, the groups
  in `row_filters`, the principals of the promoted policies, and the first
  seeding of `acl_groups` and `table_readers`. A dev group with no mapping
  keeps its name. Later promotions keep prod's own `acl_groups` and
  `table_readers`, and print how dev's (mapped) values differ.
- `raw_exempt_principals` are env-owned and never mapped; a `group_map` key
  that is one is refused. Many-to-one mappings are refused (they would merge
  tiers or readers), and so is mapping `account users`.
- `promote-to` reads only committed files. `rehearse` and `release` check
  live, before the lock: every group the env names (tiers, readers,
  `acl_groups`, row-filter groups, mapped or not) must exist in the
  workspace.
- Nested groups are expanded only after mapping: the reader and agent
  checks read the destination's own (mapped) config.
- Adding, removing or retargeting a mapping is a weakening. Compared with
  the `group_map` the last successful rehearse or release used, each change
  stops the run until `envs/<env>/ack.txt` has
  `<env>:weaken:group_map:<dev>=<target>` (for a removed mapping the target
  is the dev group itself).
- Legacy destinations keep their own tiers on re-promotion, as before.

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
