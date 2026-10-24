"""Scripts make, shell and CI run by path resolve their ``scripts.`` imports.

Run as ``python3 shared/scripts/x.py``, only shared/scripts is on sys.path, so
``from scripts.auth_configured import ...`` needs the script to put shared/ there
itself. The unit tests set PYTHONPATH, which hides a missing setup; these run
each script with PYTHONPATH unset and cwd outside the repo.
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SHARED = Path(__file__).resolve().parents[1]

# sys.path[0] is the script's own directory when Python runs it by path; then
# its module code runs and every scripts.* module it names (lazy ones included)
# must import.
_PROBE = """
import importlib, runpy, sys
script, modules = sys.argv[1], sys.argv[2:]
sys.path[0] = str(__import__("pathlib").Path(script).parent)
runpy.run_path(script, run_name="__by_path_probe__")
for name in modules:
    importlib.import_module(name)
"""


def _scripts_imports(path: Path) -> list[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "scripts":
                found.update(f"scripts.{alias.name}" for alias in node.names)
            elif node.module.startswith("scripts."):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("scripts."))
    return sorted(found)


IMPORTERS = sorted(
    path for path in SHARED.rglob("*.py")
    if "tests" not in path.relative_to(SHARED).parts and _scripts_imports(path)
)


def _env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k != "PYTHONPATH" and not k.startswith("GENIERAILS_")}


def _run(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=_env(), capture_output=True, text=True, timeout=120)


def test_importers_found():
    names = {str(path.relative_to(SHARED)) for path in IMPORTERS}
    assert {"scripts/classification_completeness.py", "scripts/genie_adopt_preflight.py",
            "scripts/reader_inventory.py"} <= names


@pytest.mark.parametrize("script", IMPORTERS, ids=lambda p: str(p.relative_to(SHARED)))
def test_scripts_imports_resolve_when_run_by_path(script, tmp_path):
    result = _run([sys.executable, "-c", _PROBE, str(script), *_scripts_imports(script)], tmp_path)
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr


def test_classification_check_by_path_reaches_auth(tmp_path):
    """make release-plan's check (Makefile.shared _CLASSIFICATION_CHECK), past the lazy import."""
    env_dir = tmp_path / "envs" / "prod"
    (env_dir / "generated").mkdir(parents=True)
    (env_dir / "generated" / "expected_classification.json").write_text(
        json.dumps({"cat.sch.tbl.ssn": ["pii"]}))
    (env_dir / "env.auto.tfvars").write_text('sql_warehouse_id = "warehouse"\n')
    (env_dir / "auth.auto.tfvars").write_text("")
    result = _run([sys.executable, str(SHARED / "scripts" / "classification_completeness.py"),
                   "check", "--env-dir", str(env_dir), "--mode", "deterministic"], tmp_path)
    assert "No module named" not in result.stderr, result.stderr
    assert "could not check production classification: fill in" in result.stderr, result.stderr
    assert result.returncode == 1


def test_genie_adopt_preflight_by_path_reaches_auth(tmp_path):
    """make genie-adopt-preflight (GENIE_ADOPT_PREFLIGHT_SCRIPT) with a legacy agent to adopt."""
    env_dir = tmp_path / "envs" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "terraform.tfstate").write_text(json.dumps({"version": 4, "resources": [{
        "mode": "managed", "type": "null_resource", "name": "genie_space_create",
        "instances": [{"index_key": "agent", "attributes": {"triggers": {"host": ""}}}],
    }]}))
    (env_dir / "auth.auto.tfvars").write_text("")
    result = _run([sys.executable, str(SHARED / "scripts" / "genie_adopt_preflight.py"),
                   str(env_dir)], tmp_path)
    assert "No module named" not in result.stderr, result.stderr
    assert "ERROR: fill in" in result.stderr, result.stderr
    assert result.returncode == 1
