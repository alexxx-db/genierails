"""An applying gate's verify-access can't be turned into a free-form run.

Gates (rehearse, release, the staged policy controller) run verify-access
through make _verify-access-gate, whose target-specific override VERIFY_GATE
beats the command line, MAKEFLAGS and make -e. On Apple make 3.81, an outer
make -e VERIFY_GATE= replaces a sub-make's command-line VERIFY_GATE=1, which
used to drop --minimum-from-tfvars (and so a weaker VERIFY_SPEC passed the
gate). Each test runs a real nested make, outer -> the real Makefile, under
every make variant installed, and records how verify_effective_access.py was
called.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import script_stubs

SHARED = Path(__file__).resolve().parents[1]
ROOT = SHARED.parent
sys.path.insert(0, str(SHARED))

import verify_effective_access as vea  # noqa: E402


def _version(make: str) -> str:
    try:
        return subprocess.run([make, "--version"], text=True, capture_output=True, timeout=30).stdout
    except OSError:
        return ""


def _makes() -> dict[str, str | None]:
    """The GNU make 4.x and Apple make 3.81 binaries installed (None if absent)."""
    found: dict[str, str | None] = {"gnu": None, "apple-3.81": None}
    for candidate in (shutil.which("gmake"), shutil.which("make"), "/usr/bin/make"):
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        version = _version(candidate)
        if "GNU Make 3.81" in version and sys.platform == "darwin":
            found["apple-3.81"] = found["apple-3.81"] or candidate
        elif "GNU Make" in version and "GNU Make 3." not in version:
            found["gnu"] = found["gnu"] or candidate
    return found


MAKES = _makes()


def _make_param(name):
    reason = ("Apple make 3.81 (/usr/bin/make on macOS) is not installed" if name == "apple-3.81"
              else "GNU make 4.x is not installed")
    return pytest.param(name, id=name, marks=pytest.mark.skipif(MAKES[name] is None, reason=reason))


MAKE_VARIANTS = [_make_param("gnu"), _make_param("apple-3.81")]
# How an operator could try to turn the gate off for the inner verify-access.
DISABLES = {
    "-e VERIFY_GATE=": (["-e", "VERIFY_GATE="], {}),
    "-e VERIFY_GATE=0": (["-e", "VERIFY_GATE=0"], {}),
    "command line VERIFY_GATE=": (["VERIFY_GATE="], {}),
    "environment VERIFY_GATE= with -e": (["-e"], {"VERIFY_GATE": ""}),
    "MAKEFLAGS e VERIFY_GATE=": ([], {"MAKEFLAGS": "e VERIFY_GATE="}),
}


def _scenario(tmp_path: Path, make: str, inner_goal: str, outer_args, outer_env):
    env_dir = tmp_path / "envs" / "dev"
    (env_dir / "data_access").mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text('genie_space_id = "x"\n')
    (env_dir / "data_access" / "abac.auto.tfvars").write_text("fgac_policies = []\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"column_masks": [], "row_filters": []}))
    log = tmp_path / "verify-calls"
    recorder = tmp_path / "verify-recorder"
    recorder.write_text(f"#!{sys.executable}\nimport json, sys\n"
                        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    resolver = tmp_path / "resolver"
    resolver.write_text("#!/bin/sh\necho stub\n")
    bootstrap = tmp_path / "make-stub"  # _guarded-bootstrap's sub-makes
    bootstrap.write_text("#!/bin/sh\nexit 0\n")
    for path in (recorder, resolver, bootstrap):
        path.chmod(0o755)
    bin_dir = script_stubs.install(tmp_path / "stub-bin", {
        "verify_effective_access.py": str(recorder), "resolve_env_config.py": str(resolver),
        "genie_space_placeholder.py": "true"})
    # The caller: a recipe that runs the real Makefile the way a gate does.
    outer = tmp_path / "outer.mk"
    outer.write_text(
        "gate:\n"
        f"\t@$(REAL_MAKE) --no-print-directory -C '{ROOT / 'aws'}' {inner_goal} ENV=dev ENV_DIR='{env_dir}' "
        f"VERIFY_SPEC='{spec}' VERIFY_FROM_TFVARS= VERIFY_ENV_FILE= MAKE='{bootstrap}'\n")
    environ = {k: v for k, v in script_stubs.without_ci(os.environ).items()
               if k not in ("MAKEFLAGS", "GNUMAKEFLAGS", "MAKELEVEL", "MFLAGS", "VERIFY_GATE", "VERIFY_SPEC",
                            "VERIFY_REQUIRE_MASKS", "VERIFY_KEY_COLUMN")}
    environ["PATH"] = f"{bin_dir}:{environ['PATH']}"
    environ.update(outer_env)
    result = subprocess.run([make, "--no-print-directory", "-f", str(outer), *outer_args, f"REAL_MAKE={make}", "gate"],
                            cwd=tmp_path, text=True, capture_output=True, env=environ, timeout=120)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


@pytest.mark.parametrize("disable", sorted(DISABLES))
@pytest.mark.parametrize("variant", MAKE_VARIANTS)
def test_the_gate_target_holds_a_spec_to_the_minimum_however_the_gate_is_unset(tmp_path, variant, disable):
    outer_args, outer_env = DISABLES[disable]
    result, calls = _scenario(tmp_path, MAKES[variant], "_verify-access-gate", outer_args, outer_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 1, calls
    assert "--gate" in calls[0] and "--minimum-from-tfvars" in calls[0], calls[0]
    assert "VERIFY_SPEC is free-form" not in result.stdout


@pytest.mark.parametrize("variant", MAKE_VARIANTS)
def test_the_gate_target_gates_without_any_override(tmp_path, variant):
    result, calls = _scenario(tmp_path, MAKES[variant], "_verify-access-gate", [], {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--gate" in calls[0] and "--minimum-from-tfvars" in calls[0], calls[0]


@pytest.mark.skipif(MAKES["apple-3.81"] is None, reason="Apple make 3.81 (/usr/bin/make on macOS) is not installed")
def test_a_command_line_verify_gate_is_what_apple_make_e_overrides(tmp_path):
    # Why gates don't pass VERIFY_GATE=1 to plain verify-access: Apple make
    # lets the outer -e VERIFY_GATE= win, and the run is free-form.
    result, calls = _scenario(tmp_path, MAKES["apple-3.81"], "verify-access VERIFY_GATE=1", ["-e", "VERIFY_GATE="], {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--minimum-from-tfvars" not in calls[0] and "--gate" not in calls[0], calls[0]


def test_every_applying_verify_access_runs_through_the_gate_target():
    makefile = (SHARED / "Makefile.shared").read_text()
    assert "\n_verify-access-gate: override VERIFY_GATE = 1\n_verify-access-gate: verify-access\n" in makefile
    callers = [line for line in makefile.splitlines()
               if re.search(r"\$\(MAKE\)(?: --no-print-directory)? _?verify-access(?:-gate)? ", line)]
    assert len(callers) == 5, callers  # rehearse (both modes), release, release MANIFEST=, staged controller
    assert all("_verify-access-gate ENV=" in line and "VERIFY_GATE=" not in line for line in callers), callers


def test_every_staged_controller_verify_command_is_the_gate_target():
    # run, apply-saved and policy-rebind each hand the controller the one gated command.
    makefile = (SHARED / "Makefile.shared").read_text()
    assert re.search(r"^override _STAGED_VERIFY_ACCESS = \$\(MAKE\) --no-print-directory _verify-access-gate ENV=",
                     makefile, re.M)
    commands = re.findall(r"--verify-access-cmd ('[^']*')", makefile)
    assert len(commands) == 3 and set(commands) == {"'$(_STAGED_VERIFY_ACCESS)'"}, commands
    rebind = makefile[makefile.index("\npolicy-rebind:"):]
    assert "--verify-access-cmd '$(_STAGED_VERIFY_ACCESS)'" in rebind[:rebind.index("\n\n")]


def test_a_gate_run_with_a_spec_and_no_minimum_refuses(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"column_masks": [], "row_filters": []}))
    with pytest.raises(SystemExit) as refused:
        vea.main(["--spec", str(spec), "--gate"])
    assert "needs --minimum-from-tfvars" in str(refused.value.code)
