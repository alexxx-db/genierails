# Scheduled Governance Checks

The opt-in scheduled job is read-only by default. On serverless compute it runs
the schema-drift audit (untagged sensitive-looking columns and stale
assignments) and the rulebook audit (live production tags with no covering
policy or mask):

```bash
python shared/scripts/run_scheduled_governance.py \
  --env-dir aws/envs/prod --step check \
  --config-source /Volumes/main/governance/genierails/envs
```

The job does not run make, Terraform, derivation, or apply, and it does not
write to the workspace. Existing serverless dependencies are sufficient. The
config source is required because `envs/` is gitignored. Point it at a
runtime-visible envs root containing both `account/` and the target environment
(for example `prod/`) to enable both audits. This gives the rulebook audit the
promoted account policies without relying on local Terraform state.
Relative values for both `--env-dir` and `--config-source` are resolved from
the repository root; absolute paths are used unchanged. To run the check
locally, point it at your own envs root:
`python3 shared/scripts/run_scheduled_governance.py --env-dir aws/envs/prod --step check --config-source aws/envs`.

On findings, the run fails and its output explains what was found. Open the run
from the failure notification, then run this from the authoritative GenieRails
checkout:

```bash
make maintain ENV=prod
```

Enable the job with `enable_scheduled_governance = true`, its Git URL and config
source, then `make apply ENV=prod`. Schedule, timezone, Git branch/provider,
dependencies, notifications, and job name use the existing
`scheduled_governance_*` variables.

The job is deployed from the data_access root
(`shared/roots/data_access/scheduled_governance.tf`), so a governance-only env
(no agents) gets it too. It used to be deployed from the workspace root. An
existing job is moved, never destroyed or recreated: before any command on
either root, `terraform_layer.sh` moves `databricks_job.scheduled_governance`
from `envs/<env>/terraform.tfstate` to `envs/<env>/data_access/terraform.tfstate`
(`+ moved databricks_job.scheduled_governance ...`), and the workspace root's
`removed` block makes a direct `terraform plan` there forget the job, never
destroy it. Run by hand, the move is:

```bash
cd "$(mktemp -d)" && terraform state mv \
  -state=<cloud>/envs/prod/terraform.tfstate \
  -state-out=<cloud>/envs/prod/data_access/terraform.tfstate \
  databricks_job.scheduled_governance databricks_job.scheduled_governance
```

After the move, the job's ID and URL outputs come from the data_access root
(`scheduled_governance_job_id`, `scheduled_governance_job_url`).

## Legacy mode

Set `scheduled_governance_mode = "legacy"` to retain the explicit legacy
`audit -> generate-delta -> coverage` flow. Its model-based delta step may
rewrite files in the ephemeral checkout but never applies them. The coverage
step runs `validate_abac.py --coverage-gate` with the generated config when
present, otherwise the split `data_access` config, and passes the target env
name for actionable failure guidance. The runner also
retains `--step all|audit|delta|coverage` for existing callers;
`--auth-file` and `--catalog` are legacy-only.

Before enabling legacy scheduled coverage, legacy envs generated before
`gr_treatment` existed must run `make generate ENV=<env>` and then
`make apply ENV=<env>` (review the plan as usual) to add `gr_treatment` tags.
Otherwise every scheduled run fails with
`classified but unprotected ... no gr_treatment`.

## Upgrading

The mode defaults to `"check"`, so existing jobs stop running legacy
`generate-delta`. If an existing config source still points directly at the old
per-environment directory, the job continues to run schema drift, prints a
warning, and skips rulebook drift because `account/abac.auto.tfvars` is not
available. Repoint `scheduled_governance_config_source` at the parent envs root
containing both `account/` and `<env>/` to enable rulebook drift. Set legacy
mode explicitly only if the old checkout-local delta rewrite is still required.
