# Deterministic-governance end-to-end scenario plan

This checked-in plan is the reproducible source for
`shared/scripts/e2e/driver.py`. The normative behavior is defined by
`design/deterministic-governance.md`, especially sections 9, 12, 13 and 17.

The catalog contains the first champion deployment; operations S1–S16 from the
acceptance plan; manifest/concurrency refusals; and the step-10 legacy migration.
Every lifecycle mutation runs through a public Make target. Each scenario has
declared preconditions, commands, pass criteria, and restoration text in the
driver's `SCENARIOS` value, which is rendered into its JSON and Markdown plan.

Safety rules:

- A live run requires an explicit checkout, 40-hex commit, external state and
  report directories, and `--confirm-live`.
- HEAD is verified to be the pinned commit before every scenario and step;
  applying targets run only on a clean tree.
- Authored changes are committed on driver-owned `genierails-e2e/*` branches
  and merged into the driver's own main (remote `genierails-e2e`, used as
  `RELEASE_REMOTE`); `origin` is never pushed.
- Missing required targets make the entire scenario `NOT RUN`; no partial
  scenario is permitted.
- Human UI gates require a TTY and a typed scenario ID; closed stdin is `NOT RUN`.
- Refusals require the target's exact diagnostic and identical read-only
  snapshots; unrelated failures are errors.
- Snapshot failures and unresolved values are errors, never empty evidence.
- The baseline is restored in `finally` (also on Ctrl-C/SIGTERM) as a forward
  release of the commit's tree; no column or principal may be less protected,
  and no grant wider, than the baseline, per scenario and for the whole run.
- Logs and reports redact secrets, including every auth-file value.
- `--dry-run` reads only checked-in files and never invokes Make, Git, or the SDK.

The offline regression suite drives the live loop with fake Make, Git, and
snapshot adapters, plus a simulated live run (a throwaway Git repository and a
fake `make` on PATH) of S6 and the champion flow. Live Databricks execution is
deliberately excluded from unit tests.
