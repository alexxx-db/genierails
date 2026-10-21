"""The champion walkthrough after release: live evidence, then maintain.

make evidence resolves the SQL warehouse itself (resolve_env_config.py
warehouse), so the documented command carries no WAREHOUSE_ID. The README
also once said adopt-custom-mask was not on main; it is.
"""

import re
from pathlib import Path

SHARED = Path(__file__).parents[1]
README = SHARED / "examples/dev_to_prod/README.md"
REFERENCE = SHARED / "examples/dev_to_prod/REFERENCE.md"
EVIDENCE_DOC = SHARED / "docs/evidence-report.md"
MAKEFILE = SHARED / "Makefile.shared"

EVIDENCE = "GENIERAILS_EVIDENCE_INTEGRATION=1 make evidence ENV=prod"


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def test_the_whole_flow_records_evidence_after_release_and_before_maintain():
    flow = _section(README.read_text(), "## The whole flow", "## Before you start")
    lines = [line.split("#")[0].strip() for line in flow.splitlines()]
    assert EVIDENCE in lines
    order = ["make release-plan ENV=prod", "make release ENV=prod", EVIDENCE, "make maintain ENV=prod"]
    assert [lines.index(step) for step in order] == sorted(lines.index(step) for step in order)


def test_prod_step_4_records_evidence_after_the_release_and_its_stop_advice():
    prod = _section(README.read_text(), "## Prod", "## Keep it safe")
    step = prod[prod.index("**4. Record evidence.**"):]
    assert prod.index("make release ENV=prod") < prod.index("**If it stops on coverage or drift**") \
        < prod.index("**4. Record evidence.**")
    assert f"```bash\n{EVIDENCE}\n```" in step
    # The checkout must be clean again before maintain / the next release-plan.
    assert "`make maintain` and the next `release-plan` need a clean checkout" in step


def test_no_doc_requires_an_explicit_warehouse_for_evidence():
    for path in (README, REFERENCE, EVIDENCE_DOC):
        text = path.read_text()
        assert not re.search(r"make evidence[^\n`]*WAREHOUSE_ID=", text), path
        assert "+ `WAREHOUSE_ID`" not in text, path
    assert f"```bash\n{EVIDENCE}\n```" in EVIDENCE_DOC.read_text()
    assert "`WAREHOUSE_ID=<id>` is optional" in EVIDENCE_DOC.read_text()


def test_make_evidence_resolves_the_warehouse_it_documents():
    makefile = MAKEFILE.read_text()
    recipe = makefile[makefile.index("\nevidence:"):makefile.index("\naudit-rulebook:")]
    assert 'resolve_env_config.py" warehouse --env-dir "$(ENV_DIR)" --explicit "$(WAREHOUSE_ID)"' in recipe


def test_readme_no_longer_says_adopt_custom_mask_is_not_on_main():
    readme = README.read_text()
    assert "is not on `main` yet" not in readme
    assert "make adopt-custom-mask ENV=dev TREATMENT=custom_<name>" in readme
    # ...because the target is on main.
    assert re.search(r"^adopt-custom-mask:", MAKEFILE.read_text(), re.MULTILINE)
