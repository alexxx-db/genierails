"""make bootstrap-pipelines (design 18.2): fakes only, no live calls."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from databricks.sdk.errors import PermissionDenied

from scripts import bootstrap_pipelines as bp
from scripts.bootstrap_pipelines import Config, Refused, bootstrap_pipelines

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYER = "5a7e0000-0000-0000-0000-00000000beef"
SECRET = "dapi-super-secret-value"
READONLY_APP = "00000000-0000-0000-0000-0000000000aa"


class FakeDatabricks:
    """A stateful account + workspace: what one run creates the next run sees."""

    def __init__(self, *, caller_owns=True, account_admin=True, inherited_system=True,
                 deployer=True, readonly_permission=None, readonly_acls=None,
                 readonly_groups=(), groups_unreadable=False):
        self.writes: list[tuple] = []
        self.readonly_groups, self.groups_unreadable = readonly_groups, groups_unreadable
        self.sps: list[SimpleNamespace] = []
        if deployer:
            self.sps.append(SimpleNamespace(id="7", application_id=DEPLOYER,
                                            display_name="genierails-deployer"))
        self.policies: dict[int, list] = {}
        self.assignments: list = []
        self.grants: dict[tuple[str, str, str], set[str]] = {}
        self.acls: dict[str, list[dict]] = {
            "genie/space-1": [{"user_name": "caller@example.com", "permission_level": "CAN_MANAGE"}],
            "warehouses/wh-1": [{"user_name": "caller@example.com", "permission_level": "CAN_MANAGE"}],
        }
        if readonly_permission:
            # An existing read-only SP, already assigned to the workspace.
            from databricks.sdk.service.iam import WorkspacePermission

            self.sps.append(SimpleNamespace(id="55", application_id=READONLY_APP,
                                            display_name="genierails-dev-readonly"))
            self.assignments.append(SimpleNamespace(
                principal=SimpleNamespace(principal_id=55),
                permissions=[WorkspacePermission[readonly_permission]]))
        for path, entries in (readonly_acls or {}).items():
            self.acls[path] = self.acls[path] + entries
        if inherited_system:
            self.grants[("catalog", "system", "account users")] = {"USE_CATALOG"}
            self.grants[("schema", "system.information_schema", "account users")] = {
                "USE_SCHEMA", "SELECT"}
        owner = "caller@example.com" if caller_owns else "someone-else@example.com"
        fake = self

        class ServicePrincipals:
            def list(self, filter):
                attr, value = re.fullmatch(r'(\w+) eq "(.*)"', filter).groups()
                key = {"displayName": "display_name", "applicationId": "application_id"}[attr]
                return [sp for sp in fake.sps if getattr(sp, key) == value]

            def create(self, display_name, active):
                fake.writes.append(("create sp", display_name))
                sp = SimpleNamespace(id=str(100 + len(fake.sps)), display_name=display_name,
                                     application_id=f"00000000-0000-0000-0000-{len(fake.sps):012d}")
                fake.sps.append(sp)
                return sp

        class Secrets:
            def __getattr__(self, name):
                raise AssertionError(f"secrets API touched: {name}")

        class WorkspaceAssignment:
            def list(self, workspace_id):
                if not account_admin:
                    raise PermissionDenied("not an account admin")
                return list(fake.assignments)

            def update(self, workspace_id, principal_id, permissions):
                fake.writes.append(("assign", principal_id, [p.value for p in permissions]))
                fake.assignments.append(SimpleNamespace(
                    principal=SimpleNamespace(principal_id=principal_id), permissions=permissions))

        class Federation:
            def list(self, service_principal_id):
                return list(fake.policies.get(service_principal_id, []))

            def create(self, service_principal_id, policy):
                fake.writes.append(("federate", service_principal_id, policy.oidc_policy.subject))
                fake.policies.setdefault(service_principal_id, []).append(policy)

        class Grants:
            def get_effective(self, securable_type, full_name, principal):
                privileges = fake.grants.get((securable_type, full_name, principal), set())
                return SimpleNamespace(privilege_assignments=[SimpleNamespace(
                    principal=principal,
                    privileges=[SimpleNamespace(privilege=p) for p in sorted(privileges)])])

            def update(self, securable_type, full_name, changes):
                for change in changes:
                    added = {p.value for p in change.add}
                    fake.writes.append(("grant", securable_type, full_name, tuple(sorted(added))))
                    fake.grants.setdefault((securable_type, full_name, change.principal),
                                           set()).update(added)

        class ApiClient:
            def do(self, method, path, body=None):
                key = path.removeprefix("/api/2.0/permissions/")
                if method == "GET":
                    return {"access_control_list": list(fake.acls.get(key, []))}
                fake.writes.append(("acl", key, body["access_control_list"][0]["permission_level"]))
                fake.acls.setdefault(key, []).extend(body["access_control_list"])
                return {}

        self.account = SimpleNamespace(
            service_principals=ServicePrincipals(),
            service_principal_secrets=Secrets(),
            workspace_assignment=WorkspaceAssignment(),
            service_principal_federation_policy=Federation(),
            workspaces=SimpleNamespace(get=lambda workspace_id: SimpleNamespace(
                workspace_url="dbc-dev.cloud.databricks.com")),
            config=SimpleNamespace(host="https://accounts.cloud.databricks.com",
                                   client_secret=SECRET),
        )
        self.workspace = SimpleNamespace(
            current_user=SimpleNamespace(me=lambda: SimpleNamespace(
                user_name="caller@example.com", groups=[])),
            metastores=SimpleNamespace(
                current=lambda: SimpleNamespace(metastore_id="meta-1"),
                get=lambda _id: SimpleNamespace(owner="metastore-admins")),
            catalogs=SimpleNamespace(get=lambda name: SimpleNamespace(
                owner="metastore-admins" if name == "system" else owner)),
            schemas=SimpleNamespace(get=lambda name: SimpleNamespace(owner="metastore-admins")),
            service_principals=SimpleNamespace(list=self._workspace_sps),
            grants=Grants(),
            api_client=ApiClient(),
        )

    def _workspace_sps(self, filter, attributes):
        if self.groups_unreadable:
            raise PermissionDenied("cannot read service principals")
        app = re.fullmatch(r'applicationId eq "(.*)"', filter).group(1)
        return [SimpleNamespace(groups=[SimpleNamespace(display=g) for g in self.readonly_groups])
                for sp in self.sps if sp.application_id == app]

    def factory(self, _cfg):
        return self.account, lambda host, index=0: self.workspace


def _cfg(**overrides) -> Config:
    values = dict(cloud="aws", env="dev", ci="github", account_id="acct-1", workspace_id=123,
                  repo="acme/governance", catalogs=("dev_cat",), genie_space_ids=("space-1",),
                  warehouse_id="wh-1", deployer_client_id=DEPLOYER, yes=True)
    values.update(overrides)
    return Config(**values)


def _run(cfg: Config, fake: FakeDatabricks) -> list[str]:
    out: list[str] = []
    assert bootstrap_pipelines(cfg, client_factory=fake.factory, emit=out.append,
                               ask=lambda _q: pytest.fail("asked despite YES")) == 0
    return out


def test_apply_creates_readonly_sp_federation_and_section17_grants():
    fake = FakeDatabricks()
    out = _run(_cfg(), fake)
    kinds = [w[0] for w in fake.writes]
    assert kinds[0] == "create sp" and fake.writes[0][1] == "genierails-dev-readonly"
    assert ("assign", 101, ["USER"]) in fake.writes
    assert ("federate", 101, "repo:acme/governance:pull_request") in fake.writes
    assert ("federate", 7, "repo:acme/governance:environment:dev") in fake.writes
    assert ("grant", "catalog", "dev_cat", ("BROWSE", "USE_CATALOG", "USE_SCHEMA")) in fake.writes
    assert ("acl", "genie/space-1", "CAN_VIEW") in fake.writes
    assert ("acl", "warehouses/wh-1", "CAN_USE") in fake.writes
    # system.information_schema is inherited from account users: no grant.
    assert not [w for w in fake.writes if w[0] == "grant" and "system" in w[2]]
    policy = fake.policies[101][0].oidc_policy
    assert policy.issuer == "https://token.actions.githubusercontent.com"
    assert set(policy.audiences) == {"acct-1", "https://dbc-dev.cloud.databricks.com/oidc/v1/token"}
    assert "DEV_READONLY_CLIENT_ID = 00000000-0000-0000-0000-000000000001" in "\n".join(out)


def test_prod_federates_every_readonly_pipeline_identity_but_not_the_deployer():
    fake = FakeDatabricks()
    out = _run(_cfg(env="prod", catalogs=("prod_cat",), genie_space_ids=()), fake)
    subjects = {w[2] for w in fake.writes if w[0] == "federate"}
    assert subjects == {"repo:acme/governance:pull_request",
                        "repo:acme/governance:environment:production-plan",
                        "repo:acme/governance:environment:production-maintain"}
    assert not [w for w in fake.writes if w[0] == "federate" and w[1] == 7]
    assert any("prod deployer is not federated" in line for line in out)


def test_azure_devops_policies_use_pipeline_subjects():
    fake = FakeDatabricks()
    _run(_cfg(cloud="azure", ci="azure-devops", repo=None, ado_org="acme",
              ado_org_id="11111111-2222-3333-4444-555555555555", ado_project="gov"), fake)
    policy = fake.policies[101][0].oidc_policy
    assert policy.issuer == "https://vstoken.dev.azure.com/11111111-2222-3333-4444-555555555555"
    assert policy.audiences == ["api://AzureADTokenExchange"]
    assert policy.subject == "p://acme/gov/genierails-pr-check-azure"
    assert fake.policies[7][0].oidc_policy.subject == "p://acme/gov/genierails-dev-azure"


def test_plan_runs_preflight_and_writes_nothing(tmp_path):
    fake = FakeDatabricks()
    out: list[str] = []
    assert bootstrap_pipelines(_cfg(dry_run=True, yes=False), client_factory=fake.factory,
                               emit=out.append, ask=lambda _q: pytest.fail("asked")) == 0
    assert fake.writes == []
    text = "\n".join(out)
    assert "PREFLIGHT OK" in text and "DRY RUN: read-only preflight passed" in text
    assert "PLAN WILL CREATE service principal genierails-dev-readonly" in text


def test_plan_still_refuses_when_the_caller_lacks_authority():
    fake = FakeDatabricks(caller_owns=False)
    with pytest.raises(Refused, match="cannot grant USE CATALOG.*Nothing was changed"):
        bootstrap_pipelines(_cfg(dry_run=True), client_factory=fake.factory, emit=lambda _l: None)
    assert fake.writes == []


@pytest.mark.parametrize("fake_kwargs, cfg_overrides, message", [
    (dict(account_admin=False), {}, "needs an account admin"),
    (dict(caller_owns=False), {}, "cannot grant USE CATALOG"),
    (dict(inherited_system=False), {}, "cannot grant USE CATALOG on catalog 'system'"),
    (dict(deployer=False), {}, "deployer service principal .* was not found"),
    ({}, dict(genie_space_ids=("space-unmanaged",)), "cannot grant CAN_VIEW on agent"),
])
def test_refuses_before_any_write(fake_kwargs, cfg_overrides, message):
    fake = FakeDatabricks(**fake_kwargs)
    with pytest.raises(Refused, match=message):
        bootstrap_pipelines(_cfg(**cfg_overrides), client_factory=fake.factory,
                            emit=lambda _l: None)
    assert fake.writes == []


def test_rerun_reports_unchanged_and_writes_nothing():
    fake = FakeDatabricks()
    _run(_cfg(), fake)
    first = list(fake.writes)
    out = _run(_cfg(yes=False), fake)  # nothing pending: no confirmation asked
    assert fake.writes == first
    changes = [line for line in out if line.startswith(("CREATED", "GRANTED"))]
    assert changes == []
    assert "UNCHANGED: everything was already in place." in out
    assert sum(line.startswith("UNCHANGED ") for line in out) >= 7


def test_never_creates_or_prints_secrets():
    fake = FakeDatabricks()  # its secrets API raises when touched
    for cfg in (_cfg(), _cfg(dry_run=True), _cfg(env="prod", genie_space_ids=())):
        out = "\n".join(_run(cfg, fake))
        assert SECRET not in out
        assert not re.search(r"client_secret\s*=\s*\S", out)
        assert "no secrets" in out


def test_offline_plan_prints_settings_without_verifying():
    def offline(_cfg):
        raise ValueError("default auth: cannot configure default credentials")

    out: list[str] = []
    assert bootstrap_pipelines(_cfg(dry_run=True), client_factory=offline, emit=out.append) == 0
    assert any("DRY RUN OFFLINE" in line for line in out)


# ── the printed settings match the sample pipelines ────────────────────────

def _github_references(path: Path) -> set[str]:
    text = path.read_text()
    names = set(re.findall(r"\$\{\{\s*vars\.([A-Za-z0-9_]+)", text))
    names |= {f'Environment "{e}"' for e in re.findall(r"^\s*environment:\s*([\w-]+)", text, re.M)}
    return names


def _ado_references(path: Path) -> set[str]:
    text = path.read_text()
    defined = set(re.findall(r"^\s*-\s*name:\s*([A-Za-z_]\w*)", text, re.M))
    names = {n for n in re.findall(r"\$\(([A-Za-z_][A-Za-z0-9_.]*)\)", text)
             if "." not in n and n not in defined}
    names |= {f'Environment "{e}"' for e in re.findall(r"^\s*environment:\s*([\w-]+)", text, re.M)}
    names |= {f'Variable group "{g}"' for g in re.findall(r"^\s*-\s*group:\s*([\w-]+)", text, re.M)}
    names |= {f'Service connection "{c}"' for c in re.findall(
        r"^\s*(?:azureSubscription|awsCredentials):\s*([\w-]+)", text, re.M)}
    # The prod pipeline triggers on the dev pipeline by name.
    names |= {f"  {s} " for s in re.findall(r"^\s*source:\s*([\w-]+)", text, re.M)}
    return names


# References matched as printed text; everything else is a variable printed as "  NAME = value".
LITERAL = ("Environment ", "Variable group ", "Service connection ", " ")


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("ci", ["github", "azure-devops"])
@pytest.mark.parametrize("env", ["dev", "prod"])
def test_printed_settings_cover_every_sample_reference(cloud, ci, env):
    folder, refs = ((".github/workflows", _github_references) if ci == "github"
                    else ("azure-pipelines", _ado_references))
    samples = sorted((REPO_ROOT / cloud / folder).glob("*.yml"))
    assert len(samples) == 4
    cfg = _cfg(cloud=cloud, ci=ci, env=env, repo="acme/governance", ado_org="acme",
               ado_org_id="11111111-2222-3333-4444-555555555555", ado_project="gov")
    printed = "\n".join(bp.settings(cfg, "client", "https://host"))
    for sample in samples:
        missing = sorted(r for r in refs(sample)
                         if (r if r.startswith(LITERAL) else f"  {r} = ") not in printed)
        assert not missing, f"{sample.relative_to(REPO_ROOT)} references {missing}, not printed"


def test_codeowners_follow_the_persona_split():
    lines = bp._codeowners(_cfg(governance_owners="@acme/gov", agent_owners="@acme/agents"))
    owners = {line.split()[0]: line.split()[1] for line in lines if not line.startswith("#")}
    assert owners["/aws/envs/*/agents/"] == "@acme/agents"
    for path in ("/shared/", "/aws/envs/", "/aws/envs/*/env.auto.tfvars",
                 "/aws/envs/*/generated/", "/aws/envs/*/ack.txt", "/aws/.github/"):
        assert owners[path] == "@acme/gov"
    # Last match wins: the agents rule must come after the envs catch-all.
    paths = [line.split()[0] for line in lines if not line.startswith("#")]
    assert paths.index("/aws/envs/*/agents/") > paths.index("/aws/envs/")


# ── defaults from the committed env (read only) and the make target ───────

def test_defaults_come_from_committed_files_and_nothing_is_written(tmp_path):
    env_dir = tmp_path / "envs" / "dev"
    (env_dir / "agents").mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text(
        f'deployer_principal_id = "{DEPLOYER}"\nuc_tables = ["cat_a.s.t", "cat_b.s.*"]\n')
    (env_dir / "agents" / "Sales.auto.tfvars").write_text(
        'genie_space_id = "space-9"\nacl_groups = []\nuc_tables = ["cat_c.s.t"]\n')
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    defaults = bp.committed_defaults(env_dir, "dev")
    assert defaults["catalogs"] == ("cat_a", "cat_b", "cat_c")
    assert defaults["genie_space_ids"] == ("space-9",)
    assert defaults["deployer_client_id"] == DEPLOYER
    assert bp.committed_defaults(env_dir, "prod")["genie_space_ids"] == ()
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("bad, message", [
    (dict(ci="true"), "CI must be github or azure-devops"),
    (dict(env="staging"), "ENV must be dev or prod"),
    (dict(repo=None), "REPO=<owner>/<name>"),
    (dict(ci="azure-devops", ado_org_id=None), "ADO_ORG_ID"),
    (dict(catalogs=()), "CATALOGS="),
])
def test_bad_input_is_refused_before_any_call(bad, message):
    with pytest.raises(ValueError, match=message):
        bootstrap_pipelines(_cfg(**bad), client_factory=lambda _c: pytest.fail("called"),
                            emit=lambda _l: None)


@pytest.mark.gnu_make
def test_make_target_passes_plan_and_writes_nothing_to_the_repo(tmp_path, monkeypatch):
    from tests import script_stubs

    record = tmp_path / "args"
    recorder = tmp_path / "rec.sh"
    recorder.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\n')
    recorder.chmod(0o755)
    bin_dir = script_stubs.install(tmp_path / "bin", {"bootstrap_pipelines.py": str(recorder)})
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    # A runner's CI=true never picks the CI system: the command line does.
    monkeypatch.setenv("CI", "true")
    status = ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--ignored", "aws"]
    before = subprocess.run(status, capture_output=True, text=True).stdout
    result = subprocess.run(
        ["make", "-s", "-C", str(REPO_ROOT / "aws"), "bootstrap-pipelines", "ENV=prod",
         "CI=github", "ACCOUNT_ID=acct", "WORKSPACE_ID=42", "REPO=acme/gov", "PLAN=1",
         "CATALOGS=prod_cat"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    args = record.read_text().split("\n")
    assert "--dry-run" in args and "--yes" not in args
    assert args[args.index("--ci") + 1] == "github"
    assert args[args.index("--env") + 1] == "prod"
    assert args[args.index("--catalogs") + 1] == "prod_cat"
    assert subprocess.run(status, capture_output=True, text=True).stdout == before


# ── review fixes: no main-wide subject, refuse more-than-read-only access ──

def test_no_prod_identity_trusts_every_job_on_main():
    for env in ("dev", "prod"):
        subjects = {i.subject for role in bp.identities(_cfg(env=env), "https://h").values()
                    for i in role}
        assert not [s for s in subjects if ":ref:" in s], subjects
    assert "repo:acme/governance:environment:production-plan" in {
        i.subject for i in bp.identities(_cfg(env="prod"), "https://h")["readonly"]}


@pytest.mark.parametrize("cloud", ["aws", "azure"])
def test_prod_release_plan_job_runs_in_production_plan_environment(cloud):
    import yaml

    workflow = yaml.safe_load((REPO_ROOT / cloud / ".github/workflows/prod-release.yml").read_text())
    job = workflow["jobs"]["release-plan"]
    assert job["environment"] == "production-plan"
    assert job["permissions"]["id-token"] == "write"
    printed = "\n".join(bp.settings(_cfg(cloud=cloud, env="prod"), "c", "https://h"))
    assert 'Environment "production-plan": deployment branches: main only; no reviewer' in printed


def test_bootstrap_settings_name_pr_freshness_controls():
    github = "\n".join(bp.settings(_cfg(ci="github"), "c", "https://h"))
    assert "branches up to date (or use a merge queue)" in github
    ado = "\n".join(bp.settings(_cfg(ci="azure-devops", repo=None, ado_org="acme",
                                     ado_org_id="11111111-2222-3333-4444-555555555555",
                                     ado_project="gov"), "c", "https://h"))
    assert "Build expiration = Immediately when main is updated" in ado


def test_refuses_while_the_readonly_sp_is_a_workspace_admin():
    fake = FakeDatabricks(readonly_permission="ADMIN")
    with pytest.raises(Refused, match="ADMIN.*not read-only.*Demote it to USER.*Nothing was changed"):
        bootstrap_pipelines(_cfg(), client_factory=fake.factory, emit=lambda _l: None)
    with pytest.raises(Refused, match="not read-only"):
        bootstrap_pipelines(_cfg(dry_run=True), client_factory=fake.factory, emit=lambda _l: None)
    assert fake.writes == []
    # USER is exactly read-only: kept, not refused.
    fake = FakeDatabricks(readonly_permission="USER")
    out = _run(_cfg(), fake)
    assert not [w for w in fake.writes if w[0] in ("create sp", "assign")]
    assert "UNCHANGED workspace access workspace 123: USER" in out


@pytest.mark.parametrize("path, level", [
    ("genie/space-1", "CAN_RUN"), ("genie/space-1", "CAN_EDIT"), ("genie/space-1", "CAN_MANAGE"),
    ("warehouses/wh-1", "CAN_MANAGE"), ("warehouses/wh-1", "IS_OWNER"),
])
def test_refuses_a_stronger_direct_acl_instead_of_counting_it_done(path, level):
    fake = FakeDatabricks(readonly_permission="USER", readonly_acls={
        path: [{"service_principal_name": READONLY_APP, "permission_level": level}]})
    with pytest.raises(Refused, match=f"has {level} \\(directly\\) on .*not read-only.*by hand.*Nothing was changed"):
        bootstrap_pipelines(_cfg(), client_factory=fake.factory, emit=lambda _l: None)
    assert fake.writes == []


INHERITED = [
    # (path, too-strong level, how it reaches the SP, expected source in the message)
    ("genie/space-1", "CAN_MANAGE", "parent", "inherited from /directories/42"),
    ("genie/space-1", "CAN_MANAGE", "group", "via group 'data-eng'"),
    ("genie/space-1", "CAN_EDIT", "group", "via group 'data-eng'"),
    ("genie/space-1", "CAN_RUN", "users", "via group 'users'"),
    ("warehouses/wh-1", "CAN_MANAGE", "parent", "inherited from /sql/warehouses"),
    ("warehouses/wh-1", "IS_OWNER", "group", "via group 'data-eng'"),
    ("warehouses/wh-1", "CAN_MANAGE", "users", "via group 'users'"),
]


def _inherited_acl(level, how, parent):
    if how == "parent":
        return {"service_principal_name": READONLY_APP, "all_permissions": [
            {"permission_level": level, "inherited": True, "inherited_from_object": [parent]}]}
    group = "users" if how == "users" else "data-eng"
    return {"group_name": group, "all_permissions": [{"permission_level": level, "inherited": False}]}


@pytest.mark.parametrize("path, level, how, source", INHERITED)
def test_refuses_stronger_effective_access_and_names_its_source(path, level, how, source):
    parent = "/directories/42" if path.startswith("genie") else "/sql/warehouses"
    fake = FakeDatabricks(readonly_permission="USER", readonly_groups=("data-eng",),
                          readonly_acls={path: [_inherited_acl(level, how, parent)]})
    with pytest.raises(Refused) as refused:
        bootstrap_pipelines(_cfg(dry_run=True), client_factory=fake.factory, emit=lambda _l: None)
    message = str(refused.value)
    assert f"effectively has {level} ({source})" in message
    assert "Remove that grant by hand" in message and "Nothing was changed" in message
    with pytest.raises(Refused):
        bootstrap_pipelines(_cfg(), client_factory=fake.factory, emit=lambda _l: None)
    assert fake.writes == []
    # The exact read-only level by the same route still passes; only a direct
    # grant counts as done, so the SP gets its own.
    exact = "CAN_VIEW" if path.startswith("genie") else "CAN_USE"
    fake = FakeDatabricks(readonly_permission="USER", readonly_groups=("data-eng",),
                          readonly_acls={path: [_inherited_acl(exact, how, parent)]})
    _run(_cfg(), fake)
    assert ("acl", path, exact) in fake.writes
    assert any(f"UNCHANGED grant {'agent space-1' if path.startswith('genie') else 'warehouse wh-1'}"
               f": {exact}" in line for line in _run(_cfg(), fake))


def test_refuses_when_effective_permissions_cannot_be_read():
    fake = FakeDatabricks(readonly_permission="USER", groups_unreadable=True)
    with pytest.raises(Refused, match="cannot read the group memberships.*Nothing was changed"):
        bootstrap_pipelines(_cfg(), client_factory=fake.factory, emit=lambda _l: None)
    assert fake.writes == []


def test_refuses_a_readonly_sp_in_the_admins_group():
    fake = FakeDatabricks(readonly_permission="USER", readonly_groups=("admins",))
    with pytest.raises(Refused, match="'admins' group.*not read-only.*Nothing was changed"):
        bootstrap_pipelines(_cfg(), client_factory=fake.factory, emit=lambda _l: None)
    assert fake.writes == []
