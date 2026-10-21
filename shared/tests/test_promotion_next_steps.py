"""What comes after promote-to, everywhere it is printed or documented.

release-plan plans only a commit on origin/main that has a passing rehearsal of
the destination's source env (its promote_from, dev by default) at that same
commit. So a deterministic promotion is always: promote-to, commit and push (or
merge the PR), rehearse the source env on that commit, release-plan, release.
Legacy envs have no release-plan and release directly.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SHARED = Path(__file__).parents[1]
ROOT = SHARED.parent
sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(SHARED / "scripts"))

from scripts import coverage_fix, migration  # noqa: E402
import classification_completeness  # noqa: E402
import run_scheduled_governance  # noqa: E402
from generate_abac import generate_next_steps  # noqa: E402

DETERMINISTIC_STG = 'governance_mode = "deterministic"\npromote_from = "stg"\n'
DETERMINISTIC = 'governance_mode = "deterministic"\n'
LEGACY = 'governance_mode = "legacy"\n'
FULL_STG = ["commit and push envs/prod/", "make rehearse ENV=stg on that commit", "make release-plan ENV=prod",
            "make release ENV=prod"]


@pytest.mark.parametrize("env,promoted,expected,absent", [
    ("stg", True, "make release-plan ENV=stg", "make rehearse ENV=stg   (applies"),
    ("stg", False, "make rehearse ENV=stg   (applies", "make release-plan ENV=stg"),
    ("prod", True, "make release-plan ENV=prod", "make rehearse ENV=prod   (applies"),
    ("dev", False, "make rehearse ENV=dev   (applies", "make release-plan ENV=dev"),
])
def test_generate_banner_follows_env_role(env, promoted, expected, absent, tmp_path):
    banner = "\n".join(generate_next_steps(tmp_path, "full", env, has_sql=True,
                                             champion_flow=True, tuning=False,
                                             source_env="dev", promoted=promoted))
    assert expected in banner
    assert absent not in banner


def _text(path: str) -> str:
    return " ".join((ROOT / path).read_text().split())


def _in_order(text: str, steps: list[str]) -> bool:
    pos = 0
    for step in steps:
        found = text.find(step, pos)
        if found < 0:
            return False
        pos = found + len(step)
    return True


SRC = "rehearse the source env (`promote_from`, dev by default)"
PLAN = ["`release-plan`", "`release`"]
MAKE_PLAN = ["make release-plan ENV=prod", "make release ENV=prod"]

# file -> where the passage starts -> what must follow, in order.
DOC_ORDER = [
    ("shared/examples/dev_to_prod/README.md", "make promote-to ENV=prod # copies the reviewed rules",
     ["git push", "make rehearse ENV=dev", *MAKE_PLAN]),
    ("shared/examples/dev_to_prod/README.md", "**3. Check, rehearse, plan, release.**",
     ["make check ENV=prod", "make rehearse ENV=dev", *MAKE_PLAN]),
    ("shared/examples/dev_to_prod/README.md", "plans only a commit on your `origin/main`",
     ["make rehearse ENV=dev", "dev is prod's `promote_from` in this walkthrough", "make rehearse ENV=stg"]),
    ("shared/examples/dev_to_prod/README.md", "**Reuse an existing mask** (the common case)",
     ["`make promote-to ENV=prod`", "`make rehearse ENV=dev` (again", "`make release-plan ENV=prod`",
      "`make release ENV=prod`"]),
    ("shared/examples/dev_to_prod/REFERENCE.md", "| `make release-plan ENV=prod` | 4 |",
     ["`make rehearse ENV=dev` receipt", "dev is prod's `promote_from` here", "`make rehearse ENV=stg`"]),
    ("shared/examples/dev_to_prod/REFERENCE.md", "Stops at `coverage-gate` or `audit-rulebook`",
     ["`promote-to`", "push to `origin/main`", "`make rehearse ENV=dev` on that commit",
      "`make release-plan ENV=prod`", "`make release ENV=prod`"]),
    ("shared/examples/dev_to_prod/REFERENCE.md", "Deterministic envs (`governance_mode = \"deterministic\"`) never",
     ["`make promote-to ENV=prod`", "commit and push", "`make rehearse ENV=dev` again", "`make release-plan ENV=prod`",
      "`make release ENV=prod`"]),
    ("shared/examples/dev_to_prod/REFERENCE.md", "- Then `make rehearse ENV=dev`, `make promote-to ENV=prod`",
     ["`make promote-to ENV=prod`", "push to `origin/main`", "`make rehearse ENV=dev` again",
      "`make release-plan ENV=prod`", "`make release ENV=prod`"]),
    ("REFERENCE.md", "| `make release-plan` |",
     ["a passing clean rehearsal of HEAD in the env's source env, its `promote_from` (default `dev`)"]),
    ("README.md", "promote the *rules* to prod", ["release a rehearsed commit", *PLAN]),
    ("shared/docs/pipelines.md", "**`make release-plan ENV=prod`** refuses",
     ["of prod's source env (`promote_from`, dev by default)"]),
    ("shared/docs/access.md", "`make promote-to ENV=prod` (seeds prod's",
     ["push that to `origin/main`", "`make rehearse` again on that commit", "`make release-plan ENV=prod`",
      "`make release ENV=prod`"]),
    ("shared/docs/import-genie-agent-from-ui.md", "The remaining workflow is unchanged",
     ["promote", "merge the promotion", SRC, *PLAN]),
    ("shared/docs/legacy-migration.md", "make promote-to ENV=prod # 2d.",
     ["make rehearse ENV=dev", *MAKE_PLAN]),
    ("shared/docs/design/deterministic-governance.md", "6. Approve and merge the promotion PR",
     ["once rehearse passes on that commit", "`make release-plan ENV=prod`", "`make release ENV=prod`"]),
    *[("shared/docs/design/deterministic-governance.md", row,
       ["merge", "rehearse the source env on", *PLAN])
      for row in ("| Promote A while B has unfinished dev edits |", "| Mask change while B is mid-curation |",
                  "| Grant another group access to A in prod |", "| Change who can read a table |",
                  "| Upgrade GenieRails |")],
    ("shared/docs/design/deterministic-governance.md", "| Stop governing a table |",
     ["a PR applies the removal: merge", "rehearse on the merge commit", *PLAN]),
    ("shared/docs/playbooks/agents.md", "make promote-to ENV=prod # carries the capture",
     ["rehearse dev on the merge commit", *MAKE_PLAN]),
    ("shared/docs/playbooks/agents.md", "3. **Merge.** After the rehearsal of",
     ["the source env (`promote_from`, dev by default) passes on the merge commit",
      "`make release-plan ENV=prod`", "`make release ENV=prod`"]),
    ("shared/docs/playbooks/agents.md", "governance adds it to `table_readers` in the same PR",
     ["merge", "rehearse the source env (`promote_from`, dev by default) on the merged commit", *PLAN]),
    ("shared/docs/playbooks/agents.md", "# merge, rehearse dev on the merge commit",
     ["release-plan", "release"]),
    ("shared/docs/playbooks/governance.md", "make promote-to ENV=prod # the promotion PR",
     ["rehearse dev on the merge commit", *MAKE_PLAN]),
    ("shared/docs/playbooks/governance.md", "make promote-to ENV=prod # commit envs/prod/, PR, merge",
     ["make rehearse ENV=dev # again, on the promotion's merge commit", *MAKE_PLAN]),
    ("shared/docs/playbooks/governance.md", "`catalog_map = { \"dev_finance\" = \"prod_finance\" }`, promote",
     ["merge the promotion", SRC, *PLAN]),
    ("shared/docs/playbooks/governance.md", "promoted from. Then generate",
     ["`promote-to`", "merge to `origin/main`", SRC, *PLAN]),
    ("shared/docs/playbooks/governance.md", "`make promote-to ENV=prod` in the same PR. Then merge",
     [f"{SRC} on the merged commit", *PLAN]),
    ("shared/docs/playbooks/one-team.md", "make promote-to ENV=prod # same PR",
     ["after rehearse passes on the merge commit", *MAKE_PLAN]),
    ("shared/docs/playbooks/one-team.md", "5. **Merge.**",
     ["`make rehearse ENV=dev` on the merge commit", "6. **Release.**", "`make release-plan ENV=prod`",
      "`make release ENV=prod`"]),
    *[("shared/docs/playbooks/one-team.md", row, ["merge, rehearse dev on the merge commit", *PLAN])
      for row in ("| Give another group an agent in prod |", "| Remove an agent |",
                  "| Remove a table from an agent |")],
]


@pytest.mark.parametrize("path, start, steps", DOC_ORDER, ids=[f"{p}::{s[:40]}" for p, s, _ in DOC_ORDER])
def test_docs_rehearse_the_promotion_commit_before_release_plan(path, start, steps):
    text = _text(path)
    assert start in text, f"{path}: passage {start!r} is gone"
    passage = text[text.index(start):][:700]
    assert _in_order(passage, steps), f"{path}: {passage[:300]}"


# A list-style "promote, release" / "PR, release" with no release-plan between.
SHORTCUT = re.compile(r"(?:\bpromot[\w-]*`?(?:\s+ENV=\S+?)?`?|\bPR|\bmerge)\s*(?:,|;|→|->)\s*(?:and\s+|then\s+)?"
                      r"(?:re-run\s+|run\s+)?`?(?:make\s+)?release\b(?!-plan)", re.IGNORECASE)
# Not deterministic flows: the legacy one-step release and a label of what a test scenario runs.
ALLOWED = ("legacy envs have no `release-plan`", "Scenario: dev to prod champion flow")


def test_no_doc_goes_from_promote_or_a_pr_straight_to_release():
    tracked = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, text=True, capture_output=True, check=True)
    found = []
    for path in tracked.stdout.split():
        if "CHANGELOG" in path:
            continue
        text = _text(path)
        for match in SHORTCUT.finditer(text):
            context = text[max(0, match.start() - 120):match.end() + 40]
            if not any(allowed in context for allowed in ALLOWED):
                found.append(f"{path}: …{context}…")
    assert not found, "\n".join(found)


# ── Printed next steps ───────────────────────────────────────────────────────


def _env(tmp_path: Path, name: str, text: str) -> Path:
    env_dir = tmp_path / "envs" / name
    env_dir.mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text(text)
    return env_dir


def _clean_env():
    drop = {"MAKEFLAGS", "MAKELEVEL", "ENV", "CI", "GENIERAILS_ALLOW_CI_APPLY"}
    return {key: value for key, value in os.environ.items() if key not in drop}


def _make(tmp_path: Path, *args: str, recipe: str = "") -> subprocess.CompletedProcess:
    extra = ["--eval", f"_t: ; @{recipe}", "_t"] if recipe else []
    return subprocess.run(
        ["make", "--no-print-directory", *extra, *args, f"CLOUD_ROOT={tmp_path}", f"SHARED_ROOT={SHARED}"],
        cwd=ROOT / "aws", text=True, capture_output=True, env=_clean_env())


@pytest.mark.parametrize("target", ["release", "release-plan", "maintain"])
def test_the_drift_hint_prints_the_full_sequence_in_a_deterministic_env(tmp_path, target):
    _env(tmp_path, "prod", DETERMINISTIC_STG)
    result = _make(tmp_path, "ENV=prod", recipe=f"$(_DRIFT_HINT) --target {target}")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert _in_order(out, ["fix the rule in stg", "make rehearse ENV=stg", "make promote-to ENV=prod", *FULL_STG]), out
    assert f"re-run make {target}" not in out


@pytest.mark.parametrize("target", ["release", "maintain"])
def test_the_drift_hint_keeps_the_direct_rerun_in_a_legacy_env(tmp_path, target):
    _env(tmp_path, "prod", LEGACY)
    result = _make(tmp_path, "ENV=prod", recipe=f"$(_DRIFT_HINT) --target {target}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "For class.* tags, follow 'How to fix' above; otherwise fix the rule in dev, make rehearse, "
        f"make promote-to ENV=prod, then re-run make {target} ENV=prod.")
    assert "release-plan" not in result.stdout


def test_every_drift_branch_prints_the_hint_for_its_own_target():
    makefile = (SHARED / "Makefile.shared").read_text()
    for target in ("release", "release-plan", "maintain"):
        assert f"$(_DRIFT_HINT) --target {target} >&2" in makefile
    assert "fix the rule in dev, make rehearse, make promote-to" not in makefile


@pytest.mark.parametrize("text, expected", [
    (DETERMINISTIC_STG, "commit and push envs/prod/ to origin/main (or merge its PR), make rehearse ENV=stg on that "
                        "commit, make release-plan ENV=prod and make release ENV=prod"),
    (DETERMINISTIC, "commit and push envs/prod/ to origin/main (or merge its PR), make rehearse ENV=dev on that "
                    "commit, make release-plan ENV=prod and make release ENV=prod"),
    (LEGACY, "make release ENV=prod"),
])
def test_release_steps_follow_the_env_mode_and_its_promote_from(tmp_path, text, expected):
    prod = _env(tmp_path, "prod", text)
    result = _make(tmp_path, "ENV=prod", recipe='$(_RELEASE_STEPS) --env-dir "$(ENV_DIR)" --env prod')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected == coverage_fix.release_steps("prod", prod)


def test_promote_and_ungovern_print_the_mode_aware_release_steps():
    makefile = (SHARED / "Makefile.shared").read_text()
    promote = makefile[makefile.index("=== Promote complete:"):]
    promote = promote[:promote.index("trap - EXIT")]
    assert '$(_RELEASE_STEPS) --env-dir "$$dest_env_dir" --env "$(DEST_ENV)" --source "$(SOURCE_ENV)"' in promote
    ungovern = makefile[makefile.index("ENV=$(ENV) is promoted, so nothing was applied."):][:300]
    assert '$(_RELEASE_STEPS) --env-dir "$(ENV_DIR)" --env "$(ENV)"' in ungovern
    assert "after rehearse, make release-plan" not in makefile


@pytest.mark.parametrize("text, expected", [
    (DETERMINISTIC_STG, "rehearse: ENV=prod is not allowed; rehearse its source env, then make release-plan ENV=prod "
                        "and make release ENV=prod"),
    (LEGACY, "rehearse: ENV=prod is not allowed; use make release ENV=prod"),
])
def test_rehearse_in_prod_points_at_the_release_for_the_env_mode(tmp_path, text, expected):
    _env(tmp_path, "prod", text)
    result = _make(tmp_path, "rehearse", "ENV=prod")
    assert result.returncode != 0
    assert expected in result.stderr


def test_a_failed_deterministic_release_never_suggests_make_apply(tmp_path):
    prod = _env(tmp_path, "prod", DETERMINISTIC_STG)
    result = subprocess.run([sys.executable, str(SHARED / "scripts" / "release_helpers.py"), "failed", str(prod),
                             "--env", "prod", "--reason", "apply failed"], text=True, capture_output=True)
    text = " ".join(result.stderr.split())
    assert "make apply" not in text
    assert _in_order(text, ["in a PR, then after merge:", "make rehearse ENV=stg on the merge commit", *MAKE_PLAN])
    assert _in_order(text, ["Otherwise fix the cause", *MAKE_PLAN])


def test_a_failed_legacy_release_keeps_apply_then_rerun(tmp_path):
    prod = _env(tmp_path, "prod", LEGACY)
    result = subprocess.run([sys.executable, str(SHARED / "scripts" / "release_helpers.py"), "failed", str(prod),
                             "--env", "prod", "--reason", "apply failed"], text=True, capture_output=True)
    assert "then run: make apply ENV=prod" in result.stderr
    assert "Then fix the cause and re-run make release ENV=prod." in result.stderr


@pytest.mark.parametrize("text, steps", [
    (DETERMINISTIC_STG, FULL_STG),
    (LEGACY, ["make release ENV=prod"]),
])
def test_scheduled_governance_and_classification_follow_the_env_mode(tmp_path, text, steps):
    prod = _env(tmp_path, "prod", text)
    assert _in_order(run_scheduled_governance._release_steps("prod", prod), steps)
    assert run_scheduled_governance._source_env(prod) == ("stg" if "stg" in text else "dev")
    assert _in_order(classification_completeness._release_steps(prod), steps)


@pytest.mark.parametrize("name, text, expected", [
    ("prod", DETERMINISTIC_STG, ["merge", "make rehearse ENV=stg on the merge commit", *MAKE_PLAN]),
    ("prod", DETERMINISTIC, ["merge", "make rehearse ENV=dev on the merge commit", *MAKE_PLAN]),
    ("dev", DETERMINISTIC, ["merge, then make rehearse ENV=dev"]),
])
def test_a_ready_cutover_rehearses_the_source_before_release_plan(tmp_path, name, text, expected):
    steps = migration.cutover_next(_env(tmp_path, name, text), name)
    assert _in_order(steps, expected), steps
    if name == "dev":
        assert "release-plan" not in steps
