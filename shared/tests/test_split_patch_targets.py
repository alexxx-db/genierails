"""No test patch goes inert when code moves out of generate_abac.py (18.7).

generate_abac.py re-exports what moved, so imports keep working, but a patch
does not follow the code: `monkeypatch.setattr(generate_abac, "X", fake)`
rebinds generate_abac.X only. A function that moved to a split module still
calls its own module's X, and the test silently stops testing anything.

This test fails when a patch on generate_abac targets a name a split module
defines or calls, or when a patch on a split module targets a name
generate_abac.py calls. Fix it by patching the module that holds the caller.

It also fails when a test patches, reads or imports a name the module does
not bind (a patch with raising=False on a missing name silently patches
nothing; a moved name dropped from the re-exports breaks every test using it).
"""

import ast
import importlib
import sys
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


NOT_A_LOOP = object()


def _loop_values(tree, node, name, namespace):
    """The values a for-loop gives `name` around node, evaluated in namespace.

    None when the loop cannot be evaluated; NOT_A_LOOP when no loop binds name.
    """
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == name:
            if namespace is None:
                return None
            try:
                return list(eval(compile(ast.Expression(node.iter), "<loop>", "eval"), dict(namespace)))
            except Exception:  # noqa: BLE001 -- unresolvable means fail closed
                return None
    return NOT_A_LOOP


def _patched_modules(tree, node, arg, aliases, namespace):
    """The watched modules a patch call's first argument can be."""
    if not isinstance(arg, ast.Name):
        return []
    if arg.id in aliases:
        return [aliases[arg.id]]
    # `for namespace in (generate_abac, other): monkeypatch.setattr(namespace, ...)`
    values = _loop_values(tree, node, arg.id, namespace)
    if values is NOT_A_LOOP:
        return []
    if values is None:
        return [UNRESOLVED]
    found, pending = [], list(values)
    while pending:  # look inside tuples too: `for namespace, names in ((generate_abac, ...), ...)`
        value = pending.pop(0)
        if isinstance(value, (tuple, list)):
            pending += value
        elif getattr(value, "__name__", None) in WATCHED and hasattr(value, "__file__"):
            found.append(value.__name__)
    return found


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
            modules = _patched_modules(tree, node, args[0], aliases, namespace) \
                if name in PATCH_CALLS and len(args) >= 2 else []
            if modules:
                if isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
                    values = [args[1].value]
                else:
                    values = None
                    if isinstance(args[1], ast.Name):
                        values = _loop_values(tree, node, args[1].id, namespace)
                    if values is NOT_A_LOOP or not values \
                            or not all(isinstance(v, str) for v in values):
                        values = [UNRESOLVED]
                for module in modules:
                    for value in values:
                        add(module, value if module != UNRESOLVED else UNRESOLVED, node)
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


def name_uses(source: str, filename: str = "<test>") -> list[tuple[str, str, str]]:
    """(module, attribute, location) for every `module.X` read and `from module import X`."""
    tree = ast.parse(source)
    aliases = _module_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) \
                and isinstance(node.value, ast.Name) and node.value.id in aliases:
            found.append((aliases[node.value.id], node.attr, f"{filename}:{node.lineno}"))
        elif isinstance(node, ast.ImportFrom) and node.module in WATCHED and node.level == 0:
            found += [(node.module, alias.name, f"{filename}:{node.lineno}") for alias in node.names]
    return found


def _bound(source: str) -> set[str]:
    """Every name the module binds at top level (also inside top-level if/try/with)."""
    bound = {"__file__", "__name__", "__doc__", "__dict__", "__spec__", "__loader__", "__package__"}
    pending = list(ast.parse(source).body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                bound |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.Import):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {a.asname or a.name for a in node.names}
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                pending += getattr(node, field, [])
        elif isinstance(node, ast.ExceptHandler):
            pending += node.body
    return bound


def unresolved(targets, sources: dict[str, str], verb: str = "patches") -> list[str]:
    """Targets naming an attribute their module does not bind."""
    bound = {module: _bound(text) for module, text in sources.items()}
    return [f"{where}: {verb} {module}.{attr}, which {module}.py does not define or import"
            for module, attr, where in targets
            if attr != UNRESOLVED and module in bound and attr not in bound[module]]


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
    return problems + unresolved(targets, sources)


def _test_files():
    return [p for p in sorted(TESTS.rglob("*.py")) if p.name != Path(__file__).name]


def _all_test_patches():
    targets = []
    for path in _test_files():
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


def test_every_name_the_tests_read_or_import_from_generate_abac_resolves():
    uses = []
    for path in _test_files():
        uses += name_uses(path.read_text(), path.relative_to(SHARED).as_posix())
    assert len([u for u in uses if u[0] == SOURCE_MODULE]) > 100
    assert unresolved(uses, _sources(), verb="uses") == []


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
    assert counts.get("configure_databricks_env", 0) + counts.get("databricks_config", 0) >= 11
    for attr, at_least in {"fetch_tables_from_databricks": 9,
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
        SOURCE_MODULE: "from hcl_text import helper, LIMIT\ndependency = None\n"
                         "def kept():\n    return helper()\n"
                         "def main():\n    return kept()\n",
        "hcl_text": "LIMIT = 1\ndef helper():\n    return dependency() + LIMIT\n",
    }
    namespace = {"NAMES": ("kept", "helper")}
    assert sorted(inert_patches(patch_targets(test, namespace=namespace), sources)) == sorted([
        "<test>:8: patches generate_abac.helper, which moved to hcl_text",
        "<test>:9: patches generate_abac.dependency, but hcl_text calls it",
        "<test>:10: patches generate_abac.LIMIT, which moved to hcl_text",
        "<test>:12: patches hcl_text.kept, but generate_abac calls it",
        "<test>:12: patches hcl_text.kept, which hcl_text.py does not define or import",
        "<test>:15: patches generate_abac.helper, which moved to hcl_text",
        "<test>:17: patches generate_abac with a name this test cannot resolve",
    ])


def test_a_patch_or_use_of_a_name_generate_abac_does_not_bind_is_reported():
    test = '''
import generate_abac
from unittest import mock
from generate_abac import kept, gone_import

def test_x(monkeypatch):
    mock.patch("generate_abac.does_not_exist")
    monkeypatch.setattr(generate_abac, "also_missing", None, raising=False)
    monkeypatch.setattr(generate_abac, "kept", None)
    for namespace in (generate_abac,):
        for name in ("kept", "loop_missing"):
            monkeypatch.setattr(namespace, name, None, raising=False)
    generate_abac.gone_attr()
    generate_abac.kept()
'''
    sources = {SOURCE_MODULE: "import sys\ndef kept():\n    return sys\n"}
    namespace = {"generate_abac": type(sys)("generate_abac")}
    namespace["generate_abac"].__file__ = "generate_abac.py"
    assert inert_patches(patch_targets(test, namespace=namespace), sources) == [
        "<test>:7: patches generate_abac.does_not_exist, which generate_abac.py does not define or import",
        "<test>:8: patches generate_abac.also_missing, which generate_abac.py does not define or import",
        "<test>:12: patches generate_abac.loop_missing, which generate_abac.py does not define or import",
    ]
    assert sorted(unresolved(name_uses(test), sources, verb="uses")) == [
        "<test>:13: uses generate_abac.gone_attr, which generate_abac.py does not define or import",
        "<test>:4: uses generate_abac.gone_import, which generate_abac.py does not define or import",
    ]
