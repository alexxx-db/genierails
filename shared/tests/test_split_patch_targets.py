"""No test patch goes inert when code moves out of generate_abac.py (18.7).

generate_abac.py re-exports what moved, so imports keep working, but a patch
does not follow the code: `monkeypatch.setattr(generate_abac, "X", fake)`
rebinds generate_abac.X only. A function that moved to a split module still
calls its own module's X, and the test silently stops testing anything.

This test fails when a patch on generate_abac targets a name a split module
defines or calls, or when a patch on a split module targets a name
generate_abac.py calls. Fix it by patching the module that holds the caller.
"""

import ast
import importlib
from pathlib import Path

from scripts.check_pure_move import SPLIT_MODULES

SHARED = Path(__file__).parents[1]
TESTS = Path(__file__).parent
SOURCE_MODULE = "generate_abac"
WATCHED = (SOURCE_MODULE, *SPLIT_MODULES)
PATCH_CALLS = {"setattr", "delattr", "object"}  # monkeypatch.setattr/delattr, mock.patch.object
STRING_PATCHES = {"setattr", "delattr", "patch"}  # monkeypatch.setattr("m.X"), mock.patch("m.X")


def _module_aliases(tree) -> dict[str, str]:
    """Local name -> watched module, from any import or `alias = module` in the file."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in WATCHED:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module" \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value in WATCHED:
            aliases["__import_module__"] = node.args[0].value
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Name) and value.id in aliases:
                aliases[node.targets[0].id] = aliases[value.id]
            elif isinstance(value, ast.Call) and getattr(value.func, "attr", "") == "import_module" \
                    and value.args and isinstance(value.args[0], ast.Constant) \
                    and value.args[0].value in WATCHED:
                aliases[node.targets[0].id] = value.args[0].value
    aliases.pop("__import_module__", None)
    return aliases


UNRESOLVED = "?"


def _loop_values(tree, node, name, namespace):
    """The values a for-loop gives `name` around node, evaluated in namespace."""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == name:
            try:
                values = list(eval(compile(ast.Expression(node.iter), "<loop>", "eval"), dict(namespace)))
            except Exception:  # noqa: BLE001 -- unresolvable means fail closed
                return None
            return values if all(isinstance(v, str) for v in values) else None
    return None


def patch_targets(source: str, filename: str = "<test>", namespace=None) -> list[tuple[str, str, str]]:
    """(module, attribute, location) for every patch of a watched module.

    A patch whose attribute is a loop variable is resolved by evaluating the
    loop's iterable in namespace (the test module's globals); one that cannot
    be resolved is reported with attribute UNRESOLVED.
    """
    tree = ast.parse(source)
    aliases = _module_aliases(tree)
    found = []

    def add(module, attr, node):
        found.append((module, attr, f"{filename}:{node.lineno}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            args = node.args
            if name in PATCH_CALLS and len(args) >= 2 and isinstance(args[0], ast.Name) \
                    and args[0].id in aliases and isinstance(args[1], ast.Constant) \
                    and isinstance(args[1].value, str):
                add(aliases[args[0].id], args[1].value, node)
            elif name in PATCH_CALLS and len(args) >= 2 and isinstance(args[0], ast.Name) \
                    and args[0].id in aliases:
                values = None
                if isinstance(args[1], ast.Name) and namespace is not None:
                    values = _loop_values(tree, node, args[1].id, namespace)
                for value in values or [UNRESOLVED]:
                    add(aliases[args[0].id], value, node)
            elif name in STRING_PATCHES and args and isinstance(args[0], ast.Constant) \
                    and isinstance(args[0].value, str) and "." in args[0].value:
                module, _, attr = args[0].value.partition(".")
                if module in WATCHED and attr.isidentifier():
                    add(module, attr, node)
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                        and target.value.id in aliases:
                    add(aliases[target.value.id], target.attr, node)
    return found


def _names(source: str) -> tuple[set[str], set[str]]:
    """(names defined at top level, names the module reads anywhere)."""
    tree = ast.parse(source)
    defined = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            defined |= {t.id for t in targets if isinstance(t, ast.Name)}
    read = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return defined, read


def inert_patches(targets, sources: dict[str, str]) -> list[str]:
    """Patches whose target's callers live in a different watched module."""
    names = {module: _names(text) for module, text in sources.items()}
    problems = []
    for module, attr, where in targets:
        if attr == UNRESOLVED:
            problems.append(f"{where}: patches {module} with a name this test cannot resolve")
        elif module == SOURCE_MODULE:
            for split, (defined, read) in names.items():
                if split == SOURCE_MODULE:
                    continue
                if attr in defined:
                    problems.append(f"{where}: patches {SOURCE_MODULE}.{attr}, which moved to {split}")
                elif attr in read:
                    problems.append(f"{where}: patches {SOURCE_MODULE}.{attr}, but {split} calls it")
        elif module in names and attr in names[SOURCE_MODULE][1]:
            problems.append(f"{where}: patches {module}.{attr}, but {SOURCE_MODULE} calls it")
    return problems


def _all_test_patches():
    targets = []
    for path in sorted(TESTS.rglob("*.py")):
        source = path.read_text()
        namespace = None
        if "for " in source:  # a loop may pick the patched name; resolve it from the module
            module = ".".join(path.relative_to(SHARED).with_suffix("").parts)
            namespace = vars(importlib.import_module(module))
        targets += patch_targets(source, path.relative_to(SHARED).as_posix(), namespace)
    return targets


def _sources():
    return {module: (SHARED / f"{module}.py").read_text()
            for module in WATCHED if (SHARED / f"{module}.py").exists()}


def test_no_patch_targets_a_name_whose_callers_moved():
    assert inert_patches(_all_test_patches(), _sources()) == []


def test_the_scanner_sees_the_suite_patches():
    """Guard the guard: it finds the patches the audit counted, in every style."""
    counts = {}
    patches = _all_test_patches()
    for module, attr, _where in patches:
        if module == SOURCE_MODULE:
            counts[attr] = counts.get(attr, 0) + 1
    # The deterministic test forbids every model surface in a loop.
    assert {attr for module, attr, where in patches
            if where.startswith("tests/test_deterministic_generate.py")} >= {
        "call_with_retries", "call_anthropic", "configure_databricks_env"}
    assert sum(counts.values()) >= 60
    for attr, at_least in {"configure_databricks_env": 11, "fetch_tables_from_databricks": 9,
                           "call_with_retries": 7, "_FGAC_PER_CATALOG_LIMIT": 6,
                           "_fetch_live_classification_source": 5}.items():
        assert counts.get(attr, 0) >= at_least, (attr, counts.get(attr))


def test_a_patch_on_a_moved_or_moved_caller_name_is_reported():
    test = '''
import generate_abac
import generate_abac as ga
from unittest import mock
import hcl_text

def test_x(monkeypatch):
    monkeypatch.setattr(generate_abac, "helper", lambda: 1)
    monkeypatch.setattr(ga, "dependency", lambda: 2)
    mock.patch("generate_abac.LIMIT")
    generate_abac.kept = 3
    monkeypatch.setattr(hcl_text, "kept", 4)
    monkeypatch.setattr(generate_abac, "kept", 5)
    for name in NAMES:
        monkeypatch.setattr(generate_abac, name, None)
    for name in unknown():
        monkeypatch.setattr(ga, name, None)
'''
    sources = {
        SOURCE_MODULE: "from hcl_text import helper\ndef kept():\n    return helper()\n"
                         "def main():\n    return kept()\n",
        "hcl_text": "LIMIT = 1\ndef helper():\n    return dependency() + LIMIT\n",
    }
    namespace = {"NAMES": ("kept", "helper")}
    assert sorted(inert_patches(patch_targets(test, namespace=namespace), sources)) == sorted([
        "<test>:8: patches generate_abac.helper, which moved to hcl_text",
        "<test>:9: patches generate_abac.dependency, but hcl_text calls it",
        "<test>:10: patches generate_abac.LIMIT, which moved to hcl_text",
        "<test>:12: patches hcl_text.kept, but generate_abac calls it",
        "<test>:15: patches generate_abac.helper, which moved to hcl_text",
        "<test>:17: patches generate_abac with a name this test cannot resolve",
    ])
