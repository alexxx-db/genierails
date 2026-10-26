"""State-changing targets refuse on Databricks compute; read-only ones still run.

A notebook, Job, serverless run or App keeps no local disk past the run, so
the receipts, manifests, staged journal, env locks and Genie ID records a
state-changing target writes would be lost after its live change
(scripts/databricks_compute.py). Each refusal is checked on a real throwaway
repo (the real aws/Makefile and shared/): nothing under it changes, and
neither terraform, the databricks CLI nor the Databricks SDK is reached.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from shared.tests.test_check_target import DEV_ENV, _build

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "shared" / "scripts" / "terraform_layer.sh"
MARKERS = {"DATABRICKS_RUNTIME_VERSION": "15.4", "DB_CLUSTER_ID": "0101-123456-abcdefgh",
           "DB_IS_JOB_CLUSTER": "TRUE", "DATABRICKS_APP_NAME": "genierails"}
REFUSED = "refused: this is Databricks compute"
OPT_IN = "GENIERAILS_ALLOW_DATABRICKS_COMPUTE"
GUARDS = ("_guard-not-ci-apply", "_guard-not-databricks-compute")


# --- which targets change state: found in the Makefile, not listed by hand ----
#
# A target changes state when its recipe (with the Makefile's _-macros expanded)
# runs a terraform write through the layer runner, imports, takes an env lock,
# or writes a receipt, manifest, phase record, staged journal or governance
# record, or when it reaches such a target through a prerequisite or a
# $(MAKE) call. A target whose guard is conditional (promote, promote-to,
# _prepare-classification) guards its own writing mode, so reaching it doesn't
# make the caller state-changing (plan re-splits and preps classification read-only).
SINKS = {
    "terraform write": r'ROOT_RUNNER\)"?(?:\s+"?[^\s";|&]+"?){2}\s+(?:apply|apply-plan|destroy|import|state-rm|state-mv)\b',
    "import": r"IMPORT_EXISTING_SCRIPT|import_existing\.(?:sh|py)",
    "staged journal": r'staged_policy_apply\.py"\)?\s+(?:run|rebind|apply-saved|abandon|confirm)\b',
    "env lock": r'environment_lock\.py"\)?\s+lock\b|state_layout\.py"\s+migrate\b',
    "receipt or manifest": r'release_gate\.py"\)?\s+(?:phase-record|rehearse-start|rehearse-finish|release-receipt|'
                           r'manifest-write|account-receipt|account-manifest-write|plan-dir|account-plan-dir)\b',
    "Genie ID record or receipt": r'agent_ownership\.py"\)?\s+(?:detach|receipt|restore-ids|sync)\b',
    "live tag or identity write": r"sync_tag_policies\.py|bootstrap_sp\.py|bootstrap_pipelines\.py",
    "promotion transaction": r"_PROMOTION_TXN_BEGIN\b",
}
MAKE_CALL = r'\$\(MAKE\)(?:\s+(?:--?[\w-]+|[A-Za-z_]+="[^"]*"|[A-Za-z_]+=\S*))*\s+([a-z_][\w.-]*)'


def _logical_lines(text):
    out, buf = [], ""
    for line in text.splitlines():
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    return out


def _parse_makefile(path=ROOT / "shared" / "Makefile.shared"):
    macros, rules, current = {}, {}, None
    for line in _logical_lines(path.read_text()):
        if line.startswith("\t"):
            if current:
                for name in current:
                    rules[name]["recipe"].append(line[1:])
            continue
        current = None
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(?:override\s+)?(_[A-Z][A-Z0-9_]*)\s*(?::=|\?=|=)(.*)$", line)
        if m:
            macros[m.group(1)] = m.group(2).strip()
            continue
        if re.match(r"^(?:override\s+)?[A-Za-z_][A-Za-z0-9_]*\s*(?::=|\?=|\+=|=)", line):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*(?:\s+[A-Za-z_][A-Za-z0-9_.-]*)*)\s*:(?!=)(.*)$", line)
        if not m or re.match(r"\s*(?:override\s|[A-Za-z_]+\s*:?=)", m.group(2)):
            continue  # not a rule, or a target-specific variable
        current = m.group(1).split()
        for name in current:
            rule = rules.setdefault(name, {"prereqs": "", "recipe": []})
            rule["prereqs"] = (rule["prereqs"] + " " + m.group(2).split("##")[0]).strip()
    return macros, rules


def _expand(text, macros, depth=0):
    """Expand $(_MACRO) and $(call _MACRO,...) references (only the Makefile's own _-macros)."""
    if depth > 15:
        return text
    return re.sub(r"\$\((?:call\s+)?(_[A-Z][A-Z0-9_]*)(?=[),])",
                  lambda m: f"({m.group(1)} " + _expand(macros[m.group(1)], macros, depth + 1) if m.group(1) in macros
                  else m.group(0), text)


def _balanced(text):
    """The leading $(...) expression of ``text``."""
    depth = 0
    for i, ch in enumerate(text):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i:
            return text[:i + 1]
    return text


def _guard_kind(rule, macros):
    """'always', 'conditional' or None: how the target takes the guard at its very start."""
    prereqs = re.sub(r"^\(_[A-Z0-9_]+ ", "", _expand(rule["prereqs"], macros).lstrip())
    if prereqs.split() and prereqs.split()[0] in GUARDS:
        return "always"
    if prereqs.startswith("$(if") and any(g in _balanced(prereqs) for g in GUARDS):
        return "conditional"
    if rule["recipe"] and "$(_DATABRICKS_COMPUTE_GUARD)" in rule["recipe"][0]:
        return "always"
    return None


def state_changing_targets(path=ROOT / "shared" / "Makefile.shared"):
    """{target: reason} for every target that (transitively) changes state."""
    macros, rules = _parse_makefile(path)
    direct, edges, kinds = {}, {}, {}
    for name, rule in rules.items():
        body = _expand("\n".join(rule["recipe"]), macros)
        direct[name] = next((label for label, pattern in SINKS.items() if re.search(pattern, body)), None)
        prereqs = re.sub(r"\$\(if [^)]*\)", " ", _expand(rule["prereqs"], macros)).split()
        edges[name] = {t for t in (*prereqs, *re.findall(MAKE_CALL, body)) if t in rules and t != name}
        kinds[name] = _guard_kind(rule, macros)
    found = {name: reason for name, reason in direct.items() if reason}
    changed = True
    while changed:
        changed = False
        for name, callees in edges.items():
            if name in found:
                continue
            via = sorted(c for c in callees if c in found and kinds[c] != "conditional")
            if via:
                found[name] = f"reaches {via[0]}"
                changed = True
    return {name: (reason, kinds[name]) for name, reason in found.items() if name not in GUARDS}


ENUMERATED = state_changing_targets()


def guarded_targets(path=ROOT / "shared" / "Makefile.shared"):
    macros, rules = _parse_makefile(path)
    return {name for name, rule in rules.items() if name not in GUARDS and _guard_kind(rule, macros)}


# Every enumerated target, plus every other target that takes the guard (the
# scenario test targets), must refuse at run time.
STATE_CHANGING = tuple(sorted({*ENUMERATED, *guarded_targets()}))
# Arguments that select a conditionally guarded target's writing mode.
WRITING_MODE = {"_prepare-classification": ("IMPORT_CLASSIFICATION=1",)}
CI_VARS = ("CI", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "GITHUB_ACTIONS",
           "GENIERAILS_ALLOW_CI_APPLY")
# Logs any call; a refusal must leave the log empty.
FAKE_CLI = '#!/bin/sh\necho "$(basename "$0") $*" >> "$CALL_LOG"\nexit 0\n'


def _env(home, **extra):
    drop = ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "ENV", "GENIERAILS_STATE_DIR", "GENIERAILS_DURABLE_STATE_DIR",
            OPT_IN, *CI_VARS, *MARKERS)
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("DATABRICKS_")}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """A committed dev env, plus a fake terraform/databricks CLI and SDK that log any use."""
    tmp = tmp_path_factory.mktemp("dbx")
    repo = _build(tmp, DEV_ENV)
    log = tmp / "calls.log"
    (tmp / "guard-bin").mkdir()
    for tool in ("terraform", "databricks"):
        (tmp / "guard-bin" / tool).write_text(FAKE_CLI)
        (tmp / "guard-bin" / tool).chmod(0o755)
    sdk = tmp / "sdk" / "databricks"
    sdk.mkdir(parents=True)
    (sdk / "__init__.py").write_text(f"open({str(log)!r}, 'a').write('import databricks\\n')\n")
    return {"tmp": tmp, "repo": repo, "log": log}


def _snapshot(root):
    """Every path under ``root`` (not following the shared/ symlink) with its bytes."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = (
                os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
    return out


def _make(site, *args, guarded=True, **env):
    # Refusal runs get the logging fakes; read-only runs get the fixture's terraform stub.
    bin_dir = site["tmp"] / ("guard-bin" if guarded else "bin")
    extra = dict(PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(site["log"]),
                 FAKE_TF_LOG=str(site["tmp"] / "tf.log"))
    if guarded:
        extra["PYTHONPATH"] = str(site["tmp"] / "sdk")
    return subprocess.run(["make", "--no-print-directory", "-C", str(site["repo"] / "aws"), *args],
                          text=True, capture_output=True, env=_env(site["tmp"], **extra, **env), timeout=120)


def _shared_status():
    return subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "shared", "aws", "azure"],
                          text=True, capture_output=True, check=True).stdout


def _assert_refused_unchanged(site, *args, **env):
    site["log"].unlink(missing_ok=True)
    before, status = _snapshot(site["tmp"]), _shared_status()
    proc = _make(site, *args, **env)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert REFUSED in output, output
    assert "deployment machine" in output
    assert not site["log"].exists(), site["log"].read_text()
    assert _snapshot(site["tmp"]) == before
    assert _shared_status() == status
    return output


# --- the Makefile: every state-changing target takes the guard first ---------

def test_every_state_changing_target_takes_the_guard_at_its_start():
    unguarded = {name: reason for name, (reason, kind) in ENUMERATED.items() if kind is None}
    assert not unguarded, f"state-changing targets without the Databricks-compute guard first: {unguarded}"


def test_the_enumeration_sees_writers_and_spares_read_only_targets():
    assert {"apply", "rehearse", "release", "release-plan", "maintain", "_apply-layer", "_destroy-layer", "import",
            "sync-tags", "account-plan", "promote", "promote-to", "_apply-account-locked",
            "_apply-account-for-workspace", "_release-plan-roots", "migrate-state-dir"} <= set(ENUMERATED)
    assert not {"plan", "_plan-layer", "check", "validate", "validate-generated", "coverage-gate", "evidence",
                "audit-schema", "policy-status", "verify-access-spec", "help"} & set(ENUMERATED)


def test_the_enumeration_flags_what_review_found_on_1c9b2e6(tmp_path):
    """Codex's REQUEST_CHANGES on 1c9b2e6: two internal targets and both promotions were unguarded."""
    old = subprocess.run(["git", "-C", str(ROOT), "show", "1c9b2e6:shared/Makefile.shared"],
                         text=True, capture_output=True)
    if old.returncode != 0:
        pytest.skip("commit 1c9b2e6 is not in this clone")
    (tmp_path / "Makefile.shared").write_text(old.stdout)
    unguarded = {name for name, (_, kind) in state_changing_targets(tmp_path / "Makefile.shared").items()
                 if kind is None}
    assert {"_apply-account-locked", "_apply-account-for-workspace", "promote", "promote-to"} <= unguarded


# --- every marker refuses every state-changing target, writing nothing --------

@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("target", STATE_CHANGING)
def test_every_state_changing_target_refuses_on_databricks_compute(site, target, marker):
    output = _assert_refused_unchanged(site, target, "ENV=dev", *WRITING_MODE.get(target, ()),
                                       **{marker: MARKERS[marker]})
    assert f"({marker} is set)" in output


@pytest.mark.parametrize("target", STATE_CHANGING)
def test_azure_targets_refuse_too(target):
    status = _shared_status()
    proc = subprocess.run(["make", "--no-print-directory", "-f", str(ROOT / "azure" / "Makefile"), target, "ENV=dev",
                           *WRITING_MODE.get(target, ())],
                          cwd=ROOT, text=True, capture_output=True, timeout=60,
                          env=_env(os.environ.get("HOME", ""), DATABRICKS_RUNTIME_VERSION="client.1.13"))
    assert proc.returncode != 0
    assert REFUSED in proc.stdout + proc.stderr
    assert _shared_status() == status


def test_the_databricks_refusal_wins_over_ci_and_its_opt_out(site):
    # GENIERAILS_ALLOW_CI_APPLY=1 is for throwaway CI runners; it never lifts this.
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", CI="true", GENIERAILS_ALLOW_CI_APPLY="1",
                                       DATABRICKS_RUNTIME_VERSION="15.4")
    assert "cannot run in CI" not in output


def test_dev_rehearse_ci_exception_does_not_apply(site):
    # CI may run make rehearse ENV=dev with remote state; Databricks compute may not.
    _assert_refused_unchanged(site, "rehearse", "ENV=dev", CI="true", DATABRICKS_RUNTIME_VERSION="15.4")


def test_cross_env_promote_refuses(site):
    _assert_refused_unchanged(site, "promote", "SOURCE_ENV=dev", "DEST_ENV=prod", "DEST_CATALOG_MAP=paycat=prodcat",
                              DATABRICKS_RUNTIME_VERSION="15.4")


def test_command_line_marker_counts(site):
    _assert_refused_unchanged(site, "apply", "ENV=dev", "DATABRICKS_RUNTIME_VERSION=15.4")


def test_empty_marker_is_not_databricks(site):
    proc = _make(site, "_guard-not-ci-apply", "ENV=dev", DATABRICKS_RUNTIME_VERSION="")
    assert proc.returncode == 0, proc.stderr


# --- read-only targets still run ----------------------------------------------

@pytest.mark.parametrize("marker", MARKERS)
def test_make_check_still_runs_on_databricks_compute(site, marker):
    before = _snapshot(site["repo"] / "aws" / "envs")
    proc = _make(site, "check", "ENV=dev", guarded=False, **{marker: MARKERS[marker]})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert REFUSED not in proc.stdout + proc.stderr
    assert _snapshot(site["repo"] / "aws" / "envs") == before


@pytest.mark.parametrize("target", ["validate", "validate-generated", "coverage-gate"])
def test_offline_read_only_targets_still_run(site, target):
    proc = _make(site, target, "ENV=dev", guarded=False, DATABRICKS_RUNTIME_VERSION="15.4")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert REFUSED not in proc.stdout + proc.stderr


def test_plan_runs_its_same_env_promote_on_databricks_compute(site):
    # plan re-splits generated/ through promote at MAKELEVEL 1: that mode isn't guarded.
    # (It then stops at the live mask inventory: the fixture has no credentials.)
    proc = _make(site, "plan", "ENV=dev", guarded=False, DATABRICKS_RUNTIME_VERSION="15.4")
    output = proc.stdout + proc.stderr
    assert REFUSED not in output
    assert "env config validation: PASS" in output and "external-mask inventory failed" in output, output


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("target", [
    ["plan"], ["validate"], ["check"], ["evidence"], ["audit-schema"], ["coverage-gate"], ["policy-status"],
    ["promote-to", "CHECK=1"], ["verify-access-spec"],
])
def test_read_only_targets_have_no_databricks_refusal(cloud, target):
    proc = subprocess.run(["make", "-n", "-f", str(ROOT / cloud / "Makefile"), *target, "ENV=dev"],
                          cwd=ROOT, text=True, capture_output=True, timeout=30,
                          env=_env(os.environ.get("HOME", ""), DATABRICKS_RUNTIME_VERSION="15.4"))
    assert REFUSED not in proc.stdout + proc.stderr
    assert "databricks_compute.py" not in proc.stdout


# --- no markers: nothing changes on laptops, servers and CI --------------------

def test_no_marker_never_runs_the_check(site, tmp_path):
    # A python3 shim logs every script it runs: without a marker the helper isn't one of them.
    shim, log = tmp_path / "shim", tmp_path / "python.log"
    shim.mkdir()
    (shim / "python3").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{sys.executable}" "$@"\n')
    (shim / "python3").chmod(0o755)
    env = _env(site["tmp"], PATH=f"{shim}:{os.environ['PATH']}")
    for marker in ({}, {"DATABRICKS_RUNTIME_VERSION": "15.4"}):
        proc = subprocess.run(["make", "--no-print-directory", "-C", str(site["repo"] / "aws"),
                               "_guard-not-ci-apply", "ENV=dev"], text=True, capture_output=True,
                              env={**env, **marker}, timeout=60)
        assert ("databricks_compute.py" in log.read_text()) is bool(marker)
        assert proc.returncode == (2 if marker else 0), proc.stderr


def test_ci_refusal_is_unchanged_without_a_marker(site):
    proc = _make(site, "apply", "ENV=dev", CI="true")
    assert proc.returncode != 0
    assert "applying targets cannot run in CI" in proc.stderr
    assert REFUSED not in proc.stderr


# --- opt-in: both parts, the flag from the make command line only --------------

@pytest.fixture
def durable(tmp_path):
    state = tmp_path / "durable" / "genierails-state"
    state.mkdir(parents=True)
    return {"GENIERAILS_DURABLE_STATE_DIR": str(tmp_path / "durable"), "GENIERAILS_STATE_DIR": str(state)}


def test_opt_in_needs_both_parts(site, durable):
    flag = f"{OPT_IN}=1"
    proc = _make(site, "_guard-not-ci-apply", "ENV=dev", flag, DB_IS_JOB_CLUSTER="TRUE", **durable)
    assert proc.returncode == 0, proc.stderr
    # Opted in, the CI guard still applies as before.
    proc = _make(site, "_guard-not-ci-apply", "ENV=dev", flag, DB_IS_JOB_CLUSTER="TRUE", CI="true", **durable)
    assert proc.returncode != 0 and "applying targets cannot run in CI" in proc.stderr

    output = _assert_refused_unchanged(site, "apply", "ENV=dev", flag, DB_IS_JOB_CLUSTER="TRUE")
    assert "GENIERAILS_DURABLE_STATE_DIR" in output and "not supported end to end" in output
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", DB_IS_JOB_CLUSTER="TRUE", **durable)
    assert "on the make command line" in output


@pytest.mark.parametrize("how", ["shell variable", "make -e", "value 0"])
def test_opt_in_flag_only_counts_from_the_command_line(site, durable, how):
    if how == "shell variable":
        output = _assert_refused_unchanged(site, "apply", "ENV=dev", DB_CLUSTER_ID="x", **{OPT_IN: "1"},
                                           **durable)
    elif how == "make -e":
        output = _assert_refused_unchanged(site, "-e", "apply", "ENV=dev", DB_CLUSTER_ID="x", **{OPT_IN: "1"},
                                           **durable)
    else:
        output = _assert_refused_unchanged(site, "apply", "ENV=dev", f"{OPT_IN}=0", DB_CLUSTER_ID="x",
                                           **durable)
    assert "on the make command line" in output


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<make>/environ (Databricks is Linux)")
def test_opt_in_flag_from_an_exported_makeflags_is_refused(site, durable):
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", DB_CLUSTER_ID="x", MAKEFLAGS=f"{OPT_IN}=1",
                                       **durable)
    assert "exported MAKEFLAGS" in output


@pytest.mark.parametrize("problem", ["missing", "relative", "state outside", "no state dir"])
def test_opt_in_durable_dir_must_hold_the_state_dir(site, durable, tmp_path, problem):
    env = dict(durable)
    if problem == "missing":
        env["GENIERAILS_DURABLE_STATE_DIR"] = str(tmp_path / "nope")
    elif problem == "relative":
        env["GENIERAILS_DURABLE_STATE_DIR"] = "durable"
    elif problem == "state outside":
        (tmp_path / "elsewhere").mkdir()
        env["GENIERAILS_STATE_DIR"] = str(tmp_path / "elsewhere")
    else:
        del env["GENIERAILS_STATE_DIR"]
    output = _assert_refused_unchanged(site, "apply", "ENV=dev", f"{OPT_IN}=1", DATABRICKS_APP_NAME="a", **env)
    assert "opt-in is incomplete" in output


def test_opt_in_hint_only_when_attempted():
    from shared.scripts import databricks_compute as dc

    message = dc.guard({"DATABRICKS_RUNTIME_VERSION": "15.4"}, "apply")
    assert message.startswith("ERROR: make apply refused") and "opt-in" not in message
    assert dc.guard({}, "apply", "1") is None
    assert dc.marker({"DB_CLUSTER_ID": ""}) is None


# --- the runner's second line: before any state or env dir, both opt-in parts --

@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.parametrize("command", ["apply", "destroy", "import", "state-rm"])
def test_runner_refuses_writes_on_databricks_compute(tmp_path, marker, command):
    proc, log = _run_layer(tmp_path, command, **{marker: MARKERS[marker], "GENIERAILS_ALLOW_CI_APPLY": "1"})
    assert proc.returncode == 2
    assert f"terraform {command} (data_access:dev) refused: this is Databricks compute" in proc.stderr
    assert not log.exists(), log.read_text()
    assert not (tmp_path / "data_access" / "terraform.tfstate").exists()


@pytest.mark.parametrize("parts", ["durable dir only", "flag only", "state dir outside"])
def test_runner_needs_both_opt_in_parts_and_creates_nothing_first(tmp_path, parts):
    durable = tmp_path / "durable"
    state = durable / "state"
    state.mkdir(parents=True)
    env = {"DATABRICKS_RUNTIME_VERSION": "15.4", "GENIERAILS_DURABLE_STATE_DIR": str(durable),
           "GENIERAILS_STATE_DIR": str(state), OPT_IN: "1"}
    if parts == "durable dir only":
        del env[OPT_IN]
    elif parts == "flag only":
        del env["GENIERAILS_DURABLE_STATE_DIR"]
    else:
        (tmp_path / "elsewhere").mkdir()
        env["GENIERAILS_STATE_DIR"] = str(tmp_path / "elsewhere")
    proc, log = _run_layer(tmp_path, "apply", **env)
    assert proc.returncode == 2 and "refused: this is Databricks compute" in proc.stderr
    assert not log.exists()
    # Refused before the layer dir, its .terraform or permissions were touched.
    assert list(state.iterdir()) == [] and not (tmp_path / "elsewhere" / "aws").exists()
    assert list((tmp_path / "data_access").iterdir()) == []


def test_runner_applies_with_both_opt_in_parts(tmp_path):
    durable = tmp_path / "durable"
    (durable / "state").mkdir(parents=True)
    proc, log = _run_layer(tmp_path, "apply", DATABRICKS_RUNTIME_VERSION="15.4",
                           GENIERAILS_DURABLE_STATE_DIR=str(durable), GENIERAILS_STATE_DIR=str(durable / "state"),
                           **{OPT_IN: "1"})
    assert "refused" not in proc.stderr, proc.stderr
    assert "terraform apply" in log.read_text()


def test_runner_plan_is_not_refused(tmp_path):
    proc, _ = _run_layer(tmp_path, "plan", DATABRICKS_RUNTIME_VERSION="15.4")
    assert "Databricks compute" not in proc.stderr


# --- the /Workspace mount marker (serverless without a variable) --------------

def test_workspace_mount_marker(tmp_path, monkeypatch):
    from shared.scripts import databricks_compute as dc

    (tmp_path / "Workspace").mkdir()
    assert dc.marker({}, tmp_path) is None  # a plain directory isn't the FUSE mount
    monkeypatch.setattr(dc.os.path, "ismount", lambda path: str(path) == str(tmp_path / "Workspace"))
    assert dc.marker({}, tmp_path) == f"the {tmp_path / 'Workspace'} mount"
    message = dc.guard({}, "apply", root=tmp_path)
    assert f"this is Databricks compute (the {tmp_path / 'Workspace'} mount)" in message
    other = tmp_path / "laptop"
    (other / "workspace").mkdir(parents=True)  # devcontainer-style lowercase mount: never a marker
    monkeypatch.setattr(dc.os.path, "ismount", lambda path: True)
    assert dc.marker({}, other) is None


def test_this_machine_has_no_marker():
    from shared.scripts import databricks_compute as dc

    assert dc.marker({k: v for k, v in os.environ.items() if k not in MARKERS}) is None


def _run_layer(tmp_path, command, **env):
    env_dir = tmp_path / "data_access"
    env_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "terraform").write_text(FAKE_CLI)
    (bin_dir / "terraform").chmod(0o755)
    log = tmp_path / "calls.log"
    args = ["-auto-approve"] if command in ("apply", "destroy") else ["x.y", "id"] if command == "import" else (
        ["x.y"] if command == "state-rm" else [])
    proc = subprocess.run([str(RUNNER), "data_access", "dev", command, *args], text=True, capture_output=True,
                          timeout=60, env=_env(tmp_path, PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(log),
                                               LAYER_ENV_DIR=str(env_dir), **env))
    return proc, log
