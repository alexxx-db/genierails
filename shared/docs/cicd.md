# CI/CD Integration

This document explains how to integrate the quickstart into a CI/CD process.

## Recommended Model

Use this split of responsibilities:

- Local or developer workflow:
  - run `make generate`
  - review and tune `generated/abac.auto.tfvars`
  - review and tune `generated/masking_functions.sql`
  - run `make validate-generated`
  - commit the reviewed config changes
- CI workflow:
  - validate committed config **and run `make coverage-gate`** (block the build if any classified sensitive column has no covering mask)
  - run `make plan`; applying from CI is not supported in v1
- Persistent deployment workspace:
  - after the reviewed change is approved, enable/wait for prod native classification and run `make release ENV=prod`
  - keep the deployment checkout, credentials, and Terraform state persistent between releases

Pipeline samples for every stage, for GitHub Actions and Azure DevOps, are in
[Pipeline samples](#pipeline-samples-where-each-command-runs). Their applying
stages stay on the deployment machine until remote state exists. Do not
bypass the v1 CI guard on a deployment job.

This keeps LLM-driven generation and human review out of the automated deployment path, keeps the LLM out of prod entirely (prod re-derives deterministically), and makes coverage an explicit, enforced check rather than something you hope happened.

> **Prerequisite:** Your configs should be version-controlled before setting up CI/CD. See [Version Control & Standalone Terraform](version-control.md) for what to commit and how to set up git tracking.

## What Should Be Committed

Commit the reviewed environment and layer config:

- `envs/account/abac.auto.tfvars`
- `envs/<env>/abac.auto.tfvars`
- `envs/<env>/data_access/abac.auto.tfvars`
- `envs/<env>/data_access/masking_functions.sql`
- `envs/<env>/env.auto.tfvars`

Do not commit secrets or local state:

- `envs/<env>/auth.auto.tfvars`
- `envs/account/auth.auto.tfvars`
- Terraform state files
- local apply fingerprint files
- fetched local DDL snapshots unless you intentionally want them in version control

## Secrets in CI

Your pipeline should inject credentials at runtime rather than committing `auth.auto.tfvars`.

For each target environment, create `envs/<env>/auth.auto.tfvars` during the job from secret values such as:

- `databricks_account_id`
- `databricks_client_id`
- `databricks_client_secret`
- `databricks_workspace_id`
- `databricks_workspace_host`

The [pipeline samples](#pipeline-samples-where-each-command-runs) use OIDC workload identity federation instead: they write only the IDs and leave `databricks_client_secret` empty.

If the account layer or `data_access` layer needs different credentials, write those layer-specific auth files separately instead of relying on the default symlinks.

## Recommended Pipeline Stages

## 1. Validate on pull requests

Run one read-only command per env:

```bash
make check ENV=dev
make check ENV=prod     # when the PR touches envs/prod/**; includes make promote-to ENV=prod CHECK=1
```

`make check` runs `validate`, checks the committed generated files are current,
then `validate-generated`, the blocking offline coverage check (it fails the PR
if any classified sensitive column has no covering mask), the mask-library unit
tests and a structural `plan`. See the [command reference](../examples/dev_to_prod/REFERENCE.md#command-reference).

## 2. Plan before deploy

On the deployment machine, where the Terraform state lives, review the change before applying it:

```bash
make plan ENV=dev
make plan ENV=prod
```

This shows the net change across the layered state model:

1. shared `account`
2. env-scoped `data_access`
3. env-scoped `workspace`

## 3. Release from the persistent deployment workspace

After approval, deploy from the persistent deployment workspace. **For prod,
re-derive facts from prod's own classification first** — never re-run `generate`
in prod (that re-invokes the LLM and could drift from the reviewed rules):

```bash
# prod facts: enable/wait for native classification first
make release ENV=prod   # row-pairing keys: picked per table and proven before apply
```

`make release` re-derives assignments from prod's own tags (no LLM), validates,
runs the coverage check and the rulebook audit, applies all layers in order
(masks and policies before grants), then proves masking with `verify-access`.
It never re-generates via the LLM. Terraform itself refuses new or wider
business `SELECT` / Genie `CAN_RUN` without a recent passing coverage result,
so no path can grant access past the gate.

## Promotion in CI/CD

There are two common models:

### Model A: Promote locally, release from the deployment workspace

Recommended for most teams.

1. A developer runs:

   ```bash
   make promote-to ENV=prod   # promote_from/catalog_map are reviewed in envs/prod/env.auto.tfvars
   ```

2. The promoted config is reviewed and committed
3. CI runs `make check ENV=prod` against the reviewed configuration; it includes `make promote-to ENV=prod CHECK=1`, which fails if the committed promotion differs from what `promote-to` writes
4. After approval, the operator enables/waits for prod native classification and runs `make release ENV=prod` from the persistent deployment workspace; if its coverage check fails, fix the rules in dev and re-promote

This is the best model when you want promotion to stay explicit and reviewable in Git.

### Model B: Generate independent environments

Use this for separate business units or environments that should not inherit `dev` governance.

1. A developer runs:

   ```bash
   make generate ENV=bu2
   ```

2. The generated config is reviewed and committed
3. CI validates and plans `ENV=bu2`; the persistent deployment workspace applies the approved change

## Pipeline samples: where each command runs

Each cloud wrapper ships one sample per stage of design section 17, for GitHub
Actions (`<cloud>/.github/workflows/`) and Azure DevOps
(`<cloud>/azure-pipelines/`). GitHub only runs workflows from `.github/workflows/`
at the repository root, so copy the ones you use there; in Azure DevOps, create
a pipeline from each file. The samples assume the repository is a GenieRails
checkout and run from `aws/` or `azure/`.

| Stage | Sample | Trigger | Runs in CI in v1 |
|---|---|---|---|
| 2. PR check | `pr-check.yml` | every pull request (a required check; re-runs when `main` moves) | `make check ENV=dev`; also `make check ENV=prod` when the PR touches `envs/prod/**` |
| 3. Dev rehearsal | `dev-rehearse.yml` | merge to `main` that touches the cloud dir or `shared/`; one run per commit, in order, never cancelled (GitHub: a queue job and no concurrency group; Azure DevOps: `batch: false`, `lockBehavior: sequential` and an Exclusive Lock check) | `make check ENV=dev` on the merged commit |
| 4. Prod release | `prod-release.yml` | dev pipeline success on `main`, only for a commit touching `envs/prod/**` (GitHub `workflow_run`; Azure DevOps pipeline-resource trigger); a protected environment with a required approval | `make check ENV=prod` on that commit, then the approval gate |
| 5. Keep protected | `prod-maintain.yml` | a schedule | `make audit-schema ENV=prod` (read-only drift audit) |

**Auth.** Every Databricks credential is a service principal through OIDC
workload identity federation (`github-oidc` or `azure-devops-oidc`); no PATs
and no client secrets. The samples write `auth.auto.tfvars` with IDs only (an
empty `databricks_client_secret`) and set `DATABRICKS_AUTH_TYPE`. Your
Databricks Terraform provider and SDK must support that auth type.

**`make check` is read-only.** Each step runs in a scratch copy of the cloud
dir that holds the env files without credentials, Terraform state or runtime
records; the plan step alone gets the env's `auth.auto.tfvars`. Nothing under
`envs/` is written and no applying target runs, so it is safe under `CI=true`.
The only file it can leave outside the copy is Terraform's own
`shared/roots/*/.terraform.lock.hcl` on a first plan, which is gitignored.

**v1 limits.**

- **Applying stages run on the deployment machine** until the env has remote
  state with locking: `make rehearse ENV=dev` (dev remote state is rollout
  step 12), `make release ENV=prod` and `make maintain ENV=prod` (prod remote
  state is deferred, design section 15). The operator runs them there, from a
  clean checkout of the commit the pipeline names, in order. Every applying
  target keeps refusing in CI, and the samples never set
  `GENIERAILS_ALLOW_CI_APPLY`, which exists only for GenieRails' own throwaway
  integration-test workspace. The dev and prod samples already carry the
  triggers, ordering, locks and approvals; their applying steps are marked
  `REQUIRES ... REMOTE STATE` and only report the commit to apply.
- **The plan in `make check` is structural**: the committed config against
  empty state, without re-reading live tags. It proves the config plans, not
  what a release would change. State-aware plans start with remote state.
- **Generated files are checked by re-running the deterministic steps**: the
  same-env split of `generated/` into layer files, the governed-table record,
  and, for an env with `promote_from` or `ENV=prod`, `promote-to`. Legacy envs
  skip this with a note. `generated/abac.auto.tfvars` itself is a reviewed
  draft and is not regenerated, so a hand edit there shows up only through the
  committed layer files it feeds.
- **`promote-to CHECK=1` never reads prod state**, so the policy names it
  expects are the ones a first promote picks. The deployed-name overlay moves
  to `release-plan` with the two-phase release (rollout step 9), as do the
  release manifest and per-commit receipts.

---

## Schema Drift Detection in CI

Use `make audit-schema` as a scheduled CI check to detect when new columns need governance:

```bash
make audit-schema ENV=prod
```

This exits `1` if untagged sensitive columns are found (forward drift) or if existing tag assignments reference deleted columns (reverse drift). Use GitHub's built-in failed-run notifications to alert when drift is detected.

When drift is found, prefer letting native classification tag the new columns, then re-derive deterministically with `make maintain ENV=prod` (runs `derive-assignments`, reusing the promoted rules, no LLM). `make generate-delta` is an exceptional/legacy remediation that invokes the LLM — it is not the routine dev-to-prod path.

---

## Notes and Gotchas

- Avoid running `make generate` automatically in CI — and never in prod. Dev drafts locally with the LLM (reviewed, committed); prod re-derives facts with `make derive-assignments` (no LLM), so prod enforcement can't drift from the reviewed rules.
- Every plan/apply (`make apply`, `plan`, `apply-governance`, `apply-genie`, and `release`) re-reads live UC state and runs the enforced coverage check. Still keep `make coverage-gate` as a required PR check, so uncovered columns fail early on the generated config.
- `make apply ENV=<workspace>` also applies the shared account layer, so the deployment-workspace identity must be authorized for both account and workspace operations.
- If you deploy multiple environments from the same repo, parameterize `ENV` and inject the matching workspace secrets per environment.
- Destroy is a deployment-workspace operation, for example `make destroy ENV=dev`; it is refused in CI.
- If you are adopting existing Databricks resources, run the import from the persistent deployment workspace before planning the managed resources in CI.
