# Deterministic-governance E2E driver

The driver encodes the champion flow from
`shared/docs/design/deterministic-governance.md` sections 9, 12, 13 and 17,
plan scenarios S1–S16, cross-cutting
manifest/concurrency checks, and the migration operation. Lifecycle mutations
are issued only as public `make` targets. Read-only snapshots cover UC grants,
column tags, effective ABAC policies, mask functions, Genie exports and ACLs,
Terraform serials, receipts, and manifests.

Always inspect the plan first; this neither loads auth nor invokes `make`:

```bash
python3 shared/scripts/e2e/driver.py --cloud aws --dry-run \
  --scenarios champion,s1,s14 --reports /tmp/gr-e2e-plan
```

Live execution is intentionally explicit and must use the persistent v1
deployment checkout and an external state directory:

```bash
python3 shared/scripts/e2e/driver.py --cloud aws \
  --repo "$GR_REPO" --checkout "$GR_REPO" --commit "$SHA" \
  --state-dir "$GENIERAILS_STATE_DIR" --reports "$ARTIFACTS" \
  --scenarios champion --confirm-live
```

The checkout must be customer-shaped, that is, it must commit `envs/`. The
upstream GenieRails `.gitignore` ignores `envs/`, so in a plain clone the
driver's local merge (`git add -A -- envs`) would fail. Before anything runs
(no log, git switch, snapshot or `make`), the driver checks, with read-only
git, that:

- the checkout is already at `--commit` (check it out first) and clean, so
  git's own ignore rules (every `.gitignore`, `.git/info/exclude`,
  `core.excludesFile`) are evaluated against exactly that tree;
- `<cloud>/.gitignore` has the block `make setup GITIGNORE=customer` writes;
- `<cloud>/envs/dev/env.auto.tfvars` and `<cloud>/envs/prod/env.auto.tfvars`
  are committed as regular files (mode `100644`/`100755`) in real
  directories, with no symlink anywhere in the path;
- `git check-ignore` ignores neither file.

If any check fails it exits non-zero with what is missing and the fix, and
runs nothing. Prepare the checkout once:

```bash
cd aws && make setup ENV=dev GITIGNORE=customer
git add .gitignore envs && git commit -m "Commit envs/ (customer repo)"
```

Then pass that commit (the checked-out `HEAD`) as `--commit`. The upstream root `.gitignore` is left
as it is: `<cloud>/.gitignore` re-includes `envs/`.

Live runs require an explicit 40-hex commit and persistent checkout. Before
each scenario and each step the driver verifies HEAD is the pinned commit
(`git checkout --force --detach <commit>` at scenario start). Applying targets
(`rehearse`, `release-plan`, `release`, `maintain`, operations) run only on a
clean tree; authoring targets (`setup`, `generate`, `promote-to`, `ungovern`)
may change `envs/`, and the scenario's next step commits that on a
`genierails-e2e/<scenario>-*` branch and merges it with `--no-ff` into the
driver's own main. That main lives in a bare repo under `<reports>/e2e-remote.git`
(git remote `genierails-e2e`), and every `make` runs with
`RELEASE_REMOTE=genierails-e2e`, so release-plan's "merged on main" rule holds
without pushing to `origin`.

Everything a scenario needs is checked before it touches anything. A missing
target (for example `capture`, `detach-agent` or the step-10 migration targets)
or a human gate without a TTY makes the whole scenario `NOT RUN`. An
unsupplied `--value` makes it `ERROR`. Either way no snapshot, git switch or
`make` runs. Placeholder text (`<...>`, `{...}`) and empty `KEY=` arguments
never reach `make`. A refusal step passes only with a non-zero exit, the exact
diagnostic the target prints, and identical before/after snapshots. Any other
failure is `ERROR`. S13 runs every command with `CI=true` and without
`GENIERAILS_ALLOW_CI_APPLY`.

Scenario values: `agent_a`, `wrong_agent`, `table` (covered), `uncovered_table`,
`treatment`, `ack_entry` (an exact `prod:<kind>:...` line), `manifest`,
`tampered_manifest`, and the 40-hex commits `u`, `o`, `r`, `r_plus_1`, `x`, `y`.

Snapshots read `<cloud>/envs/<env>/auth.auto.tfvars`
(`databricks_workspace_host`, `databricks_client_id`,
`databricks_client_secret`) and connect with OAuth M2M while `DATABRICKS_*`,
`ARM_*` and `AZURE_*` are hidden from the SDK. A client that resolves any other
host is refused. A failed query, an unreadable catalog, or an unreadable state
file is an error, never empty evidence.

Human gates need an interactive TTY and the typed scenario ID. A closed or
piped stdin aborts the run, and the scenario is `NOT RUN`. A wrong token is
`ERROR`. Ctrl-C, SIGTERM and SIGHUP stop the run as `interrupted`. In every
case the scenario's `finally` restores and writes a partial report. The restore
first resets the checkout to the commit. After any applying step it then
releases the commit's tree again as a forward fix on the driver's main
(`rehearse`, `release-plan`, `release MANIFEST=`, `maintain`). The driver then
re-snapshots dev and prod. A column that lost a tag, a removed mask or policy, a
newly exempted or uncovered principal, or a new grant compared with the
scenario's baseline is `RESTORE FAILED`. `<reports>/final-protection.json`
repeats that check against the run's first baseline. Logs and reports redact
secret-named keys, every value in the cloud's auth files, `Authorization`,
`Bearer` and `*_TOKEN=` values. Reports and state must be outside the checkout.
