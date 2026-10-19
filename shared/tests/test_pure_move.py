"""The generate_abac.py split moves code without changing it (18.7).

scripts/check_pure_move.py compares every definition that left
generate_abac.py with the baseline taken before the split, and requires
generate_abac.py to re-export it; these tests run it and prove it bites.
"""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import generate_abac
from scripts import check_pure_move

SHARED = Path(__file__).parents[1]
BASELINE = json.loads(check_pure_move.BASELINE.read_text())


def _moved():
    moved, problems = check_pure_move.check(SHARED, BASELINE)
    assert problems == []
    return moved


def test_every_moved_definition_is_byte_identical_and_re_exported(capsys):
    assert check_pure_move.main([]) == 0
    assert "OK:" in capsys.readouterr().out


def test_the_baseline_is_the_base_commit():
    base = BASELINE["base_commit"]
    have = subprocess.run(["git", "-C", str(SHARED), "cat-file", "-e", f"{base}^{{commit}}"],
                          capture_output=True)
    if have.returncode != 0:
        pytest.skip(f"base commit {base[:12]} is not in this clone")
    assert check_pure_move.main(["--ref", base]) == 0


def test_moved_names_are_the_same_objects_through_generate_abac():
    moved = _moved()
    assert moved, "nothing has moved out of generate_abac.py"
    for name in moved:
        module = __import__(check_pure_move.reexports(
            (SHARED / "generate_abac.py").read_text())[name])
        assert getattr(generate_abac, name) is getattr(module, name), name


def _generate_abac_imports():
    """(file, name) for every name imported from generate_abac outside the tests."""
    found = []
    for path in sorted([*SHARED.glob("*.py"), *(SHARED / "scripts").rglob("*.py")]):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "generate_abac":
                found += [(path.name, alias.name) for alias in node.names]
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "ga" and path.name == "discover_agent_tables.py":
                found.append((path.name, node.attr))
    makefile = (SHARED / "Makefile.shared").read_text()
    for match in re.finditer(r"from generate_abac import ([A-Za-z_][\w, ]*)", makefile):
        found += [("Makefile.shared", n.strip()) for n in match.group(1).split(",")]
    return found


def test_every_import_from_generate_abac_still_resolves():
    imports = _generate_abac_imports()
    assert len(imports) > 20
    missing = [(f, n) for f, n in imports if not hasattr(generate_abac, n)]
    assert missing == []


def _copy(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    for name in ("generate_abac.py", *(f"{m}.py" for m in check_pure_move.SPLIT_MODULES)):
        if (SHARED / name).exists():
            shutil.copy(SHARED / name, root / name)
    return root


def _check(root):
    return check_pure_move.check(root, BASELINE)[1]


def _flip_one_byte(path: Path, name: str):
    text = path.read_text()
    start = text.index(f"\ndef {name}(")
    # The first letter after the signature's first quote: still valid Python.
    quote = text.index('"', text.index(")", start))
    at = re.compile("[A-Za-z]").search(text, quote).start()
    path.write_text(text[:at] + text[at].swapcase() + text[at + 1:])


def test_checker_catches_one_changed_byte_in_a_moved_function(tmp_path):
    root = _copy(tmp_path)
    name = next(n for n in _moved() if callable(getattr(generate_abac, n)))
    module = check_pure_move.reexports((root / "generate_abac.py").read_text())[name]
    _flip_one_byte(root / f"{module}.py", name)
    assert _check(root) == [f"{name}: {module}.py body differs from {BASELINE['base_commit']}"]


def test_checker_catches_a_missing_re_export_and_new_code(tmp_path):
    root = _copy(tmp_path)
    name = _moved()[0]
    module = check_pure_move.reexports((root / "generate_abac.py").read_text())[name]
    source = (root / "generate_abac.py").read_text()
    (root / "generate_abac.py").write_text(re.sub(rf"\n    {name},", "", source, count=1))
    with (root / f"{module}.py").open("a") as handle:
        handle.write("\n\ndef brand_new_helper():\n    return 1\n")
    problems = _check(root)
    assert f"{name}: generate_abac.py does not re-export it from {module}" in problems
    assert f"brand_new_helper: new definition in {module}.py (not a move)" in problems


def test_checker_catches_a_definition_left_in_both_places(tmp_path):
    root = _copy(tmp_path)
    name = next(n for n in _moved() if callable(getattr(generate_abac, n)))
    module = check_pure_move.reexports((root / "generate_abac.py").read_text())[name]
    segment = check_pure_move.definitions((root / f"{module}.py").read_text())[name][0]
    with (root / "generate_abac.py").open("a") as handle:
        handle.write("\n\n" + segment)
    assert f"{name}: defined in generate_abac.py and in {module}" in _check(root)
