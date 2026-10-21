# Ship a Genie agent from dev to prod

Take a Genie agent you've curated in **dev** and ship it to **prod** with its sensitive columns masked. Databricks Data Classification finds the sensitive columns; GenieRails turns that into masks and access rules as code. No new business access is granted until every classified sensitive column the agent can reach is covered by a mask policy, checked against freshly read tags.

## The whole flow

```bash
# your own repo first: make check, rehearse receipts and release-plan read committed envs/
git remote rename origin genierails      # keep GenieRails for upgrades
git remote add origin <your-repo-url>

# dev
make setup ENV=dev          # writes the .gitignore that commits envs/; copy the templates; set deployer_principal_id and groups; per agent, its ID and acl_groups
git add .gitignore envs/ && git commit -m "Govern the first agent in dev" && git push -u origin main
#   Catalog Explorer: turn on Data classification, review detections, enable auto-tagging
make generate ENV=dev       # imports the agent, finds its tables, drafts the rules
git add envs/ && git commit -m "Draft the rules" && git push
make check ENV=dev          # read-only check of the committed files (rehearse doesn't run it)
make rehearse ENV=dev       # applies in dev and proves the masks work (verify-access: ALL EFFECTIVE)

# prod (same for stg or any env)
make setup ENV=prod         # set deployer_principal_id, groups and catalog_map
make promote-to ENV=prod    # copies the reviewed rules
#   Catalog Explorer: same classification review on the prod catalog
git add .gitignore envs/ && git commit -m "Promote the rules to prod" && git push
make check ENV=prod         # also runs promote-to CHECK=1 (release-plan doesn't run it)
make rehearse ENV=dev       # on this new commit: release-plan needs a dev rehearsal of it (dev isn't re-applied)
make release-plan ENV=prod  # plans prod and writes the manifest to approve
make release ENV=prod       # applies exactly the approved plan, proves the masks
make maintain ENV=prod      # run on a schedule
```

<a id="prerequisites--gather-required-values"></a>
## Before you start

```bash
git clone https://github.com/databricks-solutions/genierails.git
cd genierails/aws           # or: cd genierails/azure — every make command runs from here
```

| You need | Notes |
|---|---|
| The [prerequisites](../../docs/prerequisites.md) | Tools, a deploying service principal (`make bootstrap-sp`), IdP-synced groups |
| Your Genie **Agent ID** | Genie UI → open the agent → **Configure** → *About this agent*. *No agent yet?* Use the [sample environment](SAMPLE_ENV.md) |
| Who may run the agent | The groups that get Genie `CAN_RUN` on it (its `acl_groups`), chosen from your access-tier groups. Required: `[]` means nobody |
| Deploying SP application ID | The `databricks_client_id` from each env's `auth.auto.tfvars`; it is committed as `deployer_principal_id` (not secret) |
| Access-tier groups | Your existing groups, most- to least-privileged, e.g. `payments_ops`, `regional_analysts`, `viewers`. Order matters: a tier listed above a masked tier also gets the agent and sees those columns unmasked |
| Dev and prod catalog names | e.g. `dev_finance` and `prod_finance` |

<a id="phase-0--dev-set-up"></a>
<a id="phase-1--dev-scan-draft-and-test-rules"></a>
## Dev

**1. Set up.**
```bash
make setup ENV=dev
cp ../shared/examples/dev_to_prod/env.auto.tfvars.example envs/dev/env.auto.tfvars
mkdir -p envs/dev/agents
cp ../shared/examples/dev_to_prod/agent.auto.tfvars.example "envs/dev/agents/Finance Analytics.auto.tfvars"
```
Fill in `envs/dev/auth.auto.tfvars` (service principal and workspace). The template starts new champions with `governance_mode = "deterministic"`; existing env files are not changed. In `envs/dev/env.auto.tfvars`, replace the deployer and group placeholders:
```hcl
deployer_principal_id = "<the dev auth.auto.tfvars databricks_client_id>"
access_tier_groups = ["payments_ops", "regional_analysts", "viewers"]
```
**Agents (preferred):** one file per agent in `envs/dev/agents/`; the file name is the agent's name. In it, set the agent's ID and who may run it:
```hcl
genie_space_id = "01ef0000000000000000000000000000"   # Genie UI > agent > Configure > About this agent
acl_groups     = ["regional_analysts", "viewers"]      # who gets CAN_RUN on the agent; [] means nobody
```
**Also accepted:** the same fields inline in `env.auto.tfvars` as `genie_spaces = [{ name = "...", genie_space_id = "...", acl_groups = [...] }]` (leave `genie_spaces = []` when using agent files). Prefer `agents/` files so agent and governance PRs stay separate; `make split-agent-files` moves an inline list into files later.
`acl_groups` is required: `make generate` stops while an agent has none. It grants only `CAN_RUN` on the agent. `SELECT` on the agent's tables comes only from `table_readers` in `env.auto.tfvars`, which `make generate` proposes for you (filled with `acl_groups`) for each table nothing covers yet. Every `acl_groups` group must be able to read every table of the agent, so pick them from `access_tier_groups` (only tier groups and `raw_exempt_principals` can be readers); otherwise `generate`, `rehearse` and `release` stop and name the table and group.

**2. Commit it (own repo).** `make check` reads only committed env files, a rehearse receipt counts only for a clean checkout, and `make release-plan` needs that commit on your `origin/main`. In a plain GenieRails clone `envs/` is git-ignored, so `make check` stops. Put the checkout in your own repo (create an empty one first) before going on:
```bash
git remote rename origin genierails      # keep GenieRails for upgrades
git remote add origin <your-repo-url>
make setup ENV=dev                       # origin is your repo now: writes .gitignore (commits envs/, ignores credentials, state and runtime files)
git add .gitignore envs/
git status                               # pass looks like: no auth.auto.tfvars, *.tfstate or .genie_* files
git commit -m "Govern the first agent in dev"
git push -u origin main
```
Commit (and push) again after each step below that changes `envs/`. Later changes go through PRs: [Commit it](#commit-it).

<a id="dev-classify"></a>
**3. Classify (in the UI).** In **Catalog Explorer**, open the dev catalog → **Details** → **Data classification**: turn it on. When the scan finishes (the first one can take up to about a day), review the detections, exclude false positives, and turn on auto-tagging. Then wait until the columns show `class.*` tags such as `class.email_address`: open a table in Catalog Explorer and check its **Columns** tab.

**4. Generate.**
```bash
make generate ENV=dev
```
It captures each new agent to `envs/dev/agents/<agent>.space.json` and proposes its `table_readers`. Review those and the drafted rules in `envs/dev/generated/`, and edit the rules if you need to. If the tags aren't there yet, it stops and tells you to wait; re-run it later. Re-runs keep whatever is already in `envs/dev/generated/`, including your edits, and only add rules for new columns. Then commit and push `envs/`.

**5. Check.**
```bash
make check ENV=dev
```
A read-only check of what git tracks under `envs/`, in a scratch copy: validate, generated files current, validate-generated, the offline coverage check, mask tests and a structural plan. It is the PR check; `make rehearse` and `make release-plan` don't run it. It stops at the first failing step.

**6. Rehearse.**
```bash
make rehearse ENV=dev
```
It applies in dev, then runs `verify-access` as its last gate (`_verify-access-gate`), querying as each test tier.
**Pass looks like:** `ALL EFFECTIVE`: each test tier sees masked values, the authorized tier sees raw ones. To re-run only that proof later: `make verify-access ENV=dev`.

<a id="phase-2--prod-set-up-and-promote-rules"></a>
<a id="phase-3--prod-scan-real-data"></a>
<a id="phase-4--prod-release-and-verify"></a>
## Prod

**1. Set up and promote.**
```bash
make setup ENV=prod
```
Fill in `envs/prod/auth.auto.tfvars`, replace its `deployer_principal_id` and `access_tier_groups` placeholders, and set:
```hcl
catalog_map = { "dev_finance" = "prod_finance" }   # dev catalog = prod catalog
```
Then:
```bash
make promote-to ENV=prod
```
Using the sample environment? Load its tables into prod first (`--skip-agent`, see [SAMPLE_ENV.md](SAMPLE_ENV.md)).
Commit and push the promoted files to `main` (`git add .gitignore envs/`, commit, `git push`): `make setup ENV=prod` added prod's lines to `.gitignore`, and `make rehearse` and `make release-plan` need a clean checkout.

**2. Classify (in the UI).** Same as dev, on the prod catalog. Prod's real data may surface sensitive types dev never had.

**3. Check, rehearse, plan, release.** Check the committed files, rehearse dev on the promotion commit, plan and review the manifest, then apply exactly what was approved:
```bash
make check ENV=prod
make rehearse ENV=dev
make release-plan ENV=prod
make release ENV=prod
```
`make release-plan ENV=prod` plans only a commit on your `origin/main` that has a passing `make rehearse ENV=dev` of that same commit (dev is prod's `promote_from` in this walkthrough; with `promote_from = "stg"` it is `make rehearse ENV=stg`); the dev step 6 rehearsal was of an earlier commit. Otherwise it stops with `no passing rehearsal of <commit>`. Since the promotion changed only `envs/prod/`, this rehearse doesn't apply anything in dev again. It prints `code and dev inputs are unchanged since the passing rehearsal of <earlier commit>; wrote a receipt for <this commit> without re-applying` and the `promote-to ENV=prod digest` it recorded. If code or `envs/dev/` changed since, it runs the full rehearsal. Rehearse again after any later commit, before its `release-plan`.
`make check ENV=prod` also runs `promote-to CHECK=1`. `make release` releases the manifest the last `release-plan` wrote, printing its path, commit and changes first. It refuses if that manifest was already released, if a newer `release-plan` replaced it, or if anything changed since it was planned. A pipeline can name the manifest explicitly with `MANIFEST=<path>`.
**Pass looks like:** `ALL EFFECTIVE` (from its `verify-access` gate) and "Release complete". Business users can now open the agent and get masked answers. To re-run only that proof later: `make verify-access ENV=prod`.

**If it stops on coverage or drift**, prod found a sensitive tag your rules don't cover. Nothing new was granted. The message prints both fixes with exact commands:
- **Reuse an existing mask** (the common case): `make scaffold-treatments ENV=prod TREATMENT=<name>` adds the class to `tag_treatments` in `envs/dev/env.auto.tfvars` (`shared/` is unchanged). Commit it, then `make generate ENV=dev`, `make check ENV=dev`, `make rehearse ENV=dev`, `make promote-to ENV=prod`, `make check ENV=prod`, `make rehearse ENV=dev` (again, on the promotion commit), `make release-plan ENV=prod` and `make release ENV=prod`, committing and pushing after each step that changes `envs/`.
- **A new kind of mask**: draft and test it with the dev-only helpers (`make draft-custom-mask` / `make test-custom-mask`, see the [governance playbook](../../docs/playbooks/governance.md)). Applying a custom mask in governance (`make adopt-custom-mask`) is not on `main` yet. The bare `make scaffold-treatments` stub and `materialize-treatment` are legacy-mode only ([legacy mode](../../docs/legacy-mode.md)).

Details: [Fixing a coverage gap found in prod](REFERENCE.md#fixing-a-coverage-gap-found-in-prod).

<a id="phase-5--prod-maintain-coverage"></a>
## Keep it safe

```bash
make maintain ENV=prod      # schedule it (cron or CI)
```
It masks newly tagged columns, never grants access past a passing coverage check, and never changes the Genie agent. If it stops, it prints what to do: usually review a new detection in Catalog Explorer, or add a missing rule in dev and re-promote.

<a id="commit-it"></a>
## Commit it

[Dev step 2](#phase-0--dev-set-up) puts the checkout in your own repo: the remote rename, `make setup` writing `.gitignore`, and the first `git add .gitignore envs/`. Do it before `make check`, `make rehearse` or `make release-plan`; if you skipped it, run it now from `aws/` (or `azure/`) and commit what you have.

From then on every change goes through a PR into `main`, with `make check` as its check (`make release-plan` plans only a commit on `origin/main`): see [Pipelines](../../docs/pipelines.md) to set up CI, and the [agents](../../docs/playbooks/agents.md), [governance](../../docs/playbooks/governance.md) or [one-team](../../docs/playbooks/one-team.md) playbook for day-2 changes.
