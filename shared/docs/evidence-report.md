# Compliance evidence report

`make evidence ENV=<env>` writes a versioned JSON artifact and matching Markdown summary under `envs/<env>/generated/evidence/`. Offline mode is the default: it records the configured column footprint and tags without creating a Databricks client.

To capture deployed Unity Catalog tags, masks, grants, and column observation status, opt into live collection explicitly:

```bash
GENIERAILS_EVIDENCE_INTEGRATION=1 make evidence ENV=prod
```

The SQL warehouse is resolved for you: the env's `sql_warehouse_id`, else one unique `genie_spaces[].sql_warehouse_id`, else the `sql_warehouse_id` output of the applied workspace layer. It stops if none is found or the per-agent values conflict. `WAREHOUSE_ID=<id>` is optional and pins a specific warehouse.

The output folder is not git-ignored. In a deterministic env, archive the record and remove `envs/<env>/generated/evidence/` before `make maintain` or the next `make release-plan`, which need a clean checkout.

Set `GENIERAILS_EVIDENCE_APPROVED_BY` and `GENIERAILS_EVIDENCE_APPROVED_AT` to populate the approval header. Each column entry contains its classification scan status/time, detected tags, applied mask, row-filter and ABAC policies, and effective column, table, schema, and catalog grants. The `schema_version` field identifies the artifact contract; JSON is canonical and Markdown is its review-friendly rendering.

Live collection uses the Databricks SDK statement execution API, Unity Catalog `system.information_schema`, and the Unity Catalog policies API. Table-attached masks and row filters come from `information_schema` (legacy envs). ABAC policies, which deterministic governance deploys at catalog level and neither view lists, come from the policies API as the set effective on each governed table (catalog, schema and table level). A policy is attached to a column only when its mapping is fully evaluated and matches: the table's tags (`system.information_schema.table_tags`) satisfy the policy's `when_condition` (none means always), and, for a column mask, the column's tags (for example its `gr_treatment` value) satisfy the match condition whose alias is the mask's `on_column`; a row filter covers every column of its table. Conditions must be an exact OR of `hasTagValue`/`hasTag` checks. `applied_policies` then names the policy and `applied_abac_policies` gives its function, conditions, `to_principals` and `except_principals`. A column covered by both kinds shows both. A policy whose type, `when_condition`, `on_column` binding or match condition can't be evaluated exactly is never shown as protection: it is listed under `abac_policies_not_evaluated` (table, policy, reason), `abac_policy_listing.status` is `incomplete`, and the Markdown carries a warning table. If the policies or the table tags can't be read, `make evidence` exits 2 and writes nothing, instead of reporting a governed column as unprotected. It is intentionally unavailable unless `GENIERAILS_EVIDENCE_INTEGRATION` is truthy, keeping unit tests and normal offline generation credential-free.
