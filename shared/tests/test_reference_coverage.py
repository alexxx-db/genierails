"""REFERENCE.md lists every make target and every root setting, and only real ones.

The root REFERENCE.md is the one "every command and setting" page (design
18.5). It drifts silently when a target is added or renamed, so this checks it
against shared/Makefile.shared and the Terraform roots: every public target
(not test-*, not _-prefixed) and every ##-documented target has a command row,
every `make <target>` the page names exists, and every root variable is named.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SHARED = ROOT / "shared"
REFERENCE = ROOT / "REFERENCE.md"
STAGES = ("setup/local", "PR check", "dev rehearse", "promotion PR",
          "prod release-plan/release", "maintain", "operations", "none")

# A rule line: a lowercase target name at column 0 followed by ':' (not ':=').
_RULE = re.compile(r"^([a-z_][a-z0-9_-]*)\s*:(?![:=])(.*)$")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _rules():
    rules = {}
    for line in (SHARED / "Makefile.shared").read_text().splitlines():
        match = _RULE.match(line)
        if match:
            rules[match.group(1)] = "## " in match.group(2)
    return rules


def _cells(line):
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def _command_rows():
    rows = {}
    for line in REFERENCE.read_text().splitlines():
        if not line.startswith("| `make "):
            continue
        cells = _cells(line)
        target = cells[0].strip("`").split()[1]
        assert target not in rows, f"REFERENCE.md has two rows for make {target}"
        rows[target] = cells
    return rows


def _named_targets(text):
    named = set()
    for span in _CODE.findall(text):
        words = span.split()
        if words[0] == "make" and len(words) > 1 and re.fullmatch(r"[a-z_][a-z0-9_-]*", words[1]):
            named.add(words[1])
    return named


def _missing(required, rows):
    return sorted(set(required) - set(rows))


def test_every_public_and_documented_target_has_a_row():
    rules = _rules()
    public = {name for name in rules if not name.startswith(("_", "test-"))}
    documented = {name for name, has_doc in rules.items() if has_doc}
    # The parse itself: these are known to exist and must be found.
    assert {"release", "release-plan", "certify", "clean-all", "test-custom-mask"} <= public | documented
    missing = _missing(public | documented, _command_rows())
    assert not missing, "REFERENCE.md has no command row for: " + ", ".join(missing)


def test_reference_names_no_target_that_does_not_exist():
    rules = _rules()
    unknown = sorted(_named_targets(REFERENCE.read_text()) - set(rules))
    assert not unknown, "REFERENCE.md names make targets that don't exist: " + ", ".join(unknown)
    private = sorted(name for name in _command_rows() if name.startswith("_"))
    assert not private, "REFERENCE.md documents internal targets: " + ", ".join(private)


def test_every_command_row_has_parameters_purpose_and_a_section_17_stage():
    for target, cells in _command_rows().items():
        assert len(cells) == 4, f"make {target}: expected Command | Parameters | Purpose | Stage"
        assert cells[1] and cells[2], f"make {target}: empty parameters or purpose"
        assert cells[3].startswith(STAGES), f"make {target}: unknown stage {cells[3]!r}"


def test_every_root_variable_is_named():
    declared = set()
    for tf in (SHARED / "roots").glob("*/*.tf"):
        declared |= set(re.findall(r'^variable "([a-z0-9_]+)"', tf.read_text(), re.M))
    assert {"governance_mode", "databricks_client_secret", "table_readers"} <= declared
    spans = set(_CODE.findall(REFERENCE.read_text()))
    missing = sorted(declared - spans)
    assert not missing, "REFERENCE.md doesn't name these root variables: " + ", ".join(missing)


def test_every_setting_row_is_a_root_variable_or_read_by_make():
    declared = set()
    for tf in (SHARED / "roots").glob("*/*.tf"):
        declared |= set(re.findall(r'^variable "([a-z0-9_]+)"', tf.read_text(), re.M))
    sources = "\n".join(path.read_text() for path in SHARED.rglob("*.py") if "tests" not in path.parts)
    text = REFERENCE.read_text()
    settings = text[text.index("\n## Settings"):]
    for line in settings.splitlines():
        if not line.startswith("| `"):
            continue
        name = _cells(line)[0].strip("`")
        assert name in declared or f'"{name}"' in sources, f"setting {name} is neither a root variable nor read by make"


def test_reference_links_resolve_and_readme_links_to_it():
    for link in _LINK.findall(REFERENCE.read_text()):
        if re.match(r"[a-z]+:", link) or link.startswith("#"):
            continue
        target = (ROOT / link.split("#")[0]).resolve()
        assert target.exists(), f"REFERENCE.md links to a missing path: {link}"
    assert "](REFERENCE.md)" in (ROOT / "README.md").read_text()


@pytest.mark.parametrize("text, named", [
    ("run `make release ENV=prod MANIFEST=<m>`", {"release"}),
    ("Run `make` from `aws/`; `make ENV=prod` is not a target", set()),
    ("`make test-unit` and `make promote-to ENV=prod CHECK=1`", {"test-unit", "promote-to"}),
])
def test_named_targets_reads_make_code_spans(text, named):
    assert _named_targets(text) == named
