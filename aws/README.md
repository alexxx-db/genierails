# GenieRails — AWS

> **On Azure?** Go to [`../azure/README.md`](../azure/README.md) instead.

## Prerequisites

- Your tables must already exist in Unity Catalog before running `make generate`
- An AWS Databricks workspace with Unity Catalog enabled
- A Databricks service principal with the roles below
- Existing dev/prod catalogs. On a fresh metastore, the metastore owner or a
  `CREATE CATALOG` holder creates them; see the shared
  [prerequisites](../shared/docs/prerequisites.md#required-features).
- For scripted classification on a fresh AWS workspace, a Workspace Admin
  creates and binds the serverless budget/usage policy. Account Admin alone is
  not sufficient for that operation.

### Which service principal roles do I need?

| Mode | Role | Why it's needed |
| ---- | ---- | --------------- |
| Full (default) | **Account Admin** | Create groups, assign groups to workspaces, manage group membership |
| Full (default) | **Workspace Admin** | Grant entitlements, create warehouses, manage Genie agents and permissions |
| Full (default) | **Account Tag Policy Creator + Manager** | Create and maintain tag policies |
| Full (default) | **Authority over the governed catalog** (not Metastore Admin) | Own it, or hold `SELECT` + `MANAGE` + `APPLY TAG` on it: tags, masking functions, FGAC policies and grants ([Check 6](../shared/docs/prerequisites.md#service-principal)) |
| Genie-only | **Workspace USER** + **Databricks SQL access** entitlement | Create Genie agents only — set `genie_only = true` and provide `sql_warehouse_id` in `env.auto.tfvars`. No admin roles needed. |

## Step 1 — Set up your environment

```bash
cd aws/     # always run from here, never from shared/
make setup
```

This creates `envs/dev/` with two template files for you to fill in.

## Step 2 — Fill in credentials

Edit `envs/dev/auth.auto.tfvars`:

```hcl
databricks_account_id     = "your-account-id"
databricks_client_id      = "your-sp-client-id"
databricks_client_secret  = "your-sp-secret"
databricks_workspace_id   = "your-workspace-id"
databricks_workspace_host = "https://dbc-xxxxxxxx-xxxx.cloud.databricks.com"
```

> **Note:** No `databricks_account_host` is needed for AWS — the Terraform provider defaults to `accounts.cloud.databricks.com`.

## Step 3 — Follow the dev-to-prod walkthrough

The **[dev-to-prod walkthrough](../shared/examples/dev_to_prod/)** is the canonical end-to-end walkthrough: Unity Catalog classifies your data, GenieRails derives one protection per classified column, a coverage check fails the build if any classified sensitive column is unprotected, and users are granted access to the prod agent only after prod's own coverage check passes. No tables or agent of your own? It ships an optional sample-environment script that creates everything, so you can run the whole flow to see it in action.

Starting from a specific point? These entry guides feed into the dev-to-prod walkthrough:

| Starting point | You have... | Guide |
|---|---|---|
| **I already have a Genie agent** | An agent configured in the Databricks UI that needs governance and promotion to prod | [Import a Genie Agent from UI into Code](../shared/docs/import-genie-agent-from-ui.md), then follow the dev-to-prod walkthrough |
| **I'm starting from scratch** | Tables in Unity Catalog, no Genie agent yet | [Quickstart](../shared/docs/quickstart.md) |

---

## Documentation

- [Dev-to-Prod Walkthrough](../shared/examples/dev_to_prod/) — the canonical end-to-end walkthrough (native classification → coverage check → safe dev→prod promotion)
- [Import a Genie Agent from UI into Code](../shared/docs/import-genie-agent-from-ui.md) — import your existing agent, then follow the dev-to-prod walkthrough
- [Quickstart](../shared/docs/quickstart.md) — create a Genie agent from scratch
- Playbooks — after the first deployment: [agents](../shared/docs/playbooks/agents.md), [governance](../shared/docs/playbooks/governance.md), [one team](../shared/docs/playbooks/one-team.md)
- [Pipelines](../shared/docs/pipelines.md) and the [deployment machine](../shared/docs/deployment-machine.md)
- [Architecture](../shared/docs/architecture.md) — layers, artifact ownership, config files, Genie agent lifecycle
- [All documentation](../shared/docs/) — full list
