# Import Existing Resources (Overwrite / Adopt)

If groups, tag policies, tag assignments, or FGAC policies **already exist**, Terraform will fail with "already exists". Use the import workflow below so Terraform can adopt them into the correct 3-layer state.

## Prerequisites

Before running imports, ensure:

1. The relevant `envs/<layer-or-workspace>/auth.auto.tfvars` contains valid credentials.
2. The target `abac.auto.tfvars` contains the resources you want Terraform to manage.
3. You have already run `make setup` for the workspace you are working on.

## Layer ownership

- `ENV=account` (`envs/account`): shared account groups, optional membership, and the tag policies (tag keys; tag policies are account-scoped and are imported only here)
- `ENV=<workspace>` data_access layer (`envs/<workspace>/data_access`): tag assignments, FGAC policies, grants
- `ENV=<workspace>` workspace layer (`envs/<workspace>`): workspace-local resources such as the SQL warehouse and existing Genie ACL targets

## Usage

From your cloud wrapper directory (`aws/` or `azure/`):

```bash
# Import account groups and tag policies
make import ENV=account

# Import env-scoped governance + workspace-local resources for the default dev env
make import
make import ENV=prod

# Preview imports directly from the env directory (the script lives in shared/scripts)
cd envs/account && ../../../shared/scripts/import_existing.sh --groups-only --dry-run
cd envs/account && ../../../shared/scripts/import_existing.sh --tags-only --dry-run
cd envs/dev/data_access && ../../../../shared/scripts/import_existing.sh --fgac-only
cd envs/dev/data_access && ../../../../shared/scripts/import_existing.sh --tag-assignments-only
```

Run by hand, the script sets `ENVS_DIR` (where the Terraform runner finds each
layer's env dir and state) to the parent of the env dir, i.e. `<cloud>/envs`,
the same value `make` exports. Run it only from `envs/account`, `envs/<env>` or
`envs/<env>/data_access`; set `ENVS_DIR` yourself only for a non-standard layout.

The script reads the target env's `abac.auto.tfvars` and imports into the module address used by that layer:

- `envs/account` imports `module.account.databricks_group.*` and `module.account.databricks_tag_policy.*`
- `envs/<workspace>/data_access` imports `module.data_access.databricks_entity_tag_assignment.*`, `module.data_access.databricks_policy_info.*` and `module.data_access.databricks_grant.*` (it skips tag policies: they're account-scoped)
- `envs/<workspace>` imports workspace-local resources through `module.workspace.*`

## Deterministic mode

`make import ENV=account` changes only Terraform state: it adopts existing tag
policy keys (Terraform ignores their values) and groups, and never adds or
removes a tag-policy value. While any env is deterministic, account changes
still go through `make account-plan` and `make apply-account`; an import changes
the account state serial, so a pending account manifest no longer matches and
`apply-account` asks for a fresh `account-plan`. Import first, then plan.
Tag-policy values that exist live but aren't in this checkout stay where they
are (see [tag-policy values](docs/deterministic-policies.md#tag-policy-values)).
For a deterministic workspace env, `make import` skips `gr_treatment` tag
assignments (`--skip-treatment-tags` is implied by `governance_mode`), and
`release-plan` records the imports a release needs instead of running them.

## Optional: reuse an existing warehouse

To use an existing warehouse instead of auto-creating one, set in the relevant `env.auto.tfvars`:

```hcl
sql_warehouse_id = "<WAREHOUSE_ID>"
```

- In `envs/<workspace>/data_access/env.auto.tfvars`, this reuses the warehouse for masking function deployment.
- In `envs/<workspace>/env.auto.tfvars`, this reuses the warehouse for the workspace layer / Genie agent.
