#!/usr/bin/env python3
"""Set up the Databricks side of the GenieRails pipelines (design section 18.2).

For one env (dev or prod) and one CI system (GitHub Actions or Azure DevOps):

- creates or reuses the env's read-only service principal, which the PR check
  (and, for prod, the release-plan and maintain jobs) authenticate as;
- adds an OIDC federation policy for each pipeline identity that uses it, and
  for dev also on the dev deployer SP (``deployer_principal_id``) for the dev
  rehearsal. The prod deployer stays on the deployment machine in v1;
- grants exactly the read-only privileges of design section 17: workspace
  access, ``USE CATALOG``, ``USE SCHEMA`` and ``BROWSE`` on the env's catalogs,
  read on ``system.information_schema``, ``CAN_VIEW`` on the agents and,
  when ``--warehouse-id`` is given, ``CAN_USE`` on that warehouse;
- prints the repo variables, environments, branch protection and CODEOWNERS
  the sample pipelines expect. It never writes to the repo.

Every credential is OIDC: no secret is created, read or printed. Like
``bootstrap_sp.py`` it runs a read-only preflight first and refuses before any
write when the caller lacks authority; ``--dry-run`` stops after it. A re-run
changes nothing and reports UNCHANGED.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

SHARED = Path(__file__).resolve().parents[1]
for _path in (SHARED, SHARED / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import bootstrap_sp as sp_helpers  # noqa: E402
from bootstrap_sp import (  # noqa: E402
    _caller_context,
    _credentials_absent,
    _error_details,
    _has_effective_privilege,
    _is_permission_denied,
    _principal_has_effective_privilege,
    _value,
    _workspace_host,
)

CI_SYSTEMS = ("github", "azure-devops")
ENVS = ("dev", "prod")
CLOUDS = ("aws", "azure")
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
ADO_AUDIENCE = "api://AzureADTokenExchange"
# Section 17: read-only SPs need USE CATALOG, USE SCHEMA, BROWSE on the env's catalogs.
CATALOG_PRIVILEGES = ("USE_CATALOG", "USE_SCHEMA", "BROWSE")
# ... and read on system.information_schema.
SYSTEM_GRANTS = (
    ("catalog", "system", ("USE_CATALOG",)),
    ("schema", "system.information_schema", ("USE_SCHEMA", "SELECT")),
)
# A direct ACL above the read-only level makes the SP more than read-only:
# refused, never downgraded automatically.
GENIE_TOO_STRONG = {"CAN_RUN", "CAN_EDIT", "CAN_MANAGE"}
WAREHOUSE_TOO_STRONG = {"CAN_MANAGE", "IS_OWNER"}
# Read-only SPs never need more than workspace USER.
WORKSPACE_PERMISSION = "USER"


class Refused(RuntimeError):
    """The preflight found something this caller can't or mustn't change."""


@dataclasses.dataclass(frozen=True)
class Config:
    cloud: str
    env: str
    ci: str
    account_id: str
    workspace_id: int
    profile: str = "DEFAULT"
    workspace_profile: str | None = None
    sp_name: str = ""
    repo: str | None = None
    ado_org: str | None = None
    ado_org_id: str | None = None
    ado_project: str | None = None
    catalogs: tuple[str, ...] = ()
    genie_space_ids: tuple[str, ...] = ()
    warehouse_id: str | None = None
    deployer_client_id: str | None = None
    governance_owners: str = "@<governance-team>"
    agent_owners: str = "@<agent-owners>"
    dry_run: bool = False
    yes: bool = False

    @property
    def readonly_name(self) -> str:
        return self.sp_name or f"genierails-{self.env}-readonly"


@dataclasses.dataclass(frozen=True)
class Identity:
    """One pipeline identity a federation policy trusts."""

    label: str
    issuer: str
    subject: str
    audiences: tuple[str, ...]


def ado_pipeline_name(cloud: str, stage: str) -> str:
    """The Azure DevOps pipeline names; the federation subjects use them."""
    return {
        "pr": f"genierails-pr-check-{cloud}",
        "dev": f"genierails-dev-{cloud}",  # prod-release.yml triggers on this name
        "release": f"genierails-prod-release-{cloud}",
        "maintain": f"genierails-prod-maintain-{cloud}",
    }[stage]


def identities(cfg: Config, host: str) -> dict[str, list[Identity]]:
    """Pipeline identities per SP role ("readonly", "deployer") for cfg.env."""
    if cfg.ci == "github":
        # github-oidc requests the token for the account ID (host metadata) or the
        # workspace token endpoint; the policy accepts either.
        audiences = (cfg.account_id, f"{host.rstrip('/')}/oidc/v1/token")
        prefix = f"repo:{cfg.repo}"

        def ident(label: str, claim: str) -> Identity:
            return Identity(label, GITHUB_ISSUER, f"{prefix}:{claim}", audiences)

        pr = ident("PR check (pr-check.yml)", "pull_request")
        if cfg.env == "dev":
            return {"readonly": [pr],
                    "deployer": [ident("dev rehearsal (dev-rehearse.yml)", "environment:dev")]}
        return {"readonly": [
            pr,
            # Its own environment, so no other workflow on main can mint this token.
            ident("prod release plan (prod-release.yml)", "environment:production-plan"),
            ident("prod maintain (prod-maintain.yml)", "environment:production-maintain"),
        ], "deployer": []}

    issuer = f"https://vstoken.dev.azure.com/{cfg.ado_org_id}"

    def pipeline(label: str, stage: str) -> Identity:
        name = ado_pipeline_name(cfg.cloud, stage)
        return Identity(f"{label} (pipeline {name})", issuer,
                        f"p://{cfg.ado_org}/{cfg.ado_project}/{name}", (ADO_AUDIENCE,))

    pr = pipeline("PR check", "pr")
    if cfg.env == "dev":
        return {"readonly": [pr], "deployer": [pipeline("dev rehearsal", "dev")]}
    return {"readonly": [pr, pipeline("prod release plan", "release"),
                         pipeline("prod maintain", "maintain")], "deployer": []}


# ── committed config (read only) ───────────────────────────────────────────

def committed_defaults(env_dir: Path | None, env: str) -> dict[str, Any]:
    """Catalogs, agent IDs and the deployer ID from the committed env files."""
    if env_dir is None or not (Path(env_dir) / "env.auto.tfvars").is_file():
        return {"catalogs": (), "genie_space_ids": (), "deployer_client_id": None}
    import agent_files

    cfg = agent_files.merged_config(Path(env_dir))
    tables = list(cfg.get("uc_tables") or [])
    space_ids = []
    for space in cfg.get("genie_spaces") or []:
        tables += list(space.get("uc_tables") or [])
        if space.get("genie_space_id") and not space.get("detached"):
            space_ids.append(str(space["genie_space_id"]))
    catalogs = tuple(dict.fromkeys(
        str(table).split(".")[0] for table in tables if str(table).count(".") >= 2))
    deployer = str(cfg.get("deployer_principal_id") or "") or None
    return {
        "catalogs": catalogs,
        # Prod agent IDs are kept in state and the release receipt, never in git.
        "genie_space_ids": tuple(space_ids) if env == "dev" else (),
        "deployer_client_id": deployer,
    }


# ── preflight (read only) ──────────────────────────────────────────────────

@dataclasses.dataclass
class Step:
    """One idempotent change: done is True when it is already in place."""

    kind: str
    target: str
    detail: str
    done: bool
    apply: Callable[[], None] | None = None


def _sp_by(account: Any, attribute: str, value: str) -> list[Any]:
    escaped = value.replace('"', '\\"')
    return list(account.service_principals.list(filter=f'{attribute} eq "{escaped}"'))


def _policy_matches(policy: Any, identity: Identity) -> bool:
    oidc = _value(policy, "oidc_policy")
    return (
        str(_value(oidc, "issuer") or "") == identity.issuer
        and str(_value(oidc, "subject") or "") == identity.subject
        and set(identity.audiences) <= set(_value(oidc, "audiences") or [])
    )


def _list_policies(account: Any, sp: Any, who: str) -> list[Any]:
    try:
        return list(account.service_principal_federation_policy.list(
            service_principal_id=int(_value(sp, "id"))))
    except Exception as exc:
        raise Refused(
            f"cannot list the federation policies of {who}: the caller must be an account "
            f"admin. Nothing was changed. Cause: {_error_details(exc)}") from exc


def _acl_levels(acl: Any, principals: set[str]) -> set[str]:
    levels: set[str] = set()
    for entry in _value(acl, "access_control_list") or []:
        principal = next((_value(entry, name) for name in (
            "user_name", "group_name", "service_principal_name") if _value(entry, name)), None)
        if str(principal).casefold() not in principals:
            continue
        levels.add(str(_value(entry, "permission_level") or "").upper())
        levels.update(str(_value(item, "permission_level") or "").upper()
                      for item in _value(entry, "all_permissions") or [])
    return levels - {""}


def _effective_grants(acl: Any, sources: dict[str, str]) -> list[tuple[str, str]]:
    """(level, where it comes from) for every ACL entry of the given principals.

    sources maps a casefolded principal (the SP, or a group it is in) to a
    label; inherited levels also name the parent object they come from.
    """
    grants: list[tuple[str, str]] = []
    for entry in _value(acl, "access_control_list") or []:
        principal = next((_value(entry, name) for name in (
            "user_name", "group_name", "service_principal_name") if _value(entry, name)), None)
        label = sources.get(str(principal).casefold())
        if label is None:
            continue
        if _value(entry, "permission_level"):
            grants.append((str(_value(entry, "permission_level")).upper(), label))
        for item in _value(entry, "all_permissions") or []:
            level = str(_value(item, "permission_level") or "").upper()
            if not level:
                continue
            parents = [str(p) for p in _value(item, "inherited_from_object") or []]
            inherited = (f"inherited from {', '.join(parents)}" if parents
                         else "inherited" if _value(item, "inherited") else "")
            # The SP's own entry is direct unless the level comes from a parent object.
            where = (inherited or label) if label == "directly" else ", ".join(
                filter(None, (label, inherited)))
            grants.append((level, where))
    return grants


class Preflight:
    """Reads everything, decides every step and proves authority; writes nothing."""

    def __init__(self, cfg: Config, account: Any, workspace_client: Callable[..., Any],
                 emit: Callable[[str], None]):
        self.cfg, self.account, self.emit = cfg, account, emit
        self.workspace_client = workspace_client
        self.steps: list[Step] = []
        self.readonly: Any | None = None
        self.host = ""

    # The SP's application ID once it exists (created during apply when new).
    def client_id(self) -> str | None:
        return str(_value(self.readonly, "application_id")) if self.readonly else None

    def run(self) -> list[Step]:
        cfg, account = self.cfg, self.account
        try:
            assignments = list(account.workspace_assignment.list(workspace_id=cfg.workspace_id))
        except Exception as exc:
            if _is_permission_denied(exc):
                raise Refused(
                    f"caller cannot read workspace {cfg.workspace_id}'s assignments: "
                    "bootstrap-pipelines needs an account admin (it creates a service principal "
                    f"and federation policies). Nothing was changed. Cause: {_error_details(exc)}"
                ) from exc
            raise
        existing = _sp_by(account, "displayName", cfg.readonly_name)
        if len(existing) > 1:
            raise Refused(f"multiple service principals have display name {cfg.readonly_name!r}")
        self.readonly = existing[0] if existing else None
        self.host = _workspace_host(account, cfg.workspace_id)
        roles = identities(cfg, self.host)

        self.steps.append(Step("service principal", cfg.readonly_name,
                               "read-only, no secret", self.readonly is not None,
                               self._create_readonly))
        assigned = None
        if self.readonly is not None:
            sp_id = str(_value(self.readonly, "id"))
            assigned = next((a for a in assignments
                             if str(_value(_value(a, "principal"), "principal_id")) == sp_id), None)
            levels = {str(getattr(p, "value", p)).upper() for p in _value(assigned, "permissions") or []}
            if "ADMIN" in levels:
                raise Refused(
                    f"{cfg.readonly_name} is a workspace ADMIN in workspace {cfg.workspace_id}, "
                    "so it is not read-only. Demote it to USER by hand (bootstrap-pipelines "
                    "never revokes), then re-run. Nothing was changed.")
        self.steps.append(Step("workspace access", f"workspace {cfg.workspace_id}",
                               WORKSPACE_PERMISSION, assigned is not None, self._assign))

        existing_policies = (_list_policies(account, self.readonly, cfg.readonly_name)
                             if self.readonly is not None else [])
        for identity in roles["readonly"]:
            done = any(_policy_matches(p, identity) for p in existing_policies)
            self.steps.append(Step("federation policy", cfg.readonly_name,
                                   f"{identity.label}: {identity.subject}", done,
                                   self._federate(lambda: self.readonly, identity)))
        self._deployer(roles["deployer"])
        self._workspace_grants()
        return self.steps

    def _deployer(self, deployer_identities: list[Identity]) -> None:
        cfg = self.cfg
        if not deployer_identities:
            if cfg.env == "prod":
                self.emit("NOTE: the prod deployer is not federated: prod releases run on the "
                          "deployment machine until prod has remote state (design section 17)")
            return
        if not cfg.deployer_client_id:
            self.emit(f"NOTE: no deployer_principal_id in envs/{cfg.env}/env.auto.tfvars (or "
                      "DEPLOYER_CLIENT_ID): the dev rehearsal's federation is skipped; run "
                      "make bootstrap-sp first, commit deployer_principal_id, then re-run")
            return
        found = _sp_by(self.account, "applicationId", cfg.deployer_client_id)
        if len(found) != 1:
            raise Refused(
                f"deployer service principal {cfg.deployer_client_id!r} (deployer_principal_id) "
                "was not found in the account; create it with make bootstrap-sp. "
                "Nothing was changed.")
        deployer = found[0]
        name = f"deployer {cfg.deployer_client_id}"
        policies = _list_policies(self.account, deployer, name)
        for identity in deployer_identities:
            self.steps.append(Step(
                "federation policy", name, f"{identity.label}: {identity.subject}",
                any(_policy_matches(p, identity) for p in policies),
                self._federate(lambda: deployer, identity)))

    def _workspace_grants(self) -> None:
        cfg = self.cfg
        config = sp_helpers.Config(
            account_id=cfg.account_id, workspace_ids=(cfg.workspace_id,),
            sp_name=cfg.readonly_name, profile=cfg.profile,
            workspace_profiles=(cfg.workspace_profile,) if cfg.workspace_profile else ())
        workspace, _host = sp_helpers._authenticate_workspaces(
            config, self.account, self.workspace_client)[cfg.workspace_id]
        self.workspace = workspace
        caller, principals, is_admin = _caller_context(workspace)
        metastore_id = str(_value(workspace.metastores.current(), "metastore_id") or "")
        metastore_owner = ""
        try:
            metastore_owner = str(_value(workspace.metastores.get(metastore_id), "owner") or "")
        except Exception as exc:
            if not _is_permission_denied(exc):
                raise
        is_metastore_owner = metastore_owner.casefold() in principals
        client_id = self.client_id()

        def effective(securable: str, name: str, principal: str) -> Any | None:
            try:
                return workspace.grants.get_effective(
                    securable_type=securable, full_name=name, principal=principal)
            except Exception:
                return None  # fail closed: unknown is never treated as granted

        def missing(securable: str, name: str, wanted: tuple[str, ...],
                    inherited_from: str | None = None) -> tuple[str, ...]:
            have: set[str] = set()
            if inherited_from:
                found = effective(securable, name, inherited_from)
                have |= {p for p in wanted
                         if _principal_has_effective_privilege(found, inherited_from, p)}
            if client_id:
                found = effective(securable, name, client_id)
                have |= {p for p in wanted if _has_effective_privilege(found, p)}
            return tuple(p for p in wanted if p not in have)

        def require_authority(securable: str, name: str, need: tuple[str, ...]) -> None:
            if is_metastore_owner:
                return
            owner = ""
            getter = {"catalog": workspace.catalogs.get, "schema": workspace.schemas.get}[securable]
            try:
                owner = str(_value(getter(name), "owner") or "")
            except Exception as exc:
                if not _is_permission_denied(exc):
                    raise Refused(f"cannot inspect {securable} {name!r} in workspace "
                                  f"{cfg.workspace_id}. Nothing was changed. "
                                  f"Cause: {_error_details(exc)}") from exc
            if owner.casefold() in principals:
                return
            if owner.casefold().startswith("_workspace_admins_") and is_admin:
                return
            if _has_effective_privilege(effective(securable, name, caller), "MANAGE"):
                return
            raise Refused(
                f"caller {caller!r} cannot grant {sp_helpers._privilege_phrase(need)} on "
                f"{securable} {name!r}: not its owner ({owner or '<unavailable>'!r}), not the "
                f"metastore owner ({metastore_owner or '<unavailable>'!r}) and no effective "
                f"MANAGE. Have the owner run bootstrap-pipelines or grant it to "
                f"{cfg.readonly_name}. Nothing was changed.")

        for catalog in cfg.catalogs:
            need = missing("catalog", catalog, CATALOG_PRIVILEGES)
            if need:
                require_authority("catalog", catalog, need)
            self.steps.append(Step("grant", f"catalog {catalog}",
                                   " + ".join(need or CATALOG_PRIVILEGES), not need,
                                   self._grant("catalog", catalog, need)))
        for securable, name, wanted in SYSTEM_GRANTS:
            # Usually inherited from account users; granted only when it isn't.
            need = missing(securable, name, wanted, inherited_from="account users")
            if need:
                require_authority(securable, name, need)
            self.steps.append(Step("grant", f"{securable} {name}",
                                   " + ".join(need or wanted), not need,
                                   self._grant(securable, name, need)))

        for space_id in cfg.genie_space_ids:
            self._acl_step(f"genie/{space_id}", f"agent {space_id}", "CAN_VIEW",
                           GENIE_TOO_STRONG, caller, principals, is_admin)
        if cfg.warehouse_id:
            self._acl_step(f"warehouses/{cfg.warehouse_id}", f"warehouse {cfg.warehouse_id}",
                           "CAN_USE", WAREHOUSE_TOO_STRONG, caller, principals, is_admin)

    def _acl_step(self, path: str, target: str, level: str, too_strong: set[str],
                  caller: str, principals: set[str], is_admin: bool) -> None:
        try:
            acl = self.workspace.api_client.do("GET", f"/api/2.0/permissions/{path}")
        except Exception as exc:
            raise Refused(f"cannot read the permissions of {target}. Nothing was changed. "
                          f"Cause: {_error_details(exc)}") from exc
        client_id = self.client_id()
        sources = {group.casefold(): f"via group {group!r}" for group in self._sp_groups()}
        if client_id:
            sources[client_id.casefold()] = "directly"
        # Effective access: direct, through a group, or inherited from a parent object.
        effective = _effective_grants(acl, sources)
        stronger = sorted({f"{lvl} ({where})" for lvl, where in effective if lvl in too_strong})
        if stronger:
            raise Refused(
                f"{self.cfg.readonly_name} effectively has {'; '.join(stronger)} on {target}, "
                f"so it is not read-only. Remove that grant by hand (from the SP, group or "
                f"parent object named) so it has at most {level}; bootstrap-pipelines never "
                "downgrades or revokes. Nothing was changed.")
        # Only the exact read-only level, granted directly, counts as done.
        done = (level, "directly") in effective
        if not done and not is_admin and "CAN_MANAGE" not in _acl_levels(acl, principals) \
                and "IS_OWNER" not in _acl_levels(acl, principals):
            raise Refused(f"caller {caller!r} cannot grant {level} on {target}: not a workspace "
                          "admin and no CAN_MANAGE. Nothing was changed.")
        workspace = self.workspace

        def apply() -> None:
            workspace.api_client.do("PATCH", f"/api/2.0/permissions/{path}", body={
                "access_control_list": [{"service_principal_name": self.client_id(),
                                         "permission_level": level}]})

        self.steps.append(Step("grant", target, level, done, apply))

    def _sp_groups(self) -> set[str]:
        """Workspace groups the read-only SP is in; fails closed when unreadable."""
        if hasattr(self, "_groups"):
            return self._groups
        groups = {"users"}  # every workspace member is in it, including a new SP
        if self.readonly is not None:
            client_id = self.client_id()
            try:
                found = list(self.workspace.service_principals.list(
                    filter=f'applicationId eq "{client_id}"', attributes="groups"))
            except Exception as exc:
                raise Refused(
                    f"cannot read the group memberships of {self.cfg.readonly_name}, so its "
                    "effective permissions are unknown. Nothing was changed. "
                    f"Cause: {_error_details(exc)}") from exc
            for sp in found:
                groups.update(str(_value(g, "display")) for g in _value(sp, "groups") or []
                              if _value(g, "display"))
        if "admins" in {g.casefold() for g in groups}:
            raise Refused(
                f"{self.cfg.readonly_name} is in the workspace 'admins' group, so it is not "
                "read-only. Remove it from 'admins' by hand, then re-run. Nothing was changed.")
        self._groups = groups
        return groups

    # ── writes, used only after the preflight passed ──

    def _create_readonly(self) -> None:
        self.readonly = self.account.service_principals.create(
            display_name=self.cfg.readonly_name, active=True)

    def _assign(self) -> None:
        from databricks.sdk.service.iam import WorkspacePermission

        self.account.workspace_assignment.update(
            workspace_id=self.cfg.workspace_id,
            principal_id=int(_value(self.readonly, "id")),
            permissions=[WorkspacePermission[WORKSPACE_PERMISSION]],
        )

    def _federate(self, sp: Callable[[], Any], identity: Identity) -> Callable[[], None]:
        def apply() -> None:
            from databricks.sdk.service.oauth2 import FederationPolicy, OidcFederationPolicy

            self.account.service_principal_federation_policy.create(
                service_principal_id=int(_value(sp(), "id")),
                policy=FederationPolicy(
                    description=f"genierails {self.cfg.env}: {identity.label}",
                    oidc_policy=OidcFederationPolicy(
                        issuer=identity.issuer, subject=identity.subject,
                        audiences=list(identity.audiences))))
        return apply

    def _grant(self, securable: str, name: str, need: tuple[str, ...]) -> Callable[[], None]:
        def apply() -> None:
            from databricks.sdk.service.catalog import PermissionsChange, Privilege

            self.workspace.grants.update(
                securable_type=securable, full_name=name,
                changes=[PermissionsChange(principal=self.client_id(),
                                           add=[Privilege[p] for p in need])])
        return apply


# ── the settings to apply by hand ─────────────────────────────────────────

def _var_values(cfg: Config, client_id: str | None, host: str) -> dict[str, str]:
    other = "prod" if cfg.env == "dev" else "dev"
    later = f"<from make bootstrap-pipelines ENV={other} CI={cfg.ci}>"
    env, upper = cfg.env, cfg.env.upper()
    values = {"DATABRICKS_ACCOUNT_ID": cfg.account_id}
    for e in ENVS:
        u = e.upper()
        values[f"{u}_READONLY_CLIENT_ID"] = later
        values[f"{u}_DATABRICKS_WORKSPACE_ID"] = later
        values[f"{u}_DATABRICKS_WORKSPACE_HOST"] = later
    values[f"{upper}_READONLY_CLIENT_ID"] = client_id or "<application ID, shown after apply>"
    values[f"{upper}_DATABRICKS_WORKSPACE_ID"] = str(cfg.workspace_id)
    values[f"{upper}_DATABRICKS_WORKSPACE_HOST"] = host or "<workspace URL>"
    values["DEV_DEPLOYER_CLIENT_ID"] = (
        cfg.deployer_client_id if env == "dev" and cfg.deployer_client_id
        else "<deployer_principal_id from envs/dev/env.auto.tfvars>")
    if cfg.cloud == "aws":
        values["DEV_STATE_ROLE_ARN"] = "<IAM role for dev remote state, trusting this pipeline's OIDC>"
        values["DEV_STATE_REGION"] = "<region of the dev state bucket and lock table>"
    else:
        values["DEV_STATE_CLIENT_ID"] = "<Entra app for dev remote state, federated to this pipeline>"
        values["AZURE_TENANT_ID"] = "<Entra tenant ID>"
        values["AZURE_SUBSCRIPTION_ID"] = "<subscription of the dev state storage account>"
    return values


def _codeowners(cfg: Config) -> list[str]:
    gov, agents, c = cfg.governance_owners, cfg.agent_owners, cfg.cloud
    pipelines = "/.github/" if cfg.ci == "github" else f"/{c}/azure-pipelines/"
    rows = [
        ("# Governance engineer (design 18.1): product code, every env file by default", None),
        ("/shared/", gov),
        (f"/{c}/envs/", gov),
        (f"/{c}/envs/*/env.auto.tfvars", gov),
        (f"/{c}/envs/*/generated/", gov),
        (f"/{c}/envs/*/ack.txt", gov),
        ("# ack.txt owns detached tombstones; agents/ edits alone cannot revive them", None),
        ("# Data citizen (18.1): agent files; lifecycle revival also needs governance review", None),
        (f"/{c}/envs/*/agents/", agents),
        ("# Pipelines and this file", None),
        (pipelines, gov),
    ]
    if cfg.ci == "github":
        rows.append(("/CODEOWNERS", gov))
    return [row if owner is None else f"{row:<36} {owner}" for row, owner in rows]


def settings(cfg: Config, client_id: str | None, host: str) -> list[str]:
    """The repo-side settings, printed for a person to apply. Never secrets."""
    values = _var_values(cfg, client_id, host)
    state_vars = (["DEV_STATE_ROLE_ARN", "DEV_STATE_REGION"] if cfg.cloud == "aws"
                  else ["DEV_STATE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"])
    repo_vars = ["DATABRICKS_ACCOUNT_ID"] + [
        f"{e.upper()}_{name}" for e in ENVS
        for name in ("READONLY_CLIENT_ID", "DATABRICKS_WORKSPACE_ID", "DATABRICKS_WORKSPACE_HOST")]
    gov = cfg.governance_owners
    out = ["", "=" * 72,
           f"Settings to apply by hand ({cfg.ci}, {cfg.cloud}). bootstrap-pipelines never "
           "writes to the repo.",
           "Every value is an ID, not a credential: there are no secrets to store "
           "(OIDC only; never create a client secret for these SPs).", "=" * 72]

    def var_lines(names: list[str]) -> list[str]:
        return [f"  {name} = {values[name]}" for name in names]

    if cfg.ci == "github":
        out += ["", "Repository variables (Settings > Secrets and variables > Actions > Variables):"]
        out += var_lines(repo_vars)
        out += ["", 'Environment "dev" (deployment branches: main only; no reviewer), variables:']
        out += var_lines(["DEV_DEPLOYER_CLIENT_ID"] + state_vars)
        out += ["", f'Environment "production": required reviewers {gov} (governance code '
                "owners); prevent self-review; deployment branches: main only.",
                "  PROD_DEPLOYER_CLIENT_ID: not used in v1; add it here only once prod releases in CI.",
                "", 'Environment "production-plan": deployment branches: main only; no reviewer '
                "(the prod release-plan job; only it can use the prod read-only SP from main).",
                "", 'Environment "production-maintain": deployment branches: main only; no '
                "reviewer (the schedule is never held for approval).",
                "", "Branch protection on main (no direct pushes, including admins):",
                '  required status check "make check" (pr-check.yml), branches up to date;',
                "  pull request required, review from Code Owners required, stale approvals dismissed;",
                "  enforce for admins; no force pushes or deletions. Equivalent command:",
                f"  gh api -X PUT repos/{cfg.repo}/branches/main/protection --input - <<'EOF'",
                '  {"required_status_checks": {"strict": true, "contexts": ["make check"]},',
                '   "enforce_admins": true,',
                '   "required_pull_request_reviews": {"require_code_owner_reviews": true,',
                '     "dismiss_stale_reviews": true, "required_approving_review_count": 1},',
                '   "restrictions": null, "allow_force_pushes": false, "allow_deletions": false}',
                "  EOF",
                "", "CODEOWNERS (.github/CODEOWNERS; last matching rule wins):"]
        out += ["  " + line for line in _codeowners(cfg)]
        return out

    names = {stage: ado_pipeline_name(cfg.cloud, stage)
             for stage in ("pr", "dev", "release", "maintain")}
    connection = f"genierails-dev-state-{cfg.cloud}"
    out += ["", 'Variable group "genierails-ids" (Pipelines > Library; plain variables, '
            "none marked secret):"]
    out += var_lines(repo_vars + ["DEV_DEPLOYER_CLIENT_ID"]
                     + (["DEV_STATE_REGION"] if cfg.cloud == "aws" else ["AZURE_SUBSCRIPTION_ID"]))
    out += ["", "Pipelines (exact names: the federation subjects and prod-release.yml use them):",
            f"  {names['pr']:<34} {cfg.cloud}/azure-pipelines/pr-check.yml",
            f"  {names['dev']:<34} {cfg.cloud}/azure-pipelines/dev-rehearse.yml",
            f"  {names['release']:<34} {cfg.cloud}/azure-pipelines/prod-release.yml",
            f"  {names['maintain']:<34} {cfg.cloud}/azure-pipelines/prod-maintain.yml",
            "", f'Service connection "{connection}": '
            + ("AWS, workload identity federation to the dev state role "
               "(DEV_STATE_ROLE_ARN's role)." if cfg.cloud == "aws"
               else "Azure Resource Manager, workload identity federation, scoped to the "
                    "dev state storage account."),
            "", 'Environment "genierails-dev": Exclusive Lock check.',
            f'Environment "genierails-prod": Approvals check ({gov}, governance code owners; '
            "approvers may not approve their own runs), Branch control check (refs/heads/main "
            "only), Exclusive Lock check.",
            "", "Branch policies on main (no bypass, including admins):",
            f"  build validation: {names['pr']}, required, expires when main is updated;",
            "  require a pull request; minimum one reviewer; reset votes on new pushes;",
            "  automatically included reviewers, required, per path (the CODEOWNERS rules below).",
            "", "Path owners (Azure Repos has no CODEOWNERS file: add each as an automatically",
            "included reviewer policy; on a GitHub repo use it as .github/CODEOWNERS):"]
    out += ["  " + line for line in _codeowners(cfg)]
    return out


# ── command ─────────────────────────────────────────────────────────────────

def _plan_header(cfg: Config, emit: Callable[[str], None]) -> None:
    emit(f"GenieRails pipeline bootstrap plan ({cfg.env}, {cfg.ci}, {cfg.cloud})")
    emit(f"  account: {cfg.account_id}; workspace: {cfg.workspace_id}")
    emit(f"  read-only service principal: {cfg.readonly_name!r} (create or reuse; no secret)")
    emit(f"  catalogs: {', '.join(cfg.catalogs)}")
    if cfg.genie_space_ids:
        emit(f"  agents (CAN_VIEW): {', '.join(cfg.genie_space_ids)}")
    elif cfg.env == "prod":
        emit("  agents: none given; prod agent IDs are in state and the release receipt, so "
             "re-run with GENIE_SPACE_IDS=<ids> after the first release")
    if cfg.warehouse_id:
        emit(f"  warehouse (CAN_USE): {cfg.warehouse_id}")


def validate(cfg: Config) -> None:
    if cfg.cloud not in CLOUDS:
        raise ValueError(f"cloud must be one of {', '.join(CLOUDS)}")
    if cfg.env not in ENVS:
        raise ValueError(f"ENV must be dev or prod (the pipelines' variables are DEV_* and "
                         f"PROD_*), not {cfg.env!r}")
    if cfg.ci not in CI_SYSTEMS:
        raise ValueError(f"CI must be github or azure-devops, not {cfg.ci!r} (on the command "
                         "line: make bootstrap-pipelines CI=github ...)")
    if cfg.ci == "github" and not (cfg.repo and cfg.repo.count("/") == 1
                                   and all(cfg.repo.split("/"))):
        raise ValueError("CI=github needs REPO=<owner>/<name>, the repository the workflows run in")
    if cfg.ci == "azure-devops" and not (cfg.ado_org and cfg.ado_org_id and cfg.ado_project):
        raise ValueError("CI=azure-devops needs ADO_ORG (name), ADO_ORG_ID (organization GUID, "
                         "the token issuer) and ADO_PROJECT")
    if not cfg.catalogs:
        raise ValueError(f"no catalogs: none found in envs/{cfg.env}'s committed uc_tables; set "
                         "CATALOGS=<catalog>[,<catalog>...]")


def bootstrap_pipelines(
    cfg: Config,
    *,
    client_factory: Callable[[Any], tuple[Any, Callable[..., Any]]] | None = None,
    emit: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
) -> int:
    validate(cfg)
    _plan_header(cfg, emit)
    sp_config = sp_helpers.Config(
        account_id=cfg.account_id, workspace_ids=(cfg.workspace_id,), sp_name=cfg.readonly_name,
        profile=cfg.profile,
        workspace_profiles=(cfg.workspace_profile,) if cfg.workspace_profile else ())
    try:
        account, workspace_client = (client_factory or sp_helpers._clients)(sp_config)
        preflight = Preflight(cfg, account, workspace_client, emit)
        steps = preflight.run()
    except Refused:
        raise
    except Exception as exc:
        if cfg.dry_run and _credentials_absent(exc):
            emit("DRY RUN OFFLINE: credentials are absent, so no API calls could be made; "
                 "the service principal, federation policies, grants and caller authority "
                 "were not verified. No changes were made.")
            for line in settings(cfg, None, ""):
                emit(line)
            return 0
        raise

    for step in steps:
        state = "UNCHANGED" if step.done else ("WILL CREATE" if step.kind != "grant" else "WILL GRANT")
        emit(f"PLAN {state} {step.kind} {step.target}: {step.detail}")
    emit(f"PREFLIGHT OK: caller has the authority for every change "
         f"({sum(not s.done for s in steps)} to make, {sum(s.done for s in steps)} unchanged)")
    pending = [step for step in steps if not step.done]
    if cfg.dry_run:
        for line in settings(cfg, preflight.client_id(), preflight.host):
            emit(line)
        emit("DRY RUN: read-only preflight passed; no changes were made.")
        return 0
    if pending and not cfg.yes and ask(
            "Apply these changes? Type 'yes' to continue: ").strip().lower() != "yes":
        emit("Aborted; no changes were made.")
        return 1
    for step in steps:
        if step.done:
            emit(f"UNCHANGED {step.kind} {step.target}: {step.detail}")
            continue
        step.apply()
        emit(f"{'GRANTED' if step.kind == 'grant' else 'CREATED'} {step.kind} "
             f"{step.target}: {step.detail}")
    if not pending:
        emit("UNCHANGED: everything was already in place.")
    for line in settings(cfg, preflight.client_id(), preflight.host):
        emit(line)
    return 0


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v.strip() for v in (value or "").split(",") if v.strip()))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cloud", required=True, choices=CLOUDS)
    p.add_argument("--env", required=True)
    p.add_argument("--ci", default="")
    p.add_argument("--env-dir", type=Path, help="envs/<env> (read only; for defaults)")
    p.add_argument("--account-id", required=True)
    p.add_argument("--workspace-id", required=True, type=int)
    p.add_argument("--profile", default="DEFAULT", help="account-admin Databricks CLI profile")
    p.add_argument("--workspace-profile")
    p.add_argument("--sp-name", default="", help="default: genierails-<env>-readonly")
    p.add_argument("--repo", help="GitHub <owner>/<name>")
    p.add_argument("--ado-org")
    p.add_argument("--ado-org-id")
    p.add_argument("--ado-project")
    p.add_argument("--catalogs", help="default: the catalogs of the env's committed uc_tables")
    p.add_argument("--genie-space-ids", help="default (dev): the committed agents' IDs")
    p.add_argument("--warehouse-id", help="grant CAN_USE (prod maintain's drift audit needs one)")
    p.add_argument("--deployer-client-id", help="default: committed deployer_principal_id")
    p.add_argument("--governance-owners", default="@<governance-team>")
    p.add_argument("--agent-owners", default="@<agent-owners>")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    defaults = committed_defaults(args.env_dir, args.env)
    return Config(
        cloud=args.cloud, env=args.env, ci=args.ci, account_id=args.account_id,
        workspace_id=args.workspace_id, profile=args.profile,
        workspace_profile=args.workspace_profile, sp_name=args.sp_name, repo=args.repo,
        ado_org=args.ado_org, ado_org_id=args.ado_org_id, ado_project=args.ado_project,
        catalogs=_csv(args.catalogs) or defaults["catalogs"],
        genie_space_ids=_csv(args.genie_space_ids) or defaults["genie_space_ids"],
        warehouse_id=args.warehouse_id or None,
        deployer_client_id=args.deployer_client_id or defaults["deployer_client_id"],
        governance_owners=args.governance_owners, agent_owners=args.agent_owners,
        dry_run=args.dry_run, yes=args.yes,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        return bootstrap_pipelines(config_from_args(parser().parse_args(argv)))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
