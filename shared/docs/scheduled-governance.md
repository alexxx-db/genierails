# Scheduled Governance Checks

## Deterministic envs: use your own scheduler

GenieRails doesn't create a scheduled job for a deterministic env. Setting
`enable_scheduled_governance = true` in a deterministic env's `env.auto.tfvars`
is refused by `make setup`, `check`, `plan`, `promote-to`, `release-plan`,
`maintain` and the other commands that read it, before anything is planned or
written. If the env already has the job, removing the setting shows
`databricks_job.scheduled_governance[0]` as a destroy in the next release plan.

Schedule the checks with your own scheduler (cron, a CI scheduler, an
orchestrator):

- `make maintain ENV=<env>` reconciles governance. Run it on the deployment
  machine, from the last released commit with a clean tree; it refuses
  anything else.
- `make audit-schema ENV=<env>` and `make audit-rulebook ENV=<env>` only
  report (untagged sensitive columns and stale assignments; applied tags with
  no covering policy or mask). A non-zero exit means findings.

For example, a crontab entry on the deployment machine, in the checkout that
ran `make release ENV=prod`:

```cron
0 6 * * * cd /srv/genierails/aws && make maintain ENV=prod >> /var/log/genierails-maintain.log 2>&1
```

The scheduler's `PATH` needs the same `make`, `python3` and `terraform` as your
shell. Use the deployment machine's existing `auth.auto.tfvars`; never copy it,
or any other secret, anywhere else for the scheduler.

## Legacy envs: the Terraform-managed job (deprecated)

Legacy envs keep the opt-in Terraform-managed job exactly as before; it is
deprecated, and new setups should use your own scheduler as above. By default
it is read-only. On serverless compute it runs the schema-drift audit and the
rulebook audit:

```bash
python shared/scripts/run_scheduled_governance.py \
  --env-dir aws/envs/prod --step check \
  --config-source /Volumes/main/governance/genierails/envs
```

The job does not run make, Terraform, derivation, or apply, and it does not
write to the workspace. Its Git checkout holds only committed files, and the
upstream GenieRails repo doesn't commit `envs/`, so the config source is
required: a runtime-visible envs root containing `account/` and the target env
(for example `prod/`, with `env.auto.tfvars` and `data_access/`). Leave
`auth.auto.tfvars` and every other secret out of it: the default check mode
then authenticates as the job's run-as identity. (The legacy delta step below
reads `auth.auto.tfvars`, so it does not run without one; use check mode.)
Relative `--env-dir` and `--config-source` values are resolved from the
repository root. To run the check locally:
`python3 shared/scripts/run_scheduled_governance.py --env-dir aws/envs/prod --step check --config-source aws/envs`.

On findings, the run fails and its output explains what was found. Run
`make maintain ENV=prod` from the authoritative GenieRails checkout.

Enable the job with `enable_scheduled_governance = true`, its Git URL and config
source in the legacy env's `env.auto.tfvars`, then `make apply ENV=prod`.
Schedule, timezone, Git branch/provider, dependencies, notifications, and job
name use the existing `scheduled_governance_*` variables.

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

### Legacy mode

Set `scheduled_governance_mode = "legacy"` to retain the explicit legacy
`audit -> generate-delta -> coverage` flow. Its model-based delta step may
rewrite files in the ephemeral checkout but never applies them. The coverage
step runs `validate_abac.py --coverage-gate` with the generated config when
present, otherwise the split `data_access` config, and passes the target env
name for actionable failure guidance. The runner also
retains `--step all|audit|delta|coverage` for existing callers;
`--auth-file` and `--catalog` are legacy-only.

Legacy `make maintain` deliberately fails closed when the schema-drift audit
reports drift, including a name-pattern false positive (a column such as
`patient_count` whose name looks sensitive but has no `class.*` or governed
tag). A legacy env's tags come from an LLM draft, so maintain cannot prove the
column is safe to leave unmasked, and there is no way to acknowledge a finding.
If the column is sensitive, review its native UC classification detection,
enable auto-tagging in Catalog Explorer, wait for the `class.*` tag, and re-run
`make maintain ENV=<env>`. If it is a false positive, exclude it in Catalog
Explorer; maintain stays blocked while the audit still flags the column, so
migrate the env to deterministic governance, where maintain reports schema
drift as advisory and continues through its coverage and rulebook gates.

Before enabling legacy scheduled coverage, legacy envs generated before
`gr_treatment` existed must run `make generate ENV=<env>`, then
`make validate-generated ENV=<env>` and `make plan ENV=<env>`. Expect existing
legacy mask policies to be replaced by `gr_mask_*` ones; review the plan, then
run `make apply ENV=<env>` to add `gr_treatment` tags. Otherwise every scheduled
run fails with `classified but unprotected ... no gr_treatment`.

### Upgrading

The mode defaults to `"check"`, so existing jobs stop running legacy
`generate-delta`. If an existing config source still points directly at the old
per-environment directory, the job continues to run schema drift, prints a
warning, and skips rulebook drift because `account/abac.auto.tfvars` is not
available. Repoint `scheduled_governance_config_source` at the parent envs root
containing both `account/` and `<env>/` to enable rulebook drift. Set legacy
mode explicitly only if the old checkout-local delta rewrite is still required.
