# Design: deterministic governance, decoupled from Genie agents

Status: **agreed** (normative spec). Review history is in the PR description.

## Goal

GenieRails has two layers with separate lifecycles:

| | Governance layer | Agent layer |
|---|---|---|
| Owns | treatment tags, mask functions, ABAC policies, row filters, coverage check, **who can read each table** (`table_readers`) | Genie config, `acl_groups`, `CAN_RUN` |
| Input | the governed set of tables (section 1) and reviewed config | the configured agents |
| Needs the other? | no | yes: its tables are governed first |

Governance is computed only from classification tags and reviewed config. It is the same for every agent and uses no AI. The champion flow uses both layers; governance can also run on its own (section 11).

## Decisions

| # | Decision |
|---|---|
| 1 | Three tiers: full access (raw), partial, fully masked. |
| 2 | Identifiers (national, tax, document and device IDs) are **redacted** for both masked tiers. The keyed hash is deferred (section 15). |
| 3 | Row filters only when declared. GenieRails never invents one. |
| 4 | An agent is run only by its `acl_groups`. A missing `acl_groups` stops the run; `[]` means nobody. |
| 5 | No AI in governance. `generate`, `rehearse`, `release` and `maintain` never call a model or require a model endpoint. AI may be used only as an explicit helper to draft Genie content, suggest an unmapped treatment, or draft custom-mask SQL; its output is reviewed and committed like any change, and never runs in CI or prod. |
| 6 | Governance works without any agent, using the existing `uc_tables`. |
| 7 | GenieRails creates `SELECT` **only** from `table_readers` (governance) and `CAN_RUN` **only** from agents' `acl_groups`. It doesn't revoke unrelated existing access except in the explicit migration (section 8). |
| 8 | `access_tier_groups` is promoted from dev, so prod uses exactly the rehearsed tiers. Where prod's group names differ, an optional env-owned `group_map` (like `catalog_map`) maps them; changing it counts as weakening. |
| 9 | Two entry points: **govern tables** (`uc_tables`, no agents) and **govern and promote agents** (the champion flow). GenieRails supports only governance it generates: masks or policies created in the UI or outside GenieRails on governed tables are not supported, and `rehearse`, `release` and `maintain` refuse while one exists (section 6). |
| 10 | The legacy AI-drafted mode exists only as a migration source. It is not documented as a path. |

## 1. The governed set

Governed set = top-level `uc_tables` ∪ tables of configured agents ∪ tables already governed (the governed-table list, so a table stays governed until `make ungovern`).

- `uc_tables` keeps its existing syntax: `schema.table` (relative to `uc_catalog`), `catalog.schema.table`, or `catalog.schema.*`.
- `uc_tables` **governs and never grants**, with or without agents.
- **Who can read a table** is governance, set in one place:

  ```hcl
  table_readers = {
    "cat.sch.customers" = ["payments_ops", "regional_analysts", "viewers"]
    "cat.sch.*"         = ["payments_ops"]                     # same key syntax as uc_tables
  }
  ```

  - **Overlapping keys are unioned:** a table's readers are the union of every entry that matches it (exact table, `catalog.schema.*`). `plan`, `generate` and `promote-to` print the resolved readers per table.
  - These groups get `SELECT`; nothing else in GenieRails grants it. A table in `table_readers` is governed automatically.
  - Every reader must be a tier group or in `raw_exempt_principals`; any other group is refused, so nobody gets an unexpected full mask.
  - New `SELECT` is gated by the coverage check. Adding a reader counts as widening and is checked by refuse-weakening (section 6).
  - `table_readers` is env-owned: the first `promote-to` seeds prod from dev, later promotions keep prod's value and print the differences.
  - No `table_readers` means GenieRails grants no `SELECT`.
- `make ungovern TABLE=...` is the only way out. It is an **authoring** command: it expands wildcards, refuses while any agent, `uc_tables` entry or wildcard still covers the table, and removes the table from the committed governed-table list. The next rehearse and release apply the removal.

For each table in the set:

1. **Treatment per column** from its `class.*` tag through the mask library (section 2). Several classes on one column: the strictest treatment wins (protection order, section 6).
2. **One policy per catalog, treatment and masked tier** (section 3), using a fixed, caller-independent function.
3. **Row filters** only where declared (section 4).
4. **Coverage check:** every `class.*` tag needs a mapping (section 6).

Same inputs give the same output. Adding an agent whose tables are already governed changes nothing in governance.

## 2. The mask library

GenieRails ships a mapping for all 93 Databricks classes. Each treatment has a fixed, reviewed **partial** and **full** version, typed for STRING, numeric, DATE, TIMESTAMP and TIMESTAMP_NTZ where it applies; any other type gets the full version. Policies are created only for treatments in use.

| Classes | Partial (tier 2) | Full (tier 3) |
|---|---|---|
| `credit_card` | last 4 | redacted |
| `bank_number`, `iban_code`, `swift_code`, `us_bank_number`, `uk_sort_code` | last 4 | redacted |
| `card_security_code`, `card_pin`, `card_track_data`, `secret` | **never raw**: redacted for every tier, including tier 1 | redacted |
| `card_expiration_date` | redacted | redacted |
| `us_ssn`, `us_itin`, passports, driver licences, all regional national, tax, social, health and voter IDs, `medical_record_number`, `health_plan_beneficiary_number`, `medical_license`, `medical_device_id`, `imei`, `vin`, `license_plate` | redacted | redacted |
| `email_address` | `j***@example.com` | redacted |
| `phone_number` | last 4 | redacted |
| `name` | initials | redacted |
| `date_of_birth`, `medical_date` | year only | NULL |
| `age` | 10-year band | NULL |
| `credit_score` | 50-point band | NULL |
| `compensation` | rounded | NULL |
| `ip_address` | network only (IPv4 /24; IPv6 redacted) | redacted |
| `mac_address` | vendor prefix | redacted |
| `url` | domain only | redacted |
| `location`, STRING | redacted | redacted |
| `location`, numeric lat/long | 1 decimal place (about 11 km) | NULL |
| health, biometric, genetic, ethnicity, religion, politics, sexual data and orientation, trade union, criminal background, marital and employment status | redacted | redacted |

"Redacted" is a fixed placeholder for text and NULL otherwise.

**Behaviour contract** (tested with a table of exact expected outputs per function, and live on a non-UTC session): NULL in gives NULL out; malformed input gets the full version; numbers round half away from zero; DATE becomes 1 January of the year; TIMESTAMP becomes the start of the year in UTC; bands include the lower bound; NaN and infinity become NULL.

**Changes from today:** `us_ssn` and `us_itin` move from last 4 to redacted for tier 2; IPv6 addresses are redacted for tier 2 instead of keeping a prefix.

**Config** (each treatment is configurable on its own; the library uses the same treatment names as the config):

```hcl
treatment_versions = { ssn = { partial = "last4" } }      # pick another shipped version for a treatment
column_overrides   = {                                      # one column: a stricter treatment, or a reviewed partial version
  "cat.sch.customers.postcode" = { partial = "prefix_3" }
}
tag_treatments     = { "sensitivity=confidential" = "redact" }   # customer-defined tags
```

A column override never changes the full version and can't make a never-raw treatment raw. Setting `partial = "raw"` anywhere counts as weakening (section 6).

## 3. The tier rule

```hcl
access_tier_groups    = ["payments_ops", "regional_analysts", "viewers"]  # full, partial, fully masked
raw_exempt_principals = ["etl-sp", "bi-service-account"]                  # optional, env-owned
```

- First group raw; last group full; any between partial. Two groups: raw and full. One group: raw only.
- **Policy shape** per catalog and treatment, each with a caller-independent function:
  - partial policy: `TO <tier-2 groups> EXCEPT <tier-1 groups, raw_exempt_principals>`;
  - full policy: `TO account users EXCEPT <tier-1 and tier-2 groups, raw_exempt_principals>`.

  GenieRails generates mutually exclusive masks: the partial and full principal sets are disjoint (checked by a test, including nested groups), and a check before every apply confirms no mask GenieRails didn't create also resolves on a governed column (section 6). A user in several tiers gets the most privileged one. Anyone outside the tiers gets the full version.
- **Precedence**, one rule: who sees what is decided first (never-raw > tier 1 and `raw_exempt_principals` > tier rule), then which version (`column_overrides` > `treatment_versions` > library). The deployer SP is in the tier-1 EXCEPT lists for ordinary treatments (verification needs raw rows), but **not** for never-raw treatments. It is named by the committed `deployer_principal_id` in each env's `env.auto.tfvars` (required in deterministic mode), never read from auth files, and applies refuse before the lock unless `databricks_client_id` matches it; never-raw columns are verified by checking every tier sees the redacted output.
- Tiers are governance: defined in dev and promoted (decision 8). If prod uses different group names, set an env-owned map in `envs/prod/env.auto.tfvars`:

  ```hcl
  group_map = { dev_analysts = "prod_analysts", dev_viewers = "prod_viewers" }
  ```

  `promote-to` maps every committed group reference it writes: `access_tier_groups`, `row_filters` `values_by_group`, and the first seed of `acl_groups` and `table_readers`. A dev group with no mapping is used as is, but every resulting group must exist in the target account (checked live in `release-plan`). A mapped name that doesn't exist, or a many-to-one mapping, is refused. Nested membership is expanded only after mapping, for reader checks and `verify-access`. Env-owned `raw_exempt_principals` are never mapped. Adding, removing or changing a mapping is a weakening (section 6) and needs an `ack`.

## 4. Row filters (declared only)

```hcl
row_filters = [
  { table = "cat.sch.payments", column = "region_code",
    values_by_group = { regional_analysts = ["APAC"] } }
]
```

- No config, no row filter, and no coverage gap.
- All rules for one table compile into one function and one ABAC policy whose scope is that table. Rules on the same table must name the same groups; otherwise the config is refused.
- Named groups see only their values (string literals, case-sensitive, NULL never matches); a user in several named groups sees the union. Tier 1 and `raw_exempt_principals` see all rows; every other group sees **none**. Naming a tier-1 group is refused.

## 5. Agents

- **Capture is explicit:** `make capture ENV=dev SPACE="<agent>"` writes that agent's exported `serialized_space` to `envs/dev/agents/<agent>.space.json`. Only the outer fields (`space_id`, `etag`, ACLs) are removed; the item IDs inside the payload (instructions, sample questions, joins, snippets, benchmarks) are kept, because the format needs them for a faithful round-trip. A plain `generate` only imports agents not yet in git (it writes files; no Terraform state changes).
- **Per-agent files:** `envs/<env>/agents/<agent>.auto.tfvars` holds `genie_space_id`, `acl_groups` and `delete`. The prod agent ID is kept in prod's Terraform state and in the release receipt, never committed by a pipeline; if state is lost it is recovered from the receipt, or by title adoption (#79). A loader merges these files before plan.
- **Access:** `acl_groups` get `CAN_RUN` on the agent, nothing else. `generate`, `rehearse` and `release` stop if any `acl_groups` group can't read one of the agent's tables through `table_readers`, and name the table and the missing group.
- **Champion convenience:** when `generate` first imports an agent, it writes a `table_readers` entry for each of the agent's tables that no entry (exact or wildcard) already covers, filled with the agent's `acl_groups`, for review and commit. It never edits an existing entry.
- **`acl_groups` is env-owned:** the first `promote-to` seeds prod from dev; later promotions keep prod's value and print the differences.
- **Dev:** agent config is never overwritten; GenieRails applies governance and access for captured tables only, and creates an agent only if missing.
- **Prod:** every `release` overwrites the agent's config from git (full replacement). Any field the API rejects fails the release; nothing is skipped silently. It prints a field-level report of what it overwrote, never recreates the agent and never changes its ID, so conversations stay.
- **Promotion remap** is JSON-aware: catalog names and the warehouse are replaced on identifier boundaries inside string values, never in keys or prose.
- **Removing an agent** takes two steps:
  1. **A PR** sets the agent's `acl_groups` to `[]` and `detached = true`. Released normally, this revokes `CAN_RUN`. Table access is unchanged, because it belongs to `table_readers`.
  2. **`make detach-agent ENV=<env> SPACE="<agent>"`** (operations stage) refuses unless step 1 is released, then runs `terraform state rm` for the agent's exact resource addresses. A later PR removes the agent from config, and its plan must show no destroy.

  The agent and its conversations stay. Removing an agent from config while it is still in state is refused. `delete = true` deletes it instead and shows in the promotion PR.

## 6. Safety rules

| Rule | Behaviour |
|---|---|
| Coverage check | New or wider access needs a recent pass. |
| Unmapped class | Blocks new access. `maintain` and `release` apply a fail-safe **never-raw redacted** treatment to the column (every tier, including tier 1, since an unknown class may be one that must never be raw) and report it until a mapping is promoted or the column is in `ack.txt`. |
| Prod classification complete | `release` refuses if a column classified in dev (remapped through `catalog_map`) has no `class.*` tag in prod. `promote-to` writes the expected list to `generated/expected_classification.json`. |
| Protection order | raw < partial versions < redacted/NULL. |
| Refuse weakening | `release-plan`, `release` and migration compare each SELECT holder and column against live state; `promote-to` compares committed inputs only. Any weakening, including new `raw_exempt_principals`, `partial = "raw"`, or removing or widening a row filter, stops the run unless acknowledged in `ack.txt`. |
| Reader check | Before the first deterministic apply of an env, and whenever governance newly covers a table, GenieRails computes the table's **current readers** that GenieRails didn't grant: `SELECT` and `ALL PRIVILEGES` at table, schema and catalog level (from `information_schema` privileges), owners of the table and its parents, readers of every view that depends on it (view-level and inherited `SELECT`/`ALL PRIVILEGES`, following views of views), and metastore admins, with group membership expanded transitively. ABAC masks still apply to view readers, because they're evaluated as the querying user. Every reader must be in a tier, in `raw_exempt_principals`, or in `ack.txt`; otherwise the run stops. Principals that can **grant** access (`MANAGE`, owners, including view owners) are reported separately as a risk. Groups a row filter would leave with zero rows are listed the same way. |
| Account layer | The account layer (groups, group members, tag policies) is shared by every env. `rehearse` and `release` never change it, except to **add** allowed values to the treatment tag policy that a new treatment needs. Any other account change runs only through `make apply-account` in the operations stage, with an approval. If the account plan isn't otherwise empty, `rehearse` and `release` refuse and print that command. Every tag-value addition takes the global **account lock**, in local and pipeline runs alike. |
| Policy changes | Any change that replaces or removes a policy or function runs as separate applies under the env lock: (1) create new functions and policies, removing nothing; (2) verify with `SHOW EFFECTIVE POLICIES` and queries as every affected tier; (3) retag or move principals; (4) verify; (5) remove the old policies and functions. Outside phase 5, policies and functions are protected from destroy. A brief `MULTIPLE_MASKS` error is acceptable only for the principals being moved; any raw value fails the run. Run tier moves as maintenance windows. |
| External masks | Before every `rehearse`, `release` and `maintain`, GenieRails inventories effective policies and table-attached masks on governed tables. One it didn't create on a governed column stops the run, because it would break queries with `MULTIPLE_MASKS`. |
| Tags | One treatment tag key for everything. Each column has one treatment-tag resource keyed by `entity_type|entity_name|tag_key` (never by value) with no `ignore_changes`, so value changes update in place (verified in the provider). Classifier-owned `class.*` tags are never managed by Terraform. Order for a column moving to a new treatment: add the value to the tag policy, create the new treatment's policies, then retag. A column is never tagged with a value no policy matches. |
| CI | Every applying target refuses in CI except exactly `make rehearse ENV=dev` in the dev pipeline, on `<cloud>/envs/dev` itself (not a promote-to destination), and only once dev has remote state with locking. The applies it runs through recursive makes pass only while they inherit that rehearse's env lock. Prod and account applying targets still refuse. |
| Coverage freshness | A pass counts only for the same commit (`check`) and the same live snapshot (`rehearse`, `release`, `maintain`). |

**Acknowledgements live in a committed file**, `envs/<env>/ack.txt`, one typed and fully scoped entry per line, so every acknowledgement is reviewed, code-owned and part of the receipt fingerprint. Each line includes the environment again so copying a file to a different environment fails closed:

```text
prod:weaken:cat.sch.t.col:etl-sp
prod:unclassified:cat.sch.t.col
prod:reader:cat.sch.t:bi-sp
prod:rollback:<commit>
```

Each entry names the env, kind and exact object. GenieRails prints the
before/after consequence of each and refuses entries that match nothing after
all checks (weakening, reader and unclassified) are reconciled together, once.
Entries for another env are refused. The weakening keys are
`<env>:weaken:<column|table|treatment|group_map>:<principal-or-mapping>` (`*` for
readers outside a removed row filter, `partial` for `partial = "raw"`, and
`<dev_group>=<target_group>` for `group_map`); reader keys are
`<env>:reader:<table>:<principal>`.
Repositories must add `envs/*/ack.txt` to `CODEOWNERS` using the existing
governance-owner team or users; the project deliberately does not prescribe an
organization-specific owner name. The `ACK` environment variable is accepted only for `rehearse ENV=dev`, never for prod.

**Residual risks (stated, not solved):**
- Owners, metastore admins and `MANAGE` holders can grant `SELECT` outside GenieRails at any time; acknowledgements can't catch that. Keep ownership and `MANAGE` off human principals; `maintain` reports new readers on its next run.
- Readers with schema- or catalog-level `SELECT` can read a new table raw until it is classified and `maintain` runs. Run `maintain` on a schedule.
- In dev, tables added to an agent in the UI but not yet captured are not governed.
- Reader inventory is bounded by the deployer's Unity Catalog visibility.
  `information_schema` returns only the grants the caller may see, so the
  check fails unless the deployer is the metastore admin (the metastore owner,
  read from the API) or an owner or `MANAGE` holder of each inventoried
  securable or a parent, including the schemas and catalogs of dependent
  views. It also fails if a table, view, schema or catalog is missing or has
  no visible owner, or if any query fails or times out. An incomplete
  inventory is never treated as empty. Group membership comes from the
  workspace SCIM API (users by user name/email, service principals by
  application id, nested groups by name).
- Delta Sharing recipients and file-level paths such as `READ FILES` are not
  covered by the v1 table-reader inventory. Govern those paths separately and
  do not treat this report as proof that they are absent.

## 7. Verification

`verify-access` checks each tier against its exact expected output: it reads paired raw rows, applies the pure partial or full function, and compares exactly. Where two tiers' expected outputs are identical (for example redacted IDs), they are compared by equality, not reported as inconclusive. A sample that can't distinguish tiers whose expected outputs differ is inconclusive, never a pass. Temporary test principals are created per tier and per row-filter group; group-membership waits (about 5 minutes) are batched into one wait per run.

## 8. Migration for existing deployments

- A report-only dry run comes first, on the live env.
- The report covers every column and every SELECT holder: what they see today and after. It lists agents missing `acl_groups`, and writes a proposed `table_readers` that reproduces every `SELECT` GenieRails grants today, so nothing is revoked by surprise. Any grant left out of the accepted `table_readers` is listed as a **revocation**.
- Weakening and revocations stop the run unless listed in `envs/<env>/ack.txt`. There is no separate accept flag.
- An inventory of masks GenieRails didn't create (`ALTER … SET MASK`, old policies) runs first; they block cutover until removed or acknowledged.
- New functions are created under new names; policies change in tighten-before-loosen phases; old functions are kept until no policy uses them. Tag state moves with generated `terraform state mv`.
- Existing AI-drafted row filters are frozen as they are until declared ones replace them.
- Cutover is reported done only after a fresh classification read and a passing tier-aware verification.
- Rollback: one PR reverts **both** the source change and its promotion (or reverts the source change and re-runs `promote-to`), adds `prod:rollback:<commit>` to `envs/prod/ack.txt`, and goes through the normal stages. The previous version's functions and policies are kept until a later release has succeeded, so masks switch back in place.

## 9. Files and version control

`make setup` writes a `.gitignore` that commits `env.auto.tfvars`, `agents/`, `generated/`, `ack.txt` and capture files, and ignores `auth.auto.tfvars`, state, locks and `generated/.live_refresh.json`.

- **Only authoring commands write committed files** (`capture`, `generate`, `scaffold-treatments`, `promote-to`, `ungovern`). Applying commands (`rehearse`, `release`, `maintain`, `detach-agent`) write only to ignored runtime files: Terraform state, receipts, lock files and live snapshots. So an applying run never leaves the tree dirty.
- **`promote-to` is a pure function of the commit.** It reads only committed files: dev's generated output and prod's committed env-owned files, including each env's `deployer_principal_id` (the application ID of the SP that applies it, which the policies exempt; promotion swaps dev's for prod's). It never reads auth files, live prod or prod state, so `promote-to` on any machine, its `CHECK=1` mode and the receipt digest all produce the same bytes. Anything that needs live prod (reader check, refuse-weakening, kept deployed policy names) runs in `release-plan`, before approval. Generated files are never hand-merged: on conflict, run `make generate` on the latest `main`. `promote-to` writes only the files it owns; env-owned keys (`acl_groups`, `table_readers`, `raw_exempt_principals`, `group_map`, `deployer_principal_id`) are never overwritten and are excluded from the generated-file equality check in section 10. Changes to them are still subject to refuse-weakening.

## 10. Change lifecycle and team workflow

| | Governance change | Agent change |
|---|---|---|
| Made in | files in git | the dev UI, then `make capture` |
| Reaches prod | when merged, rehearsed and released | when captured, merged, rehearsed and released |

| Stage | What runs | Touches dev? |
|---|---|---|
| PR | read-only: validate, generated files current, coverage check, mask tests, `make plan ENV=dev` | no |
| Merge to `main` | `make rehearse ENV=dev` on the new `main` | yes, one at a time |
| Promote | a person runs `make promote-to ENV=prod` and opens the promotion PR | no |
| Release | after the promotion PR is merged and rehearse has passed on that merge commit: the prod pipeline (section 17), or in v1 the operator on the deployment machine | prod |

- **Rehearse writes a receipt per commit** (append-only, keyed by commit) next to dev's state: commit, clean-tree flag, fingerprint of code and inputs, result, target env mapping, and the digest of what `promote-to` produces for each target. If the dev fingerprint matches a passing receipt, rehearse writes a receipt for the new commit without re-applying, and still recomputes the `promote-to` digests.
- **`release` recomputes everything and refuses unless** `HEAD` equals the receipt's commit, the tree is clean, `HEAD` is on freshly fetched `origin/main`, the fingerprints match, and the digest of `envs/prod/generated` matches the receipt. It also refuses a commit that is an ancestor of the last release (an out-of-order or stale approval) unless `<env>:rollback:<commit>` is acknowledged. The promotion PR's merge commit is the one that must pass rehearse, so code merged after it can't reach prod untested.
- **Unfinished agents** never reach git, because capture is explicit. Promoting A carries B's last captured version unchanged.
- **Removing an agent revokes `CAN_RUN` only. Removing a `table_readers` entry revokes the `SELECT` it granted.** Masks stay until `make ungovern`.
- A failed rehearse blocks release until a fix merges and rehearse passes. CI also regenerates each open promotion PR and fails it if it differs.
- v1 runs every applying target from one deployment machine; the env lock allows one run at a time. No hand edits in prod; remove `CAN_EDIT` on prod agents from everyone except the deployer SP.
- Until dev has remote state (`envs/dev/remote_state.hcl`, rollout step 12b), the operator runs the post-merge rehearse.

## 11. Flows

**Champion flow (with agents):**

1. `make setup ENV=dev`; set `access_tier_groups`; per agent, set `genie_space_id` and `acl_groups` in its agent file.
2. Classify the dev catalog in the UI; turn on auto-tagging; wait for `class.*` tags.
3. `make generate ENV=dev`: imports the agents, derives governance, and proposes `table_readers` for the agents' tables. Review and commit.
4. `make rehearse ENV=dev`.
5. Set `catalog_map`; `make promote-to ENV=prod`; classify prod in the UI.
6. Approve and merge the promotion PR; once rehearse passes on that commit, the operator runs `make release ENV=prod`.
7. `make maintain ENV=prod` on a schedule.

**Governance-only flow (no agents):** the same commands with `uc_tables` set and no agents. GenieRails creates no agent. It grants `SELECT` only if `table_readers` is set; otherwise masks apply to whoever already has access, after the reader check (section 6).

## 12. Scenarios

| Scenario | What happens |
|---|---|
| Promote A while B has unfinished dev edits | Capture A, merge, rehearse, promote, release. Prod B gets its last captured version unchanged. |
| A adds a table | Capture records it; governance derives its masks; the promotion PR shows both; prod applies masks, then A's access. |
| A and B share a table | Masks are shared and unchanged; only A changes. |
| Mask change while B is mid-curation | Governance change; rehearse, promote, release. B's config is untouched in dev and prod. |
| New sensitive column in prod | `maintain` applies the library mask, or the fail-safe redaction if unmapped. |
| Grant another group access to A in prod | Add the group to A's `acl_groups` in `envs/prod/agents/A.auto.tfvars` and, if it can't already read A's tables, to `table_readers`; PR, release. |
| Remove agent A | A PR sets `acl_groups = []` and `detached = true` and is released (`CAN_RUN` revoked); then `make detach-agent`; then a PR removes it from config with a no-destroy plan. Table access and masks unchanged. |
| Change who can read a table | Edit `table_readers` (dev, then promote, or prod directly as an env-owned change), PR, release. Additions are checked as widening. |
| Stop governing a table | `make ungovern TABLE=...` edits the committed governed-table list (refused while anything still covers it); a PR, rehearse and release apply the removal. |
| Roll back | One revert PR covering the source change and its promotion, with `prod:rollback:<commit>` acknowledged; normal stages; masks switch back in place. |
| Upgrade GenieRails | A governance change: upgrade, rehearse, promote, release. |
| Several teams | Per-agent files with code owners; a central team owns governance files. |

## 13. What a user learns

The README shows one golden-path config (three tiers, one agent, `catalog_map`); every optional setting lives in an advanced doc.


- **Settings:** `access_tier_groups`, `table_readers`, `uc_tables`, `catalog_map`, optional `group_map`, per-agent `genie_space_id` / `acl_groups` / `delete`, and optionally `raw_exempt_principals`, `treatment_versions`, `column_overrides`, `tag_treatments`, `row_filters`. Legacy `governance_mode` exists only as migration input.
- **One acknowledgement file:** `envs/<env>/ack.txt` (env-scoped entries, code-owned).
- **Commands:** `setup`, `generate`, `capture`, `rehearse`, `promote-to`, `release-plan`, `release`, `maintain`; occasionally `ungovern`, `detach-agent`, `apply-account`, `scaffold-treatments`.

## 14. Platform checks (before step 4)

- ABAC policy limits per securable and per metastore, and the tag-policy allowed-value limit, against a realistic governance-only env.
- Treatment-tag value updates in place on a live column with the new resource key.
- Genie round-trip fidelity for every supported field type: export, import into a scratch space, export again; the diff must be empty, and conversations must survive an update.
- Each check runs on AWS and Azure.

## 15. Deferred

- **Keyed hash** for identifiers. It needs its own design: a UC secret, a Python UDF, fallback rules and key handling, plus about 8 s per query measured on AWS dev.
- Group-level overrides of the tier rule.
- Merge queue, CI releases to prod, and remote state for prod. Without a merge queue, section 17's in-order dev runs and the re-check of open PRs on every push to `main` are mandatory.

## 16. Rollout

Every step keeps `main` working and ships its own live test on AWS and Azure where it touches Databricks. Existing deployments are untouched until step 9.

1. **Schemas, CI guard and committed `envs/`** (done in #108, plus the `.gitignore`).
2. **Tier-aware verification** and the prod classification check (done in #109; add equality for identical tiers).
3. **Mask library** (#110).
4. **Platform checks** (section 14), then **stable tags and the governed set**: tag resources keyed by entity and key; the governed set from `uc_tables`, agent tables and the governed-table list; `make ungovern` (#112).
5. **Governance-only policies, no grants:** per-tier policies, `raw_exempt_principals`, fail-safe redaction for unmapped classes, the external-mask inventory, and a test that no grant targets `account users`.
6. **Policy changes and tier moves:** the staged-apply protocol and its verification.
7. **Reader and weakening checks** with committed, scoped acknowledgements (`ack.txt`).
8. **Access:** `SELECT` only from `table_readers`, `CAN_RUN` only from `acl_groups`; per-agent files; the agent-readability check and the proposed `table_readers` in `generate`. Built (except per-agent files, which move to step 11 with capture), extended with the live agent coverage check that replaces the `CAN_RUN` gate's dependence on data_access state, and `group_map` (decision 8) ([access.md](../access.md)).
9. **Release gating, then migration:** the rehearse receipt and `release` attestation first (built: rehearse receipts, `release-plan` and `release MANIFEST=`, the release receipt and `maintain` gating, the runtime overlay of section 9; [cicd.md](../cicd.md#release-gating-deterministic-envs)); then the migration dry run, revocation list, frozen AI row filters, tag state moves and staged cutover.
10. **Declared row filters.**
11. **Capture and prod ownership:** `make capture`, JSON-aware remap, prod IDs in state and the release receipt, overwrite report, two-step detach.
12. **Pipelines:** `make check`; per-commit receipts; remote state for dev and the dev pipeline; GitHub Actions and Azure DevOps samples for each stage in section 17. The prod pipeline stays on the deployment machine until remote state for prod exists.
13. **Docs** (golden path plus advanced), then a full live run on AWS and Azure.

## 17. Where each command runs

One command per stage. People run the authoring commands on their own machine; pipelines run the rest.

| Stage | Where | Command | Changes Databricks? |
|---|---|---|---|
| **1. Author** | a person's machine | `make capture ENV=dev SPACE="<agent>"` (agent), `make generate ENV=dev` (governance), `make scaffold-treatments` (new class), `make promote-to ENV=prod` (promotion); commit, open a PR | no; files only |
| **2. PR check** | CI, every PR | `make check ENV=dev`; also `make check ENV=prod` when the PR touches `envs/prod/**` | no |
| **3. Dev rehearsal** | **dev pipeline**, every merge to `main` that touches code, governance or `envs/**`; every commit gets its own run, in order | `make rehearse ENV=dev` | dev only |
| **4. Prod release** | **prod pipeline**, when stage 3 **succeeds** on a commit that touches `envs/prod/**`: `make release-plan ENV=prod` publishes the approval manifest; after a required approval, `make release ENV=prod MANIFEST=<manifest>` applies it | `release-plan`, then `release` | prod |
| **5. Keep protected** | **prod pipeline**, on a schedule, checked out at the last released commit (read from the release receipt) | `make maintain ENV=prod` | prod, governance only |
| **6. Operations** | **prod pipeline**, manual dispatch, one approval per operation and per phase | `make detach-agent`, `make apply-account`, migration phases, phased policy changes (each with the provenance rules below) | prod or account layer |

A promotion PR is just a PR whose diff includes `promote-to` output. It may also carry the dev change it promotes, in one PR. Prod-only PRs (`acl_groups`, `table_readers`, `ack.txt` in `envs/prod`) follow the same stages: rehearse on the merge commit is a no-op that writes a receipt, and the safety comes from the prod plan, refuse-weakening and the reader check.

**Two-phase release.**
1. **`make release-plan ENV=prod`**, under the prod lock, reads prod state and live policies and builds an ignored runtime overlay that keeps deployed policy names (#68: it fails closed when policies can't be listed, and refuses a collision with an unmanaged policy). From the committed promotion output plus that overlay it saves a Terraform plan for every root, runs the reader and refuse-weakening checks, and writes one immutable **manifest**: commit, state serial per root, plan file hashes, live-snapshot digest, the kept-name mapping and the reports.
2. The approval is for that manifest.
3. **`make release ENV=prod MANIFEST=<manifest>`** applies only those saved plans, then verifies. It refuses, and asks for a fresh `release-plan` and approval, if the commit, any state serial, the live snapshot or any plan hash has changed. The release receipt records the manifest.

A change that needs an intermediate replan (staged policy changes, tier moves, migration) never goes through `release`; it runs as operations, with a manifest and an approval per phase.

**Provenance for operations.** Every applying command enforces its own provenance, independent of pipeline settings:
- `detach-agent`: HEAD is the last released commit, and that release had the agent with `detached = true`.
- `apply-account`: clean `origin/main` HEAD; a saved account plan and manifest; approval; the account lock; it writes an account receipt.
- Migration and policy phases: a clean checkout whose `HEAD` is on freshly fetched `origin/main`, with a matching passing dev receipt, and not older than the last release unless `<env>:rollback:<commit>` is acknowledged; one manifest and approval per phase, each recording the commit and this provenance result.

**`make check ENV=<env>`** is read-only. It runs `validate`, a regeneration that fails if any committed generated file differs, `validate-generated`, the offline coverage check, the mask-library unit tests and `plan`. With `ENV=prod` it also runs `make promote-to ENV=prod CHECK=1`, which regenerates into a scratch directory and fails if the committed output differs. Before an env has remote state, `plan` is a structural plan against empty state; state-aware plans start with remote state.

**What each pipeline needs:**

| Pipeline | Trigger | Credentials | State and concurrency |
|---|---|---|---|
| PR check | pull request; re-run for open PRs on every push to `main` | dev and prod read-only SPs | reads the dev receipt store |
| Dev | merge to `main` (path filter) | dev deployer SP | dev remote state with locking; append-only receipt store; runs queue in order and are **never cancelled** (GitHub: a queue job, no `cancel-in-progress`; Azure DevOps: Exclusive Lock with `lockBehavior: sequential`) |
| Prod | success of the dev pipeline on a commit touching `envs/prod/**` (GitHub `workflow_run`; Azure DevOps pipeline-resource trigger); a schedule; manual dispatch | prod deployer SP in a protected environment; read-only access to the dev receipt store | prod remote state with locking; one run at a time; pending runs superseded by a newer release are cancelled |

- **Auth:** every Databricks credential is a service principal through OIDC workload identity federation (GitHub OIDC, Azure DevOps service connection). No PATs. Read-only SPs need `USE CATALOG`, `USE SCHEMA`, `BROWSE`, read on `system.information_schema` and `CAN_VIEW` on the agents. The dev and prod deployer SPs also need account-level rights to add allowed values to the treatment tag policy; only the operations identity may run `apply-account`.
- **Repo settings:** `main` requires `check ENV=dev` (and `check ENV=prod` for `envs/prod/**`), up-to-date branches, CODEOWNERS approval on `envs/prod/**`, `ack.txt` and governance files, and no direct pushes, including admins. The prod environment requires a reviewer from the governance code owners, forbids self-approval and deploys only from `main`.
- **No pipeline writes to git.**
- **Failures alert the owning team**, including `maintain` reporting a new reader or an unmapped class.

**Rules that make this safe:**
- Only the dev and prod pipelines apply anything; each applying command checks its receipt, manifest or lock itself (section 10, two-phase release, provenance for operations).
- Prod releases only a commit on `main` that passed stage 3, matching its receipt, never older than the last release unless a rollback is acknowledged.
- `release` writes a **release receipt** (commit and digests); `maintain` refuses unless it runs from that commit with matching digests.
- Acknowledgements are committed and reviewed (section 6), never pipeline parameters.

**v1 vs later.** Until an env has remote state with locking, its applying stages run on the **deployment machine** with the same commands and the same checks, and every applying target refuses in CI. The deployment machine keeps Terraform state, receipts and locks **outside the checkout**, in `GENIERAILS_STATE_DIR` (one folder per env plus one for the account layer), takes a machine-wide lock per env and one for the account layer, and runs each command from a fresh, clean checkout of the commit being applied. Dev now has opt-in S3+DynamoDB or Azure Blob remote state and its pipeline may run only `make rehearse ENV=dev`; its receipt is published append-only for read-only use by prod. Prod remote state remains deferred (section 15), so prod and account applies remain on the deployment machine. Stages 1 and 2 work in CI from v1. GenieRails ships a sample per stage for GitHub Actions and Azure DevOps.

## 18. Next iteration (agreed, not yet scheduled)

Agreed in design discussion after the rollout above was set. It starts once rollout steps 1–13 are built and pass end to end. Nothing here changes sections 1–17; where it refines them, this section wins.

### 18.1 Personas and the files each owns

| Persona | Changes | Owns (CODEOWNERS) |
|---|---|---|
| **Champion** (first deployment) | one agent, end to end | everything, once |
| **Data citizen** | curates an agent in the dev UI (including which tables it reads), captures it, promotes it | `envs/<env>/agents/<agent>.*` |
| **Governance engineer** | treatments, `tag_treatments`, tiers, `group_map`, `table_readers`, row filters, top-level `uc_tables`, `ungovern`; approves every change to the governed-table set | `envs/<env>/env.auto.tfvars`, `envs/<env>/generated/` (including `governed_tables.json`), `ack.txt` |
| **One team doing both** (small or medium company) | both kinds of change, often in one PR | both sets |

- `env.auto.tfvars` holds only governance and env-owned settings. Agent settings live only in `agents/` files (section 5), so an agent-only PR never touches `env.auto.tfvars`.
- **An agent owns its table list; governance owns the governed set and all access.** The tables an agent reads are agent-owned (`agents/<agent>.*`, captured from the dev UI); there is no second, governance-owned per-table approval list. Instead, `generated/governed_tables.json` (governance-owned, committed) always records the full governed set: the sticky governed tables, and under `agent_tables` each file-defined agent's tables. `generate`, `capture` and `promote-to` rewrite it, so an agent change that adds or removes a table, even one with no classified column, is always a diff in a governance-owned file, and CODEOWNERS routes it to governance. `make check` (and `promote-to CHECK=1`) fails while it differs from a fresh regeneration, so it can't be skipped. The record grants nothing: `SELECT` comes only from `table_readers` in `env.auto.tfvars`, and `generate`, `rehearse` and `release` refuse `CAN_RUN` while an `acl_groups` group can't read one of the agent's tables. A `catalog.schema.*` entry in `table_readers` is governance's choice to cover every governed table in that schema, including one an agent adds later.
- **Customer class mappings never live in `shared/`.** `shared/` is GenieRails product code and is replaced on upgrade. `scaffold-treatments ENV=<env> TREATMENT=<name>` writes the mapping to the env's `tag_treatments` (and the promotion carries it), not to `shared/treatment_config.json`.
- **`promote-to` keeps the split.** It writes agent output under `envs/<target>/agents/` and governance output under `envs/<target>/generated/`, so CODEOWNERS can require only the owners of what changed. A table that only an agent file lists stays in the target's agent file, never in its `env.auto.tfvars`. An agent-only promotion that changes no table needs no governance approval; one that changes a table also changes the target's `governed_tables.json`, so it needs governance approval, as does any promotion that changes governance.
- **`generate` proposes, it doesn't decide, for governance-owned files.** The `table_readers` entries it seeds for a new agent (section 5) are a proposal the governance owner approves in the same PR.

### 18.2 Pipeline setup

- **`make bootstrap-pipelines ENV=<env> CI=github|azure-devops`**: creates the read-only service principal per env and the OIDC federation for each pipeline identity, grants exactly the privileges section 17 lists, and prints the repo variables, environments (with required reviewers), branch-protection settings and a `CODEOWNERS` matching 18.1. It is idempotent, has a `PLAN=1` dry run like `bootstrap-sp`, and never writes to the repo.
- **The prod stage triggers on `shared/**` changes too,** not only `envs/prod/**`, because a product or mapping change can change prod's generated output.
- **The scheduled governance job is deployed from the data_access root,** so governance-only envs (no agents) get it.

### 18.3 First deployment in git

- `make setup` writes the section 9 `.gitignore`. It writes it only into a customer repo: `GITIGNORE=customer` opts in once, then every `make setup` refreshes its managed block in `<cloud>/.gitignore`; the GenieRails repo never has the block, so `make setup` never changes it there ([version-control.md](../version-control.md#setting-up-git-tracking)).
- The champion walkthrough ends with a commit step: what to `git add`, and opening the first PR.

### 18.4 Optional AI helpers (decision 5)

Explicit, opt-in commands, never called by `generate`, `rehearse`, `release` or `maintain`, never run in CI:

- `make suggest-treatment CLASS=<class>`: proposes a treatment and mask for an unmapped class, for review.
- `make draft-genie-content SPACE=<agent>`: drafts instructions, sample questions and benchmarks for an agent that has none.
- `make draft-custom-mask ENV=dev TREATMENT=<name> TYPE=<sql-type>`: writes a draft to `envs/dev/drafts/custom_masks/<name>.sql` and an exact-output test fixture to `envs/dev/drafts/custom_masks/<name>.json`, for review. `tag_treatments` can't reference the draft until `make test-custom-mask ENV=dev TREATMENT=<name>` passes the mask library's exact-output tests.

Output is written to files for review and committed like any change. A missing model endpoint only disables these helpers.

### 18.5 Docs, organised by persona

```
README.md (root)                     start here; pick: govern tables, or govern and promote agents
shared/examples/dev_to_prod/README   champion flow
shared/docs/agents.md                data citizen playbook
shared/docs/governance.md            governance engineer playbook
shared/docs/operations.md            day-2 changes, including the one-team path (from #111)
shared/docs/pipelines.md             DevOps setup for GitHub Actions and Azure DevOps (replaces cicd.md)
shared/docs/deployment-machine.md    the v1 deployment machine
shared/docs/legacy.md                legacy mode, as a migration source only
REFERENCE.md                         every command and setting
```

Each playbook follows the champion README's style: the flow in one code block, short numbered steps, a "pass looks like" line, and what to do when it stops. `playbook.md`, `version-control.md`, `from-ui-to-production.md` and `self-service-genie.md` are merged into these and retired.

### 18.6 Proving it

- **Live runs per persona** on AWS and Azure: champion; data citizen (promote agent A while B is unfinished); governance engineer (mask change while an agent is mid-curation; tier move; new class); one team (both in one PR); governance only (no agents).
- **Real pipelines:** the section 17 stages running in GitHub Actions and Azure DevOps against a test repo with OIDC and `bootstrap-pipelines`, including the refusals (unrehearsed commit, older commit, stale promotion PR, `maintain` from a non-released commit).
- **Migrating the existing live AWS and Azure deployments** from legacy mode with step 9's migration, with the revocation report reviewed before cutover.

### 18.7 Code health

- Split `generate_abac.py` along the governance and agent layers, with no behaviour change, after step 11.

### 18.8 Rollout (continues section 16)

14. Persona file ownership (18.1): `tag_treatments` for customer mappings, `promote-to` output split, CODEOWNERS sample. Built: every reader (generate, the ACL sidecar, promote) merges `agents/` files, and generate never imports a captured agent's live config into `generated/`; `promote-to` carries agents before the split and prints the changed files by owner; `shared/examples/CODEOWNERS.example`; `tag_treatments` keys are `class.<name>` mapped to mask-library treatments (other tag keys are refused, since this version reads only `class.*` tags), written by `scaffold-treatments` to the env (a promote-to target's source); legacy envs keep their inline agents until the explicit `make split-agent-files`, which refuses any move that would change the plan.
15. `make setup` `.gitignore` and the walkthrough commit step (18.3); scheduled job from the data_access root and the `shared/**` prod trigger (18.2). Built: an existing job moves from the workspace state to the data_access state before any command on either root (`move_scheduled_job.py`), and the workspace root's `removed` block forgets rather than destroys it.
16. `make bootstrap-pipelines` (18.2). Built ([cicd.md](../cicd.md#setting-up-the-pipelines)).
17. Optional AI helpers (18.4). Built: `make suggest-treatment`, `draft-genie-content` and `draft-custom-mask` (`scripts/ai_helpers.py`) run only with `ENV=dev` at the checkout's real `envs/dev`, write only inside the real `envs/dev/drafts/` (no symlinks, staged writes), refuse in CI, send the model only schema (never data or an agent's existing text), and read the model endpoint from `ai_helper_endpoint` in `envs/dev/env.auto.tfvars`; `make test-custom-mask` runs the mask library's exact-output rules on a draft (offline, then each fixture case, whose inputs must be typed literals, on the dev SQL warehouse, read-only) and records the digests it passed. A pass does not adopt the mask. Nothing reads `drafts/` (the rehearse fingerprint and `make check`'s copy skip it), and a test walks every target and module `generate`, `rehearse`, `release`, `maintain` and `check` reach and runs them under a model tripwire. `tag_treatments` still names only mask-library treatments; applying a custom mask in governance is not built.
18. Persona docs (18.5).
19. Live persona runs, real pipeline runs, and migration of the existing deployments (18.6).
20. `generate_abac.py` split (18.7).

Still deferred: the items in section 15. Accepted residual risks are unchanged: a new table can be read raw until it is classified if readers already have schema-level access, and owners and admins can grant access outside GenieRails.
