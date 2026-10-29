"""Docs for two fresh-repo gaps: an independent (not promoted) env, and the
account split file `make import ENV=account` reads. Each pins text and ties it
to the Makefile/script behavior it describes."""

import re
from pathlib import Path

import pytest

from scripts import release_gate

ROOT = Path(__file__).parents[2]
SHARED = ROOT / "shared"
MAKEFILE = (SHARED / "Makefile.shared").read_text()
REFERENCE = (ROOT / "REFERENCE.md").read_text()
ADVANCED = (SHARED / "docs/advanced.md").read_text()
IMPORT_EXISTING = (SHARED / "IMPORT_EXISTING.md").read_text()
TROUBLESHOOTING = (SHARED / "docs/troubleshooting.md").read_text()
IMPORT_SCRIPT = (SHARED / "scripts/import_existing.sh").read_text()

SEEDED_LINES = ('promote_from = "dev"', 'catalog_map  = { "<dev_catalog>" = "<bu2_catalog>" }')
NOT_FOUND = "ERROR: abac.auto.tfvars not found. Configure it before importing."


def _flat(text):
    return " ".join(text.split())


def _section(text, heading):
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end != -1 else None]


def _seeded_env_file(env):
    """What make setup ENV=<env> writes for a new non-default env: the template plus the seeded promotion lines."""
    printf = re.search(r"printf '(\\n# Promotion source[^']*)' \"\$\$env_name\"", MAKEFILE)
    assert printf, "make setup no longer seeds promote_from with the printf these docs describe"
    seed = printf.group(1).replace("$(DEFAULT_ENV)", "dev").replace("%s", env).replace("\\n", "\n")
    return (SHARED / "env.auto.tfvars.example").read_text() + seed


# -- 1. independent env --------------------------------------------------------

def test_setup_seeds_exactly_the_lines_the_docs_say_to_delete():
    text = _seeded_env_file("bu2")
    for line in SEEDED_LINES:
        assert text.splitlines().count(line) == 1, line
    for doc in (REFERENCE, ADVANCED):
        for line in SEEDED_LINES:
            assert line in doc, line


def test_deleting_the_seeded_lines_makes_an_independent_env_generate_accepts(tmp_path):
    env_dir = tmp_path / "envs" / "bu2"
    env_dir.mkdir(parents=True)
    seeded = _seeded_env_file("bu2")
    (env_dir / "env.auto.tfvars").write_text(seeded)
    assert release_gate.env_role(env_dir) == release_gate.PROMOTED
    with pytest.raises(release_gate.Refused):
        release_gate.generate_guard(env_dir, "bu2", "dev")

    edited = "\n".join(line for line in seeded.splitlines() if line not in SEEDED_LINES) + "\n"
    (env_dir / "env.auto.tfvars").write_text(edited)
    assert release_gate.env_role(env_dir) == release_gate.REHEARSE
    release_gate.generate_guard(env_dir, "bu2", "dev")


def test_generate_without_an_env_file_is_refused_for_bu2_as_the_docs_say(tmp_path):
    env_dir = tmp_path / "envs" / "bu2"
    env_dir.mkdir(parents=True)
    with pytest.raises(release_gate.Refused, match=r"Run make setup ENV=bu2 first"):
        release_gate.generate_guard(env_dir, "bu2", "dev")


def test_reference_documents_the_independent_env_steps():
    text = _flat(REFERENCE)
    assert '<a id="independent-env"></a>' in REFERENCE
    assert "**An independent env (not promoted).**" in text
    assert ("`make setup ENV=<env>` seeds every new env except `dev` and `account` as promoted from dev: it appends "
            "`promote_from = \"dev\"` and a placeholder `catalog_map`") in text
    assert "1. `make setup ENV=bu2`." in text
    assert "2. In `envs/bu2/env.auto.tfvars`, delete the seeded `promote_from = \"dev\"`" in text
    assert "Setup never rewrites an existing `env.auto.tfvars`, so they stay deleted." in text
    assert ("3. `make setup ENV=bu2` again: it now prints the rehearsal (Dev section) steps for bu2. Then "
            "`make generate ENV=bu2` and `make rehearse ENV=bu2` (which runs `make check ENV=bu2` first)") in text
    assert "[independent environments](shared/docs/advanced.md#independent-environments)" in text
    # The generate row points at it.
    assert ("With no `env.auto.tfvars`, refused for any env but `dev`: run `make setup ENV=<env>` first "
            "(see [an independent env](#independent-env)).") in text


def test_advanced_no_longer_says_generate_alone_creates_an_independent_env():
    section = _section(ADVANCED, "## Independent Environments")
    text = _flat(section)
    assert "generate it on its own instead of promoting:\n\n```bash\nmake generate ENV=bu2" not in section
    assert section.index("```bash\nmake setup ENV=bu2") < section.index("```bash\nmake generate ENV=bu2")
    assert "`make setup` seeds every new env except `dev` and `account` as promoted from dev" in text
    assert "delete the two seeded lines at the end of the file (and the comment above them)" in text
    assert "with no `promote_from` it prints the rehearsal (Dev section) steps for bu2" in text
    assert "`make capture` runs only in `dev`, and `make generate` imports agents only there." in text


def test_setup_never_rewrites_an_existing_env_file_and_capture_is_dev_only():
    # The docs rely on both: deleted lines stay deleted, and agents are imported only in dev.
    assert 'if [ ! -e "$$env_dir/env.auto.tfvars" ] && [ ! -L "$$env_dir/env.auto.tfvars" ]; then' in MAKEFILE
    assert ('@if [ "$(ENV)" != "$(DEFAULT_ENV)" ]; then echo "ERROR: capture runs only with ENV=$(DEFAULT_ENV)'
            in MAKEFILE)
    assert ('@if [ "$(ENV)" = "$(DEFAULT_ENV)" ]; then $(_AGENT_OWNERSHIP) import-new --env-dir' in MAKEFILE)


# -- 2. account split file before make import ENV=account ------------------------

def test_account_import_reads_the_file_only_promote_writes():
    assert NOT_FOUND in IMPORT_SCRIPT
    assert 'cd "$(ACCOUNT_ENV_DIR)" && "$(SHARED_ROOT)/scripts/import_existing.sh"' in MAKEFILE
    promote = MAKEFILE[MAKEFILE.index("\npromote: "):]
    promote = promote[:promote.index("\n\n")]
    assert "split_abac_config.py\" \"$$gen_abac\" ../$(ACCOUNT_ENV)/abac.auto.tfvars" in promote


def test_import_guide_runs_promote_before_the_account_import():
    usage = _section(IMPORT_EXISTING, "## Usage")
    assert usage.index("make promote ENV=dev") < usage.index("make import ENV=account")
    assert "# envs/account/abac.auto.tfvars, which the account import reads" in usage
    text = _flat(IMPORT_EXISTING)
    assert ("4. For `ENV=account`: `envs/account/abac.auto.tfvars` exists. `make setup` and a plain "
            "`make generate` don't write it; `make promote ENV=<env>` does") in text
    assert f"or the import stops at `{NOT_FOUND}`" in text


def test_troubleshooting_names_the_error_and_the_fix():
    brownfield = _section(TROUBLESHOOTING, "## Importing Existing Resources (Brownfield)")
    first = brownfield[brownfield.index("```bash"):]
    assert first.index("make promote ENV=dev") < first.index("make import ENV=account")
    heading = '### `make import ENV=account` fails with "abac.auto.tfvars not found"'
    assert heading in TROUBLESHOOTING
    entry = TROUBLESHOOTING[TROUBLESHOOTING.index(heading):]
    entry = entry[:entry.index("\n### ", 1)]
    assert "```bash\nmake promote ENV=dev\nmake import ENV=account\n```" in entry
    assert "[import existing](../IMPORT_EXISTING.md)" in entry


def test_reference_import_row_names_the_promote_prerequisite():
    assert ("it reads `envs/account/abac.auto.tfvars`, so on a fresh repo run `make promote ENV=<env>` first: it "
            "writes that file, and without it the import stops at `abac.auto.tfvars not found`") in _flat(REFERENCE)
