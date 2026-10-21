# Compliance evidence report

`make evidence ENV=<env>` writes a versioned JSON artifact and matching Markdown summary under `envs/<env>/generated/evidence/`. Offline mode is the default: it records the configured column footprint and tags without creating a Databricks client.

To capture deployed Unity Catalog tags, masks, grants, and column observation status, opt into live collection explicitly:

```bash
GENIERAILS_EVIDENCE_INTEGRATION=1 make evidence ENV=prod
```

The SQL warehouse is resolved for you: the env's `sql_warehouse_id`, else one unique `genie_spaces[].sql_warehouse_id`, else the `sql_warehouse_id` output of the applied workspace layer. It stops if none is found or the per-agent values conflict. `WAREHOUSE_ID=<id>` is optional and pins a specific warehouse.

The output folder is not git-ignored. In a deterministic env, archive the record and remove `envs/<env>/generated/evidence/` before `make maintain` or the next `make release-plan`, which need a clean checkout.

Set `GENIERAILS_EVIDENCE_APPROVED_BY` and `GENIERAILS_EVIDENCE_APPROVED_AT` to populate the approval header. Each column entry contains its classification scan status/time, detected tags, applied mask and row-filter policy names, and effective column, table, schema, and catalog grants. The `schema_version` field identifies the artifact contract; JSON is canonical and Markdown is its review-friendly rendering.

Live collection uses the Databricks SDK statement execution API and Unity Catalog `system.information_schema`. It is intentionally unavailable unless `GENIERAILS_EVIDENCE_INTEGRATION` is truthy, keeping unit tests and normal offline generation credential-free.
