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
| `envs/dev/remote_state.hcl`, `envs/account/remote_state.hcl` | Remote state backend (non-secret settings only), when the dev pipeline runs in CI ([CI/CD](cicd.md#state-location-and-dev-remote-state)) |

### Who owns which file

Each persona changes its own files (design section 18.1), so CODEOWNERS can
require only the owners of what changed:

| Persona | Files |
|---|---|
| Governance engineer (treatments, `tag_treatments`, tiers, `group_map`, `table_readers`, row filters, top-level `uc_tables`, `ungovern`; approves the governed-table set) | `envs/<env>/env.auto.tfvars`, `envs/<env>/generated/` (including `governed_tables.json`), `envs/<env>/ack.txt` |
| Data citizen (curates an agent in the dev UI, including its tables, captures it, promotes it) | `envs/<env>/agents/<agent>.auto.tfvars` (`genie_space_id`, `acl_groups`, `uc_tables`, `detached`, `delete`) and `envs/<env>/agents/<agent>.space.json` |

- `env.auto.tfvars` holds only governance and env-owned settings; agent
  settings live only in `agents/` files, so an agent-only PR never touches a
  governance file. Customer class mappings are the env's `tag_treatments`
  (`make scaffold-treatments ENV=<env> TREATMENT=<name>` writes them), never
  `shared/`, which is GenieRails product code.
- An agent's table list is agent-owned, and governance approves the result:
  `make generate`, `make capture` and `make promote-to` record every
  file-defined agent's tables under `agent_tables` in
  `generated/governed_tables.json` (commit it). So an agent change that adds
  or removes a table, even one with no classified column, always changes that
  governance-owned file, and `make check` fails while it is stale. It grants
  nothing: `SELECT` comes only from `table_readers`, and `CAN_RUN` is refused
  while an `acl_groups` group can't read one of the agent's tables
  ([access.md](access.md)).
- `make promote-to` writes agent output under `envs/<target>/agents/` and
  governance output under `envs/<target>/generated/` and
  `envs/<target>/env.auto.tfvars`, and prints which kind it changed. An
  agent-only promotion (capture, then promote) changes only `agents/` files;
  one that changes an agent's tables also changes
  `envs/<target>/generated/governed_tables.json`, never `env.auto.tfvars`.
- [`shared/examples/CODEOWNERS.example`](../examples/CODEOWNERS.example) is a
  sample with both rules; copy it to `.github/CODEOWNERS` and use your teams.

**Moving an existing env's agents out of `env.auto.tfvars`.** An env that
keeps its agents inline in `genie_spaces` (the legacy layout) keeps working
as it is; nothing moves them implicitly. To split them, run the explicit
migration and review its diff in a PR:

```bash
make split-agent-files ENV=dev   # then each promote-to target, e.g. ENV=prod
make plan ENV=dev                # no change: Terraform gets the same agents
```

It moves each entry's `genie_space_id`, `acl_groups`, `uc_tables` and
`sql_warehouse_id` into `agents/<name>.auto.tfvars`, sets `genie_spaces = []`
and leaves every other setting as it was. It refuses, writing nothing, when
moving an entry would change what Terraform plans: an entry with no `name`
(its key is its ID), a setting agent files don't hold (such as
`declared_footprint`), a pending `delete = true`, two names with the same
Terraform key, or an agent ID in a promoted env (move it to
`.genie_space_id_<key>` first). Migrate an env and its promote-to targets
together: while prod still has an agent inline that dev keeps in a file,
`make promote-to ENV=prod` refuses and points here, so prod's own
`acl_groups` are never replaced.

### Never commit

| File | Why |
|------|-----|
| `auth.auto.tfvars` | Contains service principal credentials |
| Runtime records | `.*.apply.sha`, `.coverage_gate.json`, `.can_run_check.json`, `generated/.*` |
| `*.tfstate`, `*.tfstate.backup`, `*.tfstate.*.backup` | Terraform state may contain secrets (`terraform state mv`/`rm` backups are written next to the env's state, never in `shared/roots`) |
| `.terraform/` | Provider binaries and cache |
| `.genie_space_id` | Auto-managed by Terraform lifecycle |
| `.genierails-state/` | Rehearse and release receipts and release manifests (when `GENIERAILS_STATE_DIR` isn't set) |
| `envs/<env>/.state-moved` | Left by `make migrate-state-dir` on the machine whose state moved; other checkouts don't have its state dir |

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

The default `.gitignore` excludes `envs/` entirely, so the GenieRails repo
itself never tracks anyone's env. In your own repo, opt in once from the cloud
dir:

```bash
make setup ENV=dev GITIGNORE=customer
```

It writes a GenieRails block into `<cloud>/.gitignore` (and prints it as a
diff) that re-includes `envs/` and ignores every runtime file: credentials,
Terraform state and plans, locks, receipts, Genie ID and adoption markers,
every dot-file under `envs/` (such as `generated/.runtime/` and
`data_access/.policy_generations/`) and, for each deterministic env, its layer
files. The rule:

- **`GITIGNORE=customer`** writes the block, or refreshes it.
- **No `GITIGNORE`** (the default): every `make setup` refreshes the block if
  the file already has it, and otherwise only prints how to opt in. So in the
  GenieRails repo, which never has the block, `make setup` changes nothing.
- **`GITIGNORE=off`** leaves the file alone.

Only the lines between the block's markers are ever rewritten; your own lines
are kept as they are, and re-running is a no-op. The block names each
deterministic env, so re-run `make setup ENV=<env>` after adding an env or
changing `governance_mode`.

Or leave `.gitignore` as it is and force-track the files:

```bash
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
