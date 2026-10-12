# Version Control & Standalone Terraform

After running `make generate` and `make apply`, your governance configuration is plain Terraform — you own it, you version-control it, and you can run it independently of GenieRails if needed.

## Where your configs live

After `make generate` + `make promote`, the single LLM-generated draft is split into three stable layers:

```
envs/
├── account/                        ← ACCOUNT LAYER (shared across all envs)
│   └── abac.auto.tfvars           ← groups, tag_policies
│
├── dev/                            ← WORKSPACE + DATA ACCESS (per environment)
│   ├── env.auto.tfvars            ← input config: tables, warehouse, genie_spaces
│   ├── abac.auto.tfvars           ← Genie agent configs (title, instructions,
│   │                                 benchmarks, SQL measures, sample questions)
│   ├── auth.auto.tfvars           ← credentials (NEVER commit)
│   ├── generated/                 ← reviewed rules (commit)
│   └── data_access/
│       ├── abac.auto.tfvars       ← tag_assignments, fgac_policies
│       └── masking_functions.sql  ← SQL UDFs for column masking
│
└── prod/                           ← same structure after cross-env promote
```

**What lives where:**

| Concern | Layer | File |
|---------|-------|------|
| Groups and tag policies | Account | `envs/account/abac.auto.tfvars` |
| Tag assignments and FGAC policies | Data Access | `envs/<env>/data_access/abac.auto.tfvars` |
| Masking SQL functions | Data Access | `envs/<env>/data_access/masking_functions.sql` |
| Genie agent configuration | Workspace | `envs/<env>/abac.auto.tfvars` |
| Environment input (tables, warehouse) | Workspace | `envs/<env>/env.auto.tfvars` |

`generated/` holds the reviewed rules. Commit it: `make promote` splits it into the three layers, and `make check` re-derives the layer files from it to prove they're current.

## What to version control

### Commit these

| File | Why |
|------|-----|
| `envs/account/abac.auto.tfvars` | Shared groups and tag policies — the governance baseline |
| `envs/<env>/env.auto.tfvars` | Your environment config (tables, warehouse, spaces) |
| `envs/<env>/abac.auto.tfvars` | Genie agent configuration (tuned instructions, benchmarks) |
| `envs/<env>/data_access/abac.auto.tfvars` | Tag assignments and FGAC policies |
| `envs/<env>/data_access/masking_functions.sql` | Masking SQL UDFs deployed to the warehouse |
| `envs/<env>/generated/` | Reviewed rules; `make check` re-derives and verifies the layer files from it |

### Never commit

| File | Why |
|------|-----|
| `auth.auto.tfvars` | Contains service principal credentials |
| Runtime records | `.*.apply.sha`, `.coverage_gate.json`, `.can_run_check.json`, `generated/.*` |
| `*.tfstate`, `*.tfstate.backup` | Terraform state may contain secrets |
| `.terraform/` | Provider binaries and cache |
| `.genie_space_id` | Auto-managed by Terraform lifecycle |
| `.genierails-state/` | Rehearse and release receipts and release manifests (when `GENIERAILS_STATE_DIR` isn't set) |

**Deterministic envs** (`governance_mode = "deterministic"`): applying commands
(`rehearse`, `release-plan`, `release`, `maintain`) never write committed files
(design section 9). What they re-derive from live tags goes to
`generated/.runtime/`, and the layer files (`envs/<env>/abac.auto.tfvars`,
`envs/<env>/data_access/`, `envs/account/abac.auto.tfvars`) and
`generated/genie_space_derived_acl_groups.auto.tfvars` are their runtime
outputs, so don't commit them: commit `env.auto.tfvars`, `generated/`,
`ack.txt` and `agents/`. `make check` re-derives and checks the layer files
from `generated/`. A release needs a clean checkout: an untracked or edited
file under the cloud dir or `shared/` makes `release-plan` refuse.

### Setting up git tracking

The default `.gitignore` excludes `envs/` entirely. To track your configs:

```bash
# Option 1 (recommended): in .gitignore, replace the three lines
# "envs/", "generated/" and "masking_functions.sql" with:
auth.auto.tfvars
*.tfstate
*.tfstate.backup
.terraform/
.genie_*
**/envs/**/data_access/.policy_generations/
**/envs/**/data_access/classification.auto.tfvars
**/envs/**/data_access/discovered_uc_tables.auto.tfvars
**/envs/**/.*.apply.sha
**/envs/**/.coverage_gate.json
**/envs/**/.can_run_check.json
**/envs/**/generated/.*
**/envs/**/ddl/_fetched.sql
.genierails-state/
# Deterministic envs: the layer files are runtime outputs (see above). With a
# legacy env in the same repo, list the deterministic envs' paths instead.
**/envs/*/abac.auto.tfvars
**/envs/*/data_access/
**/envs/*/generated/genie_space_derived_acl_groups.auto.tfvars

# Option 2: Force-track specific files (leave .gitignore as-is)
git add -f envs/account/abac.auto.tfvars
git add -f envs/dev/env.auto.tfvars
git add -f envs/dev/abac.auto.tfvars
git add -f envs/dev/data_access/abac.auto.tfvars
git add -f envs/dev/data_access/masking_functions.sql
git add -f envs/dev/generated/abac.auto.tfvars envs/dev/generated/masking_functions.sql envs/dev/generated/governed_tables.json
# promoted envs also: envs/prod/generated/expected_classification.json
```

## Pinning to a GenieRails version

After deploying, pin your GenieRails version to avoid unexpected changes from upstream updates:

```bash
# Use a tagged release
git checkout v1.0.0

# Or pin to a specific commit
git checkout abc1234
```

**Why this matters:** The Terraform modules in `shared/modules/` and `shared/roots/` define resource structure. If a future GenieRails version changes a module (adds a resource, renames a variable), running `make apply` against your existing state could modify or destroy deployed resources.

**Safety checklist after updating GenieRails:**

1. `git diff` — review what changed in modules and scripts
2. `make plan ENV=dev` — preview what Terraform would change
3. Only `make apply` if the plan looks safe

The Terraform Databricks provider is already pinned (`~> 1.91.0` in each root), so provider updates won't surprise you. GenieRails module changes are the main thing to watch.

## Running Terraform without GenieRails

After `make promote`, the split configs are plain `.tfvars` files. You can run Terraform directly:

```bash
# 1. Account layer (groups, tag policies)
cd shared/roots/account
terraform init -backend-config="path=../../../envs/account/terraform.tfstate"
terraform apply \
  -var-file="../../../envs/account/auth.auto.tfvars" \
  -var-file="../../../envs/account/abac.auto.tfvars"

# 2. Data access layer (tag assignments, FGAC policies, masking UDFs)
cd shared/roots/data_access
terraform init -backend-config="path=../../../envs/dev/data_access/terraform.tfstate"
terraform apply \
  -var-file="../../../envs/dev/auth.auto.tfvars" \
  -var-file="../../../envs/dev/env.auto.tfvars" \
  -var-file="../../../envs/dev/data_access/abac.auto.tfvars" \
  -var="env_dir=$(pwd)/../../../envs/dev/data_access"

# 3. Workspace layer (Genie agents, ACLs)
cd shared/roots/workspace
terraform init -backend-config="path=../../../envs/dev/terraform.tfstate"
terraform apply \
  -var-file="../../../envs/dev/auth.auto.tfvars" \
  -var-file="../../../envs/dev/env.auto.tfvars" \
  -var-file="../../../envs/dev/abac.auto.tfvars" \
  -var="env_dir=$(pwd)/../../../envs/dev"
```

**Important:** Apply layers in order — account first, then data access, then workspace. Tag policies must exist before FGAC policies can reference them.

### What the Makefile adds

Running standalone Terraform works, but you lose these conveniences:

| Feature | What it does |
|---------|-------------|
| Layer ordering with waits | 30s propagation delay between layers |
| Tag policy visibility check | Verifies tag policies are visible to FGAC engine before proceeding |
| Auto-import | Imports existing resources to prevent "already exists" errors |
| Fingerprint skip | Skips apply if config hasn't changed since last run |
| `TF_DATA_DIR` isolation | Prevents Terraform state conflicts between layers |

### Python dependencies in Terraform

Two Terraform resources use `local-exec` provisioners that call Python scripts:

| Layer | Script | Purpose |
|-------|--------|---------|
| Data Access | `deploy_masking_functions.py` | Deploys masking SQL UDFs to the warehouse |
| Workspace | `genie_space.sh` | Creates/updates Genie agents via REST API |

These scripts must be available at their expected paths (relative to `shared/scripts/`). If you relocate files, update the `genie_script_path` and `masking_script_path` variables in the Terraform roots.

## Next steps

- [CI/CD Integration](cicd.md) — automate validation and deployment from a pipeline
- [Architecture](architecture.md) — understand the three-layer model in depth
