# Migrating a legacy env to deterministic governance

An env that GenieRails deployed in legacy mode (AI-drafted masks on
`gr_treatment`) moves to deterministic governance by committing the opt-in and
running the normal stages. An env that never opts in is untouched: its plans
show no change. Design: [deterministic-governance.md, section 8](design/deterministic-governance.md#8-migration-for-existing-deployments).

```bash
make migrate-plan ENV=prod        # 1. read-only dry run on the live env; proposes table_readers
# 2. set governance_mode = "deterministic", access_tier_groups, table_readers, acl_groups;
#    make generate (dev) or make promote-to ENV=prod; add the reviewed ack.txt lines
make migrate-plan ENV=prod        # 3. repeat until "the cutover may proceed"; PR, review, merge
make rehearse ENV=dev             # 4. dev migrates here (and writes the receipt prod needs)
make release-plan ENV=prod        # 5. one manifest per phase: approve, then
make release ENV=prod MANIFEST=<manifest>   #    repeat 5 until "Release complete"
```

## The dry run

`make migrate-plan ENV=<env>` reads the env's Terraform state (what GenieRails
deployed) and the committed config; it changes nothing in git or Databricks.
It reports:

- per column and per principal: what each sees today (the legacy mask) and after
  (the tier rule), with the legacy function's SQL next to the library's;
- every legacy policy's `TO`/`EXCEPT` principals next to the new partial and full policies;
- every `SELECT` and `CAN_RUN` GenieRails grants today that the config drops (a
  **revocation**), and the `USE_CATALOG`/`USE_SCHEMA` grants that go with them;
- weakenings, frozen legacy row filters, agents without `acl_groups`, masks
  GenieRails didn't create, and the generated `terraform state mv` lines.

It writes a proposed `table_readers` that reproduces every `SELECT` GenieRails
grants today to `<state dir>/<env>/migration/table_readers.proposed.hcl` (and
`report.json`). Run it before opting in for the proposal; after, for the full
comparison. Exit code 0 means the cutover may proceed.

## Refusals

| Refused | To accept it |
|---|---|
| a weakening (a principal sees more than today) | `<env>:weaken:<column>:<principal>` in `envs/<env>/ack.txt` |
| a revoked `SELECT` | `<env>:revoke:<catalog.schema.table>:<principal>` |
| a revoked `CAN_RUN` | `<env>:revoke:<agent key>:<group>` (the key the report prints) |
| a legacy row filter missing or changed | none: legacy row filters stay frozen until declared `row_filters` replace them |
| a mask GenieRails didn't create | none: remove it |
| an agent without `acl_groups`, a legacy generated config, a legacy mask not on `gr_treatment` | fix the config |

The same checks run again when the cutover starts (rehearse, release-plan), so
nothing is moved or applied while one fails. After the cutover the
acknowledgements stay accounted for, so they never turn stale.

## The cutover

The staged policy controller ([deterministic-policies.md](deterministic-policies.md))
adopts the legacy deployment as the live generation:

1. Under the env lock, each legacy treatment tag moves to its stable key with
   `terraform state mv` (never destroyed). Legacy sensitivity tags the config
   drops are forgotten with `terraform state rm`; they stay on the column.
2. **create**: the new functions and policies are added under new names; every
   column keeps its legacy value and mask. Verified live and with the legacy
   verify-access checks.
3. **switch**: each column is retagged in place, from its legacy value to its
   deterministic one. One tag per column, so no column is ever unmasked or
   doubly masked. Verified against the tier rule.
4. **grant**: `SELECT` from `table_readers` and `CAN_RUN` from `acl_groups`.

In prod each changing phase is one `release-plan` manifest and approval, with
release-plan's provenance (clean checkout on freshly fetched `origin/main`, a
matching passing dev receipt, not older than the last release unless
`prod:rollback:<commit>` is acknowledged). The legacy policies stay live until
a later successful release retires them; legacy functions are never dropped.
Once migrated, an env can't be applied in legacy mode again; undo a change by
rolling back within deterministic governance.
