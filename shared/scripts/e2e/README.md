# Deterministic-governance E2E driver

The driver encodes the champion flow, plan scenarios S1–S16, cross-cutting
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

The checkout is switched with `git checkout --force --detach <commit>` and is
required to be clean. Missing public targets are reported as `requires step N`
and automatically become runnable when those targets land. Human classification
and curation gates pause with an explicit prompt. Each scenario writes JSON and
Markdown reports plus sanitized command logs. Never put report/state output in
the repository.

