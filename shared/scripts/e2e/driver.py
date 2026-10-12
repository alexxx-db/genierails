#!/usr/bin/env python3
"""Run the deterministic-governance acceptance plan through public Make targets.

The runner is deliberately data driven.  Live discovery is read-only; every
mutation is delegated to a documented public Make target.  ``--dry-run`` does
not load authentication, run git, invoke make, or contact Databricks.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


PLAN_SOURCE = "/tmp/genierails-e2e-plan.md"
DESIGN_SOURCE = "shared/docs/design/deterministic-governance.md"
SECRET_KEYS = re.compile(r"(?i)(secret|token|password|private[_-]?key)")


@dataclasses.dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...] = ()
    env: str | None = None
    target: str | None = None
    requires_step: int | None = None
    human_prompt: str | None = None
    expect_failure: bool = False
    assertions: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    preconditions: tuple[str, ...]
    steps: tuple[Step, ...]
    pass_criteria: tuple[str, ...]
    restore: tuple[str, ...]
    requires: tuple[str, ...] = ()


def mk(name: str, target: str, env: str | None = None, *, step: int | None = None,
       args: Sequence[str] = (), fail: bool = False, assertions: Sequence[str] = ()) -> Step:
    command = ("make", target) + ((f"ENV={env}",) if env else ()) + tuple(args)
    return Step(name, command, env, target, step, expect_failure=fail,
                assertions=tuple(assertions))


DAY2 = (
    mk("PR check (dev)", "check", "dev", step=12),
    mk("Dev rehearsal", "rehearse", "dev"),
    mk("Promote deterministic output", "promote-to", "prod"),
    mk("PR check (prod)", "check", "prod", step=12),
    mk("Promotion determinism", "promote-to", "prod", args=("CHECK=1",)),
    mk("Rehearse promotion merge", "rehearse", "dev"),
    mk("Create immutable release manifest", "release-plan", "prod", step=9),
    mk("Apply approved manifest", "release", "prod", step=9, args=("MANIFEST={manifest}",)),
    mk("Governance-only maintenance", "maintain", "prod"),
)


def _scenario(key: str, title: str, pre: Sequence[str], lead: Sequence[Step], passed: Sequence[str],
              restore: Sequence[str], requires: Sequence[str] = ()) -> Scenario:
    return Scenario(key, title, tuple(pre), tuple(lead), tuple(passed), tuple(restore), tuple(requires))


SCENARIOS: tuple[Scenario, ...] = (
    _scenario("champion", "First champion deployment", (
        "Fresh prefixed dev agent A and matching test-only dev/prod tables exist.",
        "Catalog mapping, tier groups, acl_groups and external state directory are reviewed.",
    ), (
        mk("Scaffold dev environment", "setup", "dev"),
        Step("Review dev classification", human_prompt="In dev Catalog Explorer review classification, exclude false positives, enable auto-tagging, then press Enter."),
        mk("Generate reviewed dev configuration", "generate", "dev"),
    ) + DAY2[:3] + (
        Step("Review prod classification", human_prompt="In prod Catalog Explorer review classification and confirm expected_classification.json, then press Enter."),
    ) + DAY2[3:], (
        "Prod grants, masks, tags, agent export/ACL and receipts match committed governance.",
        "Second release-plan and maintain are idempotent; agent ID and conversations are stable.",
    ), ("Release delete=true for disposable agents; remove governed fixtures through reviewed lifecycle changes.",)),
    _scenario("s1", "Promote A while B has unfinished dev edits", (
        "A and B have released captured baselines; make an unmistakable UI-only edit to dev B.",
    ), (Step("Curate only A", human_prompt="Curate dev A, leave B unfinished, then press Enter."),
        mk("Capture A only", "capture", "dev", step=11, args=("SPACE=A",))) + DAY2, (
        "Only A changes; dev B remains UI-edited and prod B stays byte-identical with stable conversations.",
    ), ("Revert/capture A through lifecycle; manually discard or separately capture B edit.",)),
    _scenario("s2", "Mask-only change while B is mid-curation", ("Begin an uncaptured edit to dev B; review a mask version/override change."),
        (Step("Leave B mid-curation", human_prompt="Leave B's UI edit uncaptured, apply the reviewed config edit, then press Enter."),
         mk("Regenerate governance", "generate", "dev")) + DAY2,
        ("Only governance changes; B config, ID and conversations do not change; staged replacement never exposes raw data.",),
        ("Revert governance change through the same lifecycle.",), ("Staged policy operation may require step 6 operation targets.",)),
    _scenario("s3", "Agent adds a new table", ("Create uniquely prefixed, classified dev/prod test table pair."),
        (Step("Add table to A", human_prompt="Add the dev table to A in the UI and review classification, then press Enter."),
         mk("Capture A", "capture", "dev", step=11, args=("SPACE=A",)), mk("Generate governance", "generate", "dev")) + DAY2,
        ("Unreadable table is refused; once readers are reviewed, prod is masked before access and identifiers are remapped.",),
        ("Remove table from A, capture/release, ungovern, then drop disposable fixtures.",)),
    _scenario("s4", "New sensitive column appears in prod", ("Add a known-class prefixed column via an approved external fixture migration."),
        (mk("Maintain released commit", "maintain", "prod"), mk("Prove idempotence", "maintain", "prod")),
        ("Known class is masked in one run for every tier; no Genie update occurs.",),
        ("Remove disposable column through its owning migration.",)),
    _scenario("s5", "Unmapped class fail-safe", ("Add prefixed unknown_secret with an unmapped class tag; no acknowledgement."),
        (mk("Plan protected release", "release-plan", "prod", step=9), mk("Maintain fail-safe", "maintain", "prod"),
         mk("Scaffold reviewed treatment", "scaffold-treatments", "dev", args=("TREATMENT={treatment}",))),
        ("Gap is reported and every principal sees never-raw redaction; new access is blocked.",),
        ("Promote a reviewed mapping or remove the fixture tag/column; prove no stale fail-safe policy.",)),
    _scenario("s6", "table_readers widening requires acknowledgement", ("Prepare a prod-owned reader widening with stale coverage and no matching ack."),
        (mk("Refuse stale/unacknowledged widening", "release-plan", "prod", step=9, fail=True),) + DAY2,
        ("Negative plan creates no applicable manifest; exact reviewed ack permits only the named grant.",),
        ("Revert reader and acknowledgement through lifecycle; verify only GenieRails-owned SELECT is revoked.",)),
    _scenario("s7", "Detach agent", ("A is released, detached=true and acl_groups=[] have been released."),
        (mk("Refuse wrong commit/agent", "detach-agent", "prod", step=11, args=("SPACE=wrong",), fail=True),
         mk("Detach A state only", "detach-agent", "prod", step=11, args=("SPACE=A",))) + DAY2[:2],
        ("Only A resource addresses leave state; live space, conversations, grants and masks remain.",),
        ("Use documented adoption/reattachment; otherwise retain disposable agent for explicit cleanup.",)),
    _scenario("s8", "Stop governing a table", ("Disposable table is in governed list; prepare each covering-source negative case."),
        (mk("Refuse while covered", "ungovern", "dev", step=4, args=("TABLE={table}",), fail=True),
         mk("Edit governed list", "ungovern", "dev", step=4, args=("TABLE={table}",))) + DAY2,
        ("Every coverage source is named/refused; only reviewed lifecycle removes policies/tags.",),
        ("Re-add via uc_tables/agent/readers and lifecycle, or drop disposable table.",)),
    _scenario("s9", "Rollback", ("R1 and visible-change R2 are released; exact prod rollback ack is reviewed."), DAY2,
        ("Bad/missing ack refuses before mutation; exact ack restores R1 behavior without agent recreation/raw exposure.",),
        ("Forward-fix through lifecycle and remove obsolete acknowledgement.",)),
    _scenario("s10", "Stale promotion PR refusal", ("Create promotion at M1, advance main to M2 and leave promotion stale."),
        (mk("Refuse stale generated output", "promote-to", "prod", args=("CHECK=1",), fail=True),
         mk("Refuse stale prod check", "check", "prod", step=12, fail=True)),
        ("Scratch regeneration differs and checks fail without workspace mutation.",), ("Delete simulator branches/worktrees.",)),
    _scenario("s11", "Release refuses unrehearsed or older commit", ("Fixture receipts model U, N and ancestor O around last release R."),
        (mk("Refuse candidate provenance", "release-plan", "prod", step=9, fail=True),
         mk("Refuse foreign manifest", "release", "prod", step=9, args=("MANIFEST={manifest}",), fail=True)),
        ("Unrehearsed, dirty, off-main, older and mismatched-manifest cases refuse with no mutation.",), ("Remove negative-case branches only.",)),
    _scenario("s12", "Maintain refuses off released commit", ("Release receipt points to R while main is R+1."),
        (mk("Refuse R+1", "maintain", "prod", fail=True), mk("Maintain exact R", "maintain", "prod")),
        ("Off-receipt/dirty/digest mismatch refuse before mutation; exact R is governance-only.",), ("Return persistent checkout to requested commit.",)),
    _scenario("s13", "CI apply guard", ("No authorized pipeline provenance; CI=true is set by the scenario fixture."),
        tuple(mk(f"Refuse CI {t}", t, "dev" if t == "rehearse" else "prod", step=11 if t == "detach-agent" else None,
                 args=(("SPACE=A",) if t == "detach-agent" else ()), fail=True)
              for t in ("rehearse", "release-plan", "release", "maintain", "detach-agent")) +
        (mk("Refuse CI account apply", "apply-account", "account", step=9, fail=True),),
        ("Every unauthorized apply fails with zero API calls and unchanged state/locks; authoring/check remain usable.",), ("Unset CI markers.",)),
    _scenario("s14", "Governance-only environment", ("Test env has prefixed uc_tables, no agent files/IDs, initially no table_readers."),
        (mk("Setup", "setup", "dev"), mk("Generate governance only", "generate", "dev")) + DAY2,
        ("No Genie resource/CAN_RUN is created; first run grants no SELECT, reviewed second run grants only table_readers.",),
        ("Remove readers, ungovern, then remove disposable tables.",)),
    _scenario("s15", "Two PRs merged then one promotion", ("X changes A; Y changes governance/B; ordered merge commits are available."),
        (mk("Rehearse X", "rehearse", "dev"), mk("Rehearse Y", "rehearse", "dev")) + DAY2[2:],
        ("Append-only receipts exist for X/Y/promotion; one cumulative prod release has no missing/duplicate change.",),
        ("Cumulative revert or ordered reverts followed by one promotion.",)),
    _scenario("s16", "Prod UI drift overwritten; conversations retained", ("Released prod A has a conversation and a recorded unsupported UI edit."),
        (Step("Make prod drift", human_prompt="Record A export/conversation IDs, make a recognizable prod UI edit, then press Enter."),) + DAY2,
        ("Overwrite report names drift; config equals Git after remap; ID/conversations remain; no recreation.",),
        ("Restore desired content via dev UI, capture and lifecycle.",)),
    _scenario("manifest", "Manifest tamper and concurrency refusals", ("Disposable fixtures and an approved manifest exist."),
        (mk("Refuse tampered manifest", "release", "prod", step=9, args=("MANIFEST={tampered_manifest}",), fail=True),),
        ("Commit, plan bytes, serial, live snapshot and kept-name changes each invalidate the old manifest; locks serialize applies.",),
        ("Remove each external grant/mask through its owning fixture immediately.",)),
    _scenario("migration", "Legacy migration operations", ("Reviewed report-only legacy fixture and revocation report exist."),
        (mk("Migration report", "migration-report", "prod", step=9), mk("Migration cutover", "migration-apply", "prod", step=9)),
        ("Every column/reader consequence is reviewed; phases tighten before loosen; verification is fresh.",),
        ("Rollback both source and promotion using exact rollback acknowledgement.",), ("Target names are supplied by rollout step 9 implementation.",)),
)


class SnapshotProvider(Protocol):
    def capture(self, env: str) -> Mapping[str, Any]: ...


def canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): canonical(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        normalized = [canonical(v) for v in value]
        return sorted(normalized, key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return value


def snapshot_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable top-level changes; ordering-only differences disappear."""
    left, right = canonical(before), canonical(after)
    keys = sorted(set(left) | set(right))
    return {key: {"before": left.get(key), "after": right.get(key)}
            for key in keys if left.get(key) != right.get(key)}


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): "<redacted>" if SECRET_KEYS.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class CommandRunner:
    def __init__(self, cwd: Path, log_dir: Path, extra_env: Mapping[str, str] | None = None):
        self.cwd, self.log_dir = cwd, log_dir
        self.env = {**os.environ, **(extra_env or {})}

    def run(self, argv: Sequence[str], label: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(list(argv), cwd=self.cwd, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        safe = re.sub(r"(?i)(secret|token|password)(\s*[=:]\s*)\S+", r"\1\2<redacted>", result.stdout)
        (self.log_dir / f"{slug(label)}.log").write_text(safe, encoding="utf-8")
        return result


class LiveSnapshotProvider:
    """Read-only SDK/SQL snapshot using env auth.auto.tfvars (values never logged)."""
    def __init__(self, cloud_root: Path, state_dir: Path):
        self.cloud_root, self.state_dir = cloud_root, state_dir

    @staticmethod
    def _tfvars(path: Path) -> dict[str, str]:
        if not path.is_file():
            raise RuntimeError(f"missing authentication file: {path}")
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^\s*([A-Za-z_][\w]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$', line)
            if m:
                values[m.group(1)] = bytes(m.group(2), "utf-8").decode("unicode_escape")
        return values

    def capture(self, env: str) -> Mapping[str, Any]:
        try:
            from databricks.sdk import WorkspaceClient  # type: ignore
        except ImportError as exc:
            raise RuntimeError("live snapshots require databricks-sdk") from exc
        env_dir = self.cloud_root / "envs" / env
        auth = self._tfvars(env_dir / "auth.auto.tfvars")
        kwargs = {k: v for k, v in {
            "profile": auth.get("databricks_profile") or auth.get("profile"),
            "host": auth.get("databricks_host") or auth.get("host"),
            "client_id": auth.get("databricks_client_id") or auth.get("client_id"),
            "client_secret": auth.get("databricks_client_secret") or auth.get("client_secret"),
            "azure_tenant_id": auth.get("azure_tenant_id"),
        }.items() if v}
        client = WorkspaceClient(**kwargs)
        catalog = _read_catalog(env_dir / "env.auto.tfvars")
        warehouse = auth.get("databricks_warehouse_id") or auth.get("warehouse_id")
        sql = _sql_snapshots(client, warehouse, catalog) if warehouse and catalog else {}
        spaces = _agent_ids(env_dir)
        agents: list[dict[str, Any]] = []
        for name, space_id in spaces:
            exported = client.genie.get_space(space_id).as_dict()
            acl = client.permissions.get("genie", space_id).as_dict()
            agents.append({"name": name, "space_id": space_id, "export": exported, "acl": acl})
        return redact({
            "uc": sql,
            "agents": agents,
            "state_serials": _state_serials(self.state_dir / env),
            "receipts": _receipt_inventory(self.state_dir / env),
        })


def _sql_snapshots(client: Any, warehouse: str, catalog: str) -> Mapping[str, Any]:
    queries = {
        "catalog_grants": f"SELECT * FROM system.information_schema.catalog_privileges WHERE catalog_name = '{_sql_lit(catalog)}'",
        "schema_grants": f"SELECT * FROM system.information_schema.schema_privileges WHERE catalog_name = '{_sql_lit(catalog)}'",
        "table_grants": f"SELECT * FROM system.information_schema.table_privileges WHERE table_catalog = '{_sql_lit(catalog)}'",
        "governed_tags": f"SELECT * FROM system.information_schema.column_tags WHERE catalog_name = '{_sql_lit(catalog)}'",
        "mask_functions": f"SELECT routine_catalog,routine_schema,routine_name,routine_type FROM system.information_schema.routines WHERE routine_catalog = '{_sql_lit(catalog)}' AND routine_name LIKE 'gr\\_%' ESCAPE '\\'",
        "tables": f"SELECT table_catalog,table_schema,table_name FROM system.information_schema.tables WHERE table_catalog = '{_sql_lit(catalog)}'",
    }
    out: dict[str, Any] = {}
    for name, statement in queries.items():
        response = client.statement_execution.execute_statement(warehouse_id=warehouse, statement=statement, wait_timeout="50s")
        columns = [c.name for c in (response.manifest.schema.columns or [])]
        rows = response.result.data_array or []
        out[name] = [dict(zip(columns, row)) for row in rows]
    # Effective-policy evidence is per table. Inability to read it is a failed
    # snapshot, never evidence that no policy exists.
    policies: list[Mapping[str, Any]] = []
    for table in out.pop("tables"):
        qualified = ".".join(f"`{str(table[k]).replace('`', '``')}`" for k in
                             ("table_catalog", "table_schema", "table_name"))
        response = client.statement_execution.execute_statement(warehouse_id=warehouse,
            statement=f"SHOW EFFECTIVE POLICIES ON TABLE {qualified}", wait_timeout="50s")
        policies.append({"table": qualified, "response": response.as_dict()})
    out["abac_policies"] = policies
    return out


def _sql_lit(value: str) -> str:
    return value.replace("'", "''")


def _read_catalog(path: Path) -> str | None:
    if not path.exists(): return None
    match = re.search(r'^\s*(?:catalog_name|catalog)\s*=\s*"([^"]+)"', path.read_text(), re.M)
    return match.group(1) if match else None


def _agent_ids(env_dir: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(env_dir.glob("agents/*")):
        if not path.is_file(): continue
        text = path.read_text(encoding="utf-8")
        match = re.search(r'^\s*(?:genie_space_id|space_id)\s*=\s*"([^"]+)"', text, re.M)
        if match: found.append((path.stem, match.group(1)))
    legacy = env_dir / ".genie_space_id"
    if legacy.is_file() and legacy.read_text().strip(): found.append(("legacy", legacy.read_text().strip()))
    return found


def _state_serials(root: Path) -> Mapping[str, int]:
    result: dict[str, int] = {}
    for path in sorted(root.rglob("*.tfstate")) if root.exists() else []:
        try: result[str(path.relative_to(root))] = int(json.loads(path.read_text()).get("serial", -1))
        except (OSError, ValueError, TypeError, json.JSONDecodeError): result[str(path.relative_to(root))] = -1
    return result


def _receipt_inventory(root: Path) -> list[Mapping[str, Any]]:
    result = []
    if not root.exists(): return result
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ("receipt" in p.name or "manifest" in p.name)):
        raw = path.read_bytes()
        result.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    return result


def make_targets(cloud_root: Path) -> set[str]:
    result = subprocess.run(["make", "-qp"], cwd=cloud_root, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False)
    return {m.group(1) for line in result.stdout.splitlines()
            if (m := re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s|$)", line)) and "%" not in m.group(1)}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(value: str, values: Mapping[str, str]) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: values.get(m.group(1), f"<{m.group(1)}>") , value)


def scenario_plan(scenario: Scenario, targets: set[str]) -> list[dict[str, Any]]:
    planned = []
    for step in scenario.steps:
        missing = bool(step.target and step.target not in targets)
        marker = f"requires step {step.requires_step}" if step.requires_step else "target not implemented"
        planned.append({"name": step.name, "command": " ".join(step.command), "human_prompt": step.human_prompt,
                        "expect_failure": step.expect_failure, "status": "skip" if missing else "planned",
                        "skip_reason": marker if missing else None, "assertions": list(step.assertions)})
    return planned


def write_report(out: Path, scenario: Scenario, payload: Mapping[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    clean = redact(payload)
    (out / f"{scenario.key}.json").write_text(json.dumps(clean, indent=2, sort_keys=True, default=str) + "\n")
    lines = [f"# {scenario.key}: {scenario.title}", "", f"Status: **{clean['status']}**", "", "## Preconditions", ""]
    lines += [f"- {x}" for x in scenario.preconditions]
    lines += ["", "## Steps", ""]
    for item in clean["steps"]:
        detail = item.get("skip_reason") or (f"exit {item['exit_code']}" if "exit_code" in item else item["status"])
        lines.append(f"- `{item['status']}` {item['name']}: {item.get('command') or 'human gate'} ({detail})")
    lines += ["", "## Pass criteria", ""] + [f"- {x}" for x in scenario.pass_criteria]
    lines += ["", "## Restore", ""] + [f"- {x}" for x in scenario.restore]
    diff = clean.get("snapshot_diff", {})
    lines += ["", "## Snapshot comparison", "", f"Changed top-level components: {', '.join(diff) if diff else 'none'}", ""]
    (out / f"{scenario.key}.md").write_text("\n".join(lines), encoding="utf-8")


def checkout(runner: CommandRunner, commit: str) -> None:
    result = runner.run(("git", "checkout", "--force", "--detach", commit), "checkout")
    if result.returncode: raise RuntimeError("detached checkout failed; see checkout.log")
    status = runner.run(("git", "status", "--porcelain"), "checkout-status")
    if status.returncode or status.stdout.strip(): raise RuntimeError("persistent checkout is not clean")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", choices=("aws", "azure"), required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--checkout", type=Path, help="persistent v1 deployment checkout (defaults to --repo)")
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--scenarios", default="all", help="comma-separated keys; use --list-scenarios to inspect")
    parser.add_argument("--reports", type=Path, default=Path("/tmp/genierails-e2e-reports"))
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--non-interactive", action="store_true", help="skip scenarios containing human gates")
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--confirm-live", action="store_true", help="required for non-dry-run execution")
    parser.add_argument("--value", action="append", default=[], metavar="KEY=VALUE", help="safe scenario placeholder")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, snapshot_factory: Callable[[Path, Path], SnapshotProvider] = LiveSnapshotProvider) -> int:
    args = parse_args(argv)
    if args.list_scenarios:
        for scenario in SCENARIOS: print(f"{scenario.key:10} {scenario.title}")
        return 0
    wanted = {x.strip() for x in args.scenarios.split(",") if x.strip()}
    selected = list(SCENARIOS) if wanted == {"all"} else [s for s in SCENARIOS if s.key in wanted]
    unknown = wanted - {s.key for s in SCENARIOS} - {"all"}
    if unknown: raise SystemExit(f"unknown scenarios: {', '.join(sorted(unknown))}")
    repo = args.repo.resolve(); checkout_dir = (args.checkout or repo).resolve(); cloud_root = checkout_dir / args.cloud
    reports = args.reports.resolve()
    # Dry-run is hermetic: target inventory is source parsing, not make execution.
    target_text = (repo / "shared" / "Makefile.shared").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-zA-Z0-9][\w.-]*):(?:\s|$)", target_text, re.M)) if args.dry_run else make_targets(cloud_root)
    if not args.dry_run and not args.confirm_live: raise SystemExit("live execution requires --confirm-live")
    values = dict(item.split("=", 1) for item in args.value)
    if args.dry_run:
        for scenario in selected:
            payload = {"scenario": scenario.key, "status": "dry-run", "cloud": args.cloud,
                       "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "steps": scenario_plan(scenario, targets), "snapshot_diff": {}}
            write_report(reports, scenario, payload)
            print(f"[{scenario.key}] {scenario.title}")
            for item in payload["steps"]:
                suffix = f" — SKIP ({item['skip_reason']})" if item["status"] == "skip" else ""
                print(f"  {item['command'] or '[human pause]'}{suffix}")
        return 0
    if not args.state_dir: raise SystemExit("live execution requires --state-dir outside the checkout")
    state_dir = args.state_dir.resolve()
    if state_dir == checkout_dir or checkout_dir in state_dir.parents: raise SystemExit("--state-dir must be outside the checkout")
    log_root = reports / "logs"; log_root.mkdir(parents=True, exist_ok=True)
    runner = CommandRunner(cloud_root, log_root, {"GENIERAILS_STATE_DIR": str(state_dir)})
    checkout(runner, args.commit)
    provider = snapshot_factory(cloud_root, state_dir / args.cloud)
    failed = False
    for scenario in selected:
        plan = scenario_plan(scenario, targets)
        if args.non_interactive and any(s.human_prompt for s in scenario.steps):
            for item in plan: item["status"] = "skip"; item["skip_reason"] = "requires human UI review"
            write_report(reports, scenario, {"scenario": scenario.key, "status": "skipped", "steps": plan, "snapshot_diff": {}})
            continue
        before = {env: provider.capture(env) for env in ("dev", "prod")}
        scenario_failed = False
        refusal_diffs: list[dict[str, Any]] = []
        manifest = values.get("manifest", "")
        for index, (step, item) in enumerate(zip(scenario.steps, plan), 1):
            if item["status"] == "skip": continue
            if step.human_prompt:
                input(f"[{scenario.key}] {step.human_prompt} ")
                item["status"] = "passed"; continue
            argv2 = [render(part, {**values, "manifest": manifest}) for part in step.command]
            refusal_before = provider.capture(step.env) if step.expect_failure and step.env in ("dev", "prod") else None
            result = runner.run(argv2, f"{scenario.key}-{index}-{step.name}")
            item["exit_code"] = result.returncode
            ok = (result.returncode != 0) if step.expect_failure else (result.returncode == 0)
            item["status"] = "passed" if ok else "failed"
            if refusal_before is not None:
                refusal_after = provider.capture(step.env)
                mutation = snapshot_diff(refusal_before, refusal_after)
                refusal_diffs.append({"step": step.name, "env": step.env, "diff": mutation})
                if mutation:
                    item["status"] = "failed"
                    item["assertion"] = "expected refusal mutated the snapshot"
                    ok = False
            if step.target == "release-plan" and ok and not step.expect_failure:
                matches = re.findall(r"(?m)(?:manifest(?: path)?\s*[:=]\s*|MANIFEST=)(\S+\.json)", result.stdout)
                if matches: manifest = matches[-1].strip("'\"")
            if not ok: scenario_failed = failed = True; break
        after = {env: provider.capture(env) for env in ("dev", "prod")}
        diff = snapshot_diff(before, after)
        incomplete = any(item["status"] == "skip" for item in plan)
        payload = {"scenario": scenario.key,
                   "status": "failed" if scenario_failed else ("incomplete" if incomplete else "passed"),
                   "cloud": args.cloud, "commit": args.commit, "steps": plan,
                   "before": before, "after": after, "snapshot_diff": diff,
                   "refusal_snapshot_diffs": refusal_diffs}
        write_report(reports, scenario, payload)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
