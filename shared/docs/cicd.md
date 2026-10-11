# CI/CD

How to run GenieRails from a pipeline. The full design is
[section 17 of the design doc](design/deterministic-governance.md#17-where-each-command-runs);
this page is what works **today (v1)**, with target-only items marked.

## Who runs what

| Stage | Where | Command | Writes Databricks? |
|---|---|---|---|
| 1. Author | your machine | `make generate ENV=dev` (and `make promote-to ENV=prod` for a promotion); review, commit, open a PR | no |
| 2. PR check | CI, every PR | `make check ENV=dev`; plus `make check ENV=prod` when the PR touches `envs/prod/**` | no |
| 3. Dev rehearsal | after each merge to `main`, in order | `make rehearse ENV=dev` | dev |
| 4. Prod release | after the promotion PR is merged, rehearsed and approved | `make release ENV=prod` (target: `release-plan` → approve → `release MANIFEST=…`, rollout step 9) | prod |
| 5. Keep protected | schedule | `make maintain ENV=prod` | prod, governance only |

- **In v1, stages 3–5 run on the deployment machine**, not in CI: every applying
  target refuses under CI (see [CI guard](#ci-guard)). The samples below carry
  the triggers and approvals and report the commit to apply.
- **Promotion is a person, not a pipeline.** `make promote-to ENV=prod` reads
  `promote_from` and `catalog_map` from `envs/prod/env.auto.tfvars`; commit its
  output in a PR. No pipeline writes to git.
- **Never run `make generate` in CI or prod.** Prod re-derives from its own tags.

## What to commit

GenieRails' own `.gitignore` ignores `envs/`, `generated/` and
`masking_functions.sql` anywhere in the tree. In your repo, replace those three
lines with the ignores in [Version control](version-control.md#setting-up-git-tracking),
then commit `envs/<env>/env.auto.tfvars`, `envs/<env>/generated/`, the layer
files (`envs/<env>/abac.auto.tfvars`, `envs/<env>/data_access/abac.auto.tfvars`,
`envs/<env>/data_access/masking_functions.sql`) and `envs/account/abac.auto.tfvars`.
Without `generated/`, `make check` skips the regeneration and coverage checks.

Never commit `auth.auto.tfvars`, Terraform state, `.terraform/`, Genie ID files,
or the runtime records GenieRails writes (`.*.apply.sha`, `.coverage_gate.json`,
`.can_run_check.json`, `generated/.*`).

## CI guard

Applying targets (`apply`, `apply-governance`, `apply-genie`, `apply-account`,
`rehearse`, `release`, `maintain`, `enable-classification`, `ungovern`,
`destroy*`, `import`, `migrate-state`) refuse when they detect CI:
`CI=true|1|yes`, or `TF_BUILD`, `JENKINS_URL`, `GITLAB_CI`, `BUILDKITE` or
`CIRCLECI` set. Only the listed targets are guarded: `make check`, `plan`,
`validate` and `audit-schema` are allowed, and `sync-tags` (which adds account
tag values), `bootstrap-sp` and `generate` are not guarded, so don't run them
from CI.
`GENIERAILS_ALLOW_CI_APPLY=1` exists only for GenieRails' own throwaway
integration tests; never set it on a deployment job.

## Branch protection and approvals

- Make the PR check a required status check and require branches to be up to
  date, so a PR is re-checked against the latest `main` before it can merge.
  No direct pushes to `main`, including admins.
- CODEOWNERS patterns are anchored at the repo root. Make the governance team
  own `/aws/envs/prod/`, `/azure/envs/prod/`, `/aws/envs/account/`,
  `/azure/envs/account/`, `**/envs/*/generated/`, `**/envs/*/data_access/` and
  `/shared/`, and turn on "Require review from Code Owners".
- Protect the prod environment (the samples use `production` on GitHub and
  `genierails-prod` on Azure DevOps): required reviewers, no self-approval,
  `main` only.

## Pipeline samples: where each command runs

Each cloud wrapper ships one sample per stage of design section 17, for GitHub
Actions (`<cloud>/.github/workflows/`) and Azure DevOps
(`<cloud>/azure-pipelines/`). GitHub only runs workflows from `.github/workflows/`
at the repository root, so copy the ones you use there; in Azure DevOps, create
a pipeline from each file. The samples assume the repository is a GenieRails
checkout and run from `aws/` or `azure/`.

| Stage | Sample | Trigger | Runs in CI in v1 |
|---|---|---|---|
| 2. PR check | `pr-check.yml` | every PR to `main`; make it required and require branches to be up to date, so a PR is re-checked against the latest `main` before it can merge (Azure Repos: a build-validation policy that re-runs when the target branch changes) | `make check ENV=dev`; also `make check ENV=prod` when the PR touches `envs/prod/**` |
| 3. Dev rehearsal | `dev-rehearse.yml` | merge to `main` that touches the cloud dir or `shared/`; one run per commit, in order, never cancelled (GitHub: a queue job and no concurrency group; Azure DevOps: `batch: false`, `lockBehavior: sequential` and an Exclusive Lock check) | `make check ENV=dev` on the merged commit |
| 4. Prod release | `prod-release.yml` | dev pipeline success on `main`, only for a commit touching `envs/prod/**` (GitHub `workflow_run`; Azure DevOps pipeline-resource trigger); a protected environment with a required approval | `make check ENV=prod` on that commit, then the approval gate |
| 5. Keep protected | `prod-maintain.yml` | a schedule | `make audit-schema ENV=prod` (read-only drift audit) |

**Auth.** Every Databricks credential is a service principal through OIDC
workload identity federation (`github-oidc` or `azure-devops-oidc`); no PATs
and no client secrets. The samples write `auth.auto.tfvars` with IDs only (an
empty `databricks_client_secret`) and set `DATABRICKS_AUTH_TYPE`. Your
Databricks Terraform provider and SDK must support that auth type. OIDC covers
the pipelines only; the deployment machine keeps its own `auth.auto.tfvars`.

**`make check` is read-only.** Each step runs in a scratch copy of the cloud
dir that holds the env files without credentials, Terraform state or runtime
records; the plan step alone gets the env's auth files. Nothing under
`envs/` is written and no applying target runs, so it is safe under `CI=true`.
The only file it can leave outside the copy is Terraform's own
`shared/roots/*/.terraform.lock.hcl` on a first plan, which is gitignored.

**v1 limits.**

- **Applying stages run on the deployment machine** until the env has remote
  state with locking: `make rehearse ENV=dev` (dev remote state is rollout
  step 12), `make release ENV=prod` and `make maintain ENV=prod` (prod remote
  state is deferred, design section 15). The operator runs them there, in
  order, in the machine's one persistent checkout: Terraform state, the env
  lock and Genie ID files live under its gitignored `envs/` today. Move it to
  the commit the pipeline names with `git fetch && git checkout --detach <sha>`
  and confirm `git status --porcelain` is empty; never `git clean -x` or
  re-clone. (State outside the checkout, `GENIERAILS_STATE_DIR`, is target.) Every applying
  target keeps refusing in CI, and the samples never set
  `GENIERAILS_ALLOW_CI_APPLY`, which exists only for GenieRails' own throwaway
  integration-test workspace. The dev and prod samples already carry the
  triggers, ordering, locks and approvals; their applying steps are marked
  `REQUIRES ... REMOTE STATE` and only report the commit to apply.
- **The shared account layer:** in deterministic envs `rehearse` and `release`
  only add tag-policy values; any other account change needs
  `make apply-account` on the deployment machine.
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

## Drift between releases

`make audit-schema ENV=prod` is read-only and safe to schedule in CI: it exits
`1` on untagged sensitive columns or tags on deleted columns. Fix drift with
`make maintain ENV=prod` on the deployment machine; it re-derives from prod's
tags with no LLM. GenieRails also ships an opt-in, read-only scheduled
Databricks job; see [Scheduled governance](scheduled-governance.md).

Independent environments that should not inherit dev governance (for example
one per business unit) are covered in [Advanced](advanced.md#independent-environments).
