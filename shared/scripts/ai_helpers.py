#!/usr/bin/env python3
"""Optional AI helpers (design 18.4, decision 5): drafts for review, never governance.

    suggest-treatment    make suggest-treatment ENV=dev CLASS=<class>: proposes a
                         mask-library treatment for a class nothing maps yet.
                         Writes drafts/treatments/<class>.json.
    draft-genie-content  make draft-genie-content ENV=dev SPACE=<agent>: drafts the
                         instructions, sample questions and benchmarks a captured
                         agent doesn't have. Writes drafts/genie/<agent>.json.
    draft-custom-mask    make draft-custom-mask ENV=dev TREATMENT=<name> TYPE=<type>:
                         drafts one mask expression and its exact-output fixture.
                         Writes drafts/custom_masks/<name>.sql and <name>.json.
    test-custom-mask     make test-custom-mask ENV=dev TREATMENT=<name>: runs the
                         mask library's exact-output checks on that draft (static
                         rules offline, then every fixture case on the dev SQL
                         warehouse, read-only). On a pass writes <name>.passed.json
                         with the digests of the files it tested. Calls no model.

These are the only commands that call a model, and only through ModelClient
below. Every one refuses under CI and runs only with ENV=dev whose physical
path is <cloud>/envs/dev; it writes only inside the real envs/dev/drafts/
(no symlink in any path component, files staged and moved into place). The
model sees only what a draft needs: class and treatment names, the mask
library, table names and column types; never data and never an agent's
existing instructions, questions or benchmarks. Fixture inputs are typed
literals only, so test-custom-mask never runs SQL a fixture supplies. Nothing in GenieRails
reads drafts/: generate, rehearse, release, maintain and check never import
this module, the rehearse fingerprint and make check's scratch copy skip the
folder, and a draft reaches config only when a person copies it there in a
reviewed PR (tag_treatments, the dev Genie UI then make capture).

The model endpoint is ai_helper_endpoint in envs/<env>/env.auto.tfvars (a
Databricks model serving endpoint). Without it the drafting commands stop with
a clear error; nothing else needs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol

import hcl2

SHARED_ROOT = Path(__file__).resolve().parents[1]
for _path in (SHARED_ROOT, SHARED_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from governance_policies import (  # noqa: E402
    DeterministicGovernanceError, normalize_sql_type, tag_treatment_errors,
)
from mask_library import load_library, type_family  # noqa: E402

DRAFTS_DIR = "drafts"
ENDPOINT_KEY = "ai_helper_endpoint"
# CI providers the applying-target guard knows, plus other common runners'
# markers (defense in depth; most also set CI). No opt-out:
# GENIERAILS_ALLOW_CI_APPLY is for isolated integration tests that apply, and
# never lets a model run in CI.
CI_MARKERS = ("TF_BUILD", "GITHUB_ACTIONS", "JENKINS_URL", "GITLAB_CI", "BUILDKITE", "CIRCLECI",
              "BUILD_BUILDID", "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI", "TEAMCITY_VERSION", "TRAVIS",
              "BITBUCKET_BUILD_NUMBER", "CODEBUILD_BUILD_ID", "DRONE", "SEMAPHORE", "APPVEYOR")
HELPER_ENV = "dev"
SYSTEM_PROMPT = (
    "You draft GenieRails configuration for a person to review before anything uses it. "
    "Reply with exactly one JSON object and nothing else."
)
# Families the mask library types its versions for; any other type only ever
# gets the full version, so there is nothing to draft.
MASKABLE_FAMILIES = ("STRING", "NUMERIC", "DATE", "TIMESTAMP", "TIMESTAMP_NTZ")
# A mask is one caller-independent, deterministic expression over `value`.
STATEMENT_WORDS = ("select", "create", "drop", "alter", "insert", "update", "delete", "merge",
                   "grant", "revoke", "truncate", "call", "execute")
CALLER_FUNCTIONS = ("current_user", "session_user", "user", "is_account_group_member", "is_member",
                    "current_groups", "current_principal")
NONDETERMINISTIC_FUNCTIONS = ("rand", "randn", "random", "uuid", "now", "current_timestamp",
                              "current_date", "current_timezone", "shuffle")
# Functions that read secrets, reach the network or other data, or resolve
# names at run time: never in a mask (ai_* is refused by prefix).
SIDE_EFFECT_FUNCTIONS = ("secret", "try_secret", "http_request", "read_files", "read_kafka", "read_kinesis",
                         "read_pubsub", "read_pulsar", "read_statestore", "identifier", "java_method",
                         "reflect", "try_reflect", "vector_search", "url_decode_to_request")
MAX_ITEMS = 10


class Refused(RuntimeError):
    """A helper stopped; nothing was written."""


# ── Guards ──────────────────────────────────────────────────────────────────

def ci_name(environ: dict | None = None) -> str | None:
    """The CI marker that is set, or None."""
    environ = os.environ if environ is None else environ
    if str(environ.get("CI", "")).strip().lower() in {"true", "1", "yes"}:
        return "CI"
    return next((name for name in CI_MARKERS if environ.get(name)), None)


def refuse_ci(command: str) -> None:
    marker = ci_name()
    if marker:
        raise Refused(
            f"make {command} refused: {marker} is set, and the AI helpers never run in CI "
            "(design decision 5). Run it on your own machine against the dev env.")


def helper_env(cloud_dir: Path, envs_dir: Path, env: str, env_dir: Path, command: str) -> Path:
    """The env to draft for: exactly ENV=dev, whose physical path is <cloud>/envs/dev.

    cloud_dir is the dir the cloud Makefile records from its own location
    (GENIERAILS_CLOUD_DIR); envs/ and envs/dev must be real directories there,
    with no symlink and no ENV_DIR or CLOUD_ROOT redirection, and dev must not
    be a promotion target.
    """
    from scripts.coverage_fix import promote_source

    if env != HELPER_ENV:
        why = "it is promoted" if env == "prod" else "the AI helpers run only in the dev env"
        raise Refused(f"make {command} refused: ENV={env} is not allowed ({why}). Run make {command} "
                      f"ENV={HELPER_ENV}, review the draft, and let promotion carry what you adopt.")
    if not str(cloud_dir or "").strip():
        raise Refused(f"make {command} refused: run it through the cloud Makefile (aws/ or azure/), "
                      "which records where envs/ is")
    cloud = Path(os.path.abspath(cloud_dir))
    expected_envs, expected_env = cloud / "envs", cloud / "envs" / HELPER_ENV
    if Path(os.path.abspath(envs_dir)) != expected_envs or Path(os.path.abspath(env_dir)) != expected_env:
        raise Refused(f"make {command} refused: ENV_DIR={env_dir} is not envs/{HELPER_ENV} of this checkout "
                      f"({expected_env}); drop ENV_DIR and CLOUD_ROOT")
    for path in (expected_envs, expected_env):
        if path.is_symlink() or not path.is_dir():
            raise Refused(f"make {command} refused: {path} is a symlink or not a directory; the AI helpers "
                          "write only inside a real envs/dev")
    if os.path.realpath(expected_env) != os.path.join(os.path.realpath(cloud), "envs", HELPER_ENV):
        raise Refused(f"make {command} refused: {expected_env} does not resolve to envs/{HELPER_ENV} of this checkout")
    env_file = expected_env / "env.auto.tfvars"
    if env_file.is_symlink() or not env_file.is_file():
        raise Refused(f"make {command} refused: envs/{HELPER_ENV}/env.auto.tfvars is missing or a symlink; "
                      f"run make setup ENV={HELPER_ENV}.")
    source = promote_source(expected_env)
    if source:
        raise Refused(f"make {command} refused: envs/{HELPER_ENV} is promoted (its promote_from is {source}); "
                      "the AI helpers draft only in the env rules are promoted from.")
    return expected_env


def _env_cfg(env_dir: Path) -> dict:
    try:
        return hcl2.loads((env_dir / "env.auto.tfvars").read_text())
    except Exception as exc:  # hcl2 raises lark errors
        raise Refused(f"envs/{env_dir.name}/env.auto.tfvars is not valid HCL: {exc}") from exc


def endpoint(env_dir: Path, command: str) -> str:
    """ai_helper_endpoint from the env's config, or a clear error."""
    value = _env_cfg(env_dir).get(ENDPOINT_KEY)
    if isinstance(value, list):
        value = value[0] if value else ""
    value = str(value or "").strip()
    if not value:
        raise Refused(
            f"make {command} needs a model endpoint, and envs/{env_dir.name}/env.auto.tfvars has no "
            f"{ENDPOINT_KEY}. Set it to a Databricks model serving endpoint, e.g.\n"
            f'  {ENDPOINT_KEY} = "databricks-claude-sonnet-4-6"\n'
            "Only the AI helpers use it; generate, rehearse, release and maintain never call a model.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise Refused(f"{ENDPOINT_KEY} = {value!r} is not a serving endpoint name")
    return value


# ── The model client: the one place GenieRails calls a model ────────────────

class ModelClient(Protocol):
    endpoint: str

    def complete(self, system: str, prompt: str) -> str: ...


def workspace_client(auth_file: Path):
    """A WorkspaceClient from the env's auth.auto.tfvars (unset values fall back to the SDK's own auth)."""
    from databricks.sdk import WorkspaceClient

    auth = {}
    if auth_file.is_file():
        try:
            auth = hcl2.loads(auth_file.read_text())
        except Exception as exc:
            raise Refused(f"{auth_file} is not valid HCL: {exc}") from exc
    kwargs = {key: str(auth.get(name) or "").strip() for key, name in (
        ("host", "databricks_workspace_host"), ("client_id", "databricks_client_id"),
        ("client_secret", "databricks_client_secret"))}
    return WorkspaceClient(**{key: value for key, value in kwargs.items() if value})


class DatabricksModel:
    """A Databricks model serving endpoint, called with the env's workspace credentials."""

    def __init__(self, endpoint_name: str, auth_file: Path):
        refuse_ci("<AI helper>")  # also here, so no other caller can reach a model in CI
        self.endpoint = endpoint_name
        self._auth_file = auth_file

    def complete(self, system: str, prompt: str) -> str:
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

        try:
            response = workspace_client(self._auth_file).serving_endpoints.query(
                name=self.endpoint, max_tokens=8192,
                messages=[ChatMessage(role=ChatMessageRole.SYSTEM, content=system),
                          ChatMessage(role=ChatMessageRole.USER, content=prompt)])
        except Refused:
            raise
        except Exception as exc:
            raise Refused(f"model endpoint {self.endpoint} failed: {exc}") from exc
        content = response.choices[0].message.content if response.choices else None
        if isinstance(content, list):
            content = "\n".join(
                (block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")) or ""
                for block in content)
        if not isinstance(content, str) or not content.strip():
            raise Refused(f"model endpoint {self.endpoint} returned no text")
        return content


def reply_json(text: str) -> dict:
    """The JSON object a model replied with (a ```json fence is allowed)."""
    body = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, flags=re.DOTALL)
    try:
        value = json.loads(fence.group(1) if fence else body)
    except json.JSONDecodeError as exc:
        raise Refused(f"the model's reply is not one JSON object ({exc}); nothing was written. Re-run to retry.") from exc
    if not isinstance(value, dict):
        raise Refused("the model's reply is not a JSON object; nothing was written. Re-run to retry.")
    return value


# ── Drafts ──────────────────────────────────────────────────────────────────

def render(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ── Contained file access under envs/dev ─────────────────────────────────────
# helper_env proved envs/dev is a real directory at its physical path. Every
# path below it that a helper reads, writes or removes must also have no
# symlink in any component (the rule promote-to and check apply, #88/#119), and
# writes land only inside the real envs/dev/drafts/.

def _contained(env_dir: Path, path: Path, *, under_drafts: bool, directory: bool = False) -> Path:
    """path, after refusing a symlink or wrong file type in any component below env_dir."""
    env_dir, path = Path(env_dir), Path(path)
    rel = path.relative_to(env_dir)
    if ".." in rel.parts or (under_drafts and rel.parts[:1] != (DRAFTS_DIR,)):
        raise Refused(f"{path} is not inside envs/{env_dir.name}/{DRAFTS_DIR if under_drafts else ''}")
    current = env_dir
    for i, part in enumerate(rel.parts):
        current = current / part
        if current.is_symlink():
            raise Refused(f"{_rel(env_dir, current)} is a symlink; the AI helpers read and write only real "
                          f"files inside envs/{env_dir.name}. Replace it with the file or directory it points to.")
        want_file = i == len(rel.parts) - 1 and not directory
        if current.exists() and not (current.is_file() if want_file else current.is_dir()):
            raise Refused(f"{_rel(env_dir, current)} is not a {'regular file' if want_file else 'directory'}")
    root = os.path.realpath(env_dir / (DRAFTS_DIR if under_drafts else ""))
    real = os.path.realpath(path)
    if not (real.startswith(root + os.sep) or (directory and real == root)):
        raise Refused(f"{path} resolves outside {root}")
    return path


def read_file(env_dir: Path, path: Path, *, under_drafts: bool = True) -> str:
    """A regular file's text, refusing symlinks (also at open time, O_NOFOLLOW)."""
    _contained(env_dir, path, under_drafts=under_drafts)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as handle:
        return handle.read()


def remove_draft(env_dir: Path, path: Path) -> None:
    _contained(env_dir, path, under_drafts=True)
    if path.exists():
        path.unlink()


def _make_dirs(env_dir: Path, folder: Path) -> None:
    current = Path(env_dir)
    for part in folder.relative_to(env_dir).parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            current.mkdir()
        _contained(env_dir, current, under_drafts=True, directory=True)


def write_drafts(env_dir: Path, files: dict[Path, str], force: bool) -> None:
    """Write every file or none, inside envs/dev/drafts/ only; a changed draft needs FORCE=1.

    Each file is staged beside its destination (created exclusively, never
    following a link) and moved into place only after every stage succeeded.
    """
    for path in files:
        _contained(env_dir, path, under_drafts=True)
    clash = [p for p, text in files.items() if p.exists() and read_file(env_dir, p) != text]
    if clash and not force:
        raise Refused(
            "a draft already exists and would change: " + ", ".join(_rel(env_dir, p) for p in clash)
            + ". Review or commit it first, or re-run with FORCE=1 to replace it.")
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text in files.items():
            _make_dirs(env_dir, path.parent)
            temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            staged.append((temp, path))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        for temp, path in staged:
            _contained(env_dir, path, under_drafts=True)  # nothing swapped in a link meanwhile
        for temp, path in staged:
            os.replace(temp, path)
        staged = []
    finally:
        for temp, _path in staged:
            temp.unlink(missing_ok=True)


def _rel(env_dir: Path, path: Path) -> str:
    return f"envs/{env_dir.name}/{path.relative_to(env_dir)}"


def _drafted_by(command: str, client: ModelClient) -> dict:
    return {"command": f"make {command}", "model_endpoint": client.endpoint,
            "status": "draft for review: GenieRails never reads drafts/"}


# ── suggest-treatment ───────────────────────────────────────────────────────

def _class_label(value: str) -> str:
    name = (value or "").strip().removeprefix("class.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", name):
        raise Refused(f"CLASS={value!r} is not a class name (lowercase, words joined by '_', e.g. CLASS=loyalty_id)")
    return f"class.{name}"


def suggest_treatment(env_dir: Path, class_name: str, client: ModelClient, force: bool = False) -> Path:
    label = _class_label(class_name)
    library = load_library()
    owner = {name: treatment for treatment, item in library["treatments"].items() for name in item["classes"]}
    name = label.removeprefix("class.")
    if name in owner:
        raise Refused(f"{label} is already mapped by the mask library (to {owner[name]}); nothing to suggest.")
    mapped = _env_cfg(env_dir).get("tag_treatments") or {}
    if isinstance(mapped, dict) and label in mapped:
        raise Refused(f"{label} is already mapped in envs/{env_dir.name}/env.auto.tfvars tag_treatments "
                      f"(to {mapped[label]}); nothing to suggest.")
    catalog = {t: {"classes": item["classes"], "partial": item.get("partial_by_type") or item["partial"],
                   "full": item["full"], "types": item["types"]}
               for t, item in sorted(library["treatments"].items())}
    prompt = (
        f"A Unity Catalog column carries the data classification tag {label}, which the GenieRails mask "
        "library does not map. Pick the existing mask-library treatment that protects it best. Tier 2 sees "
        "the partial version and tier 3 the full one; when unsure, prefer the stricter treatment.\n"
        f"Mask-library treatments (JSON): {json.dumps(catalog, sort_keys=True)}\n"
        'Reply as {"treatment": "<one treatment name above, or null if none fits>", "rationale": "<two '
        'sentences>", "custom_mask": null or {"name": "<lowercase_name>", "type": "<SQL type>", '
        '"description": "<the partial version to draft>"}}. Set custom_mask only when treatment is null.')
    reply = reply_json(client.complete(SYSTEM_PROMPT, prompt))
    treatment, rationale, custom = reply.get("treatment"), reply.get("rationale"), reply.get("custom_mask")
    if not isinstance(rationale, str) or not rationale.strip():
        raise Refused("the model gave no rationale; nothing was written. Re-run to retry.")
    draft = {"class": label, "rationale": rationale.strip(), "drafted_by": _drafted_by("suggest-treatment", client)}
    if treatment is not None:
        problems = tag_treatment_errors({label: treatment}, library)
        if problems or custom:
            raise Refused("the model's suggestion is not usable; nothing was written:\n  - "
                          + "\n  - ".join(problems or ["it set both treatment and custom_mask"]))
        item = library["treatments"][treatment]
        draft |= {
            "suggested_treatment": treatment,
            "library_versions": {"partial": item.get("partial_by_type") or item["partial"], "full": item["full"],
                                 "types": item["types"]},
            "to_adopt": (f'add "{label}" = "{treatment}" to tag_treatments in envs/{env_dir.name}/env.auto.tfvars '
                         f"(or make scaffold-treatments ENV={env_dir.name} TREATMENT={treatment}) in a PR, "
                         f"then make generate ENV={env_dir.name}"),
        }
    else:
        if not isinstance(custom, dict) or not all(isinstance(custom.get(k), str) and custom[k].strip()
                                                   for k in ("name", "type", "description")):
            raise Refused("the model found no library treatment and gave no custom_mask {name, type, description}; "
                          "nothing was written. Re-run to retry.")
        mask_name = _mask_name(custom["name"])
        sql_type = _mask_type(custom["type"])
        draft |= {
            "suggested_treatment": None,
            "custom_mask": {"name": mask_name, "type": sql_type, "description": custom["description"].strip()},
            "to_adopt": (f"no mask-library treatment fits. Draft one with make draft-custom-mask ENV={env_dir.name} "
                         f"TREATMENT={mask_name} TYPE={sql_type}, then make test-custom-mask ENV={env_dir.name} "
                         f"TREATMENT={mask_name}. Until a mapping is adopted the column keeps the fail-safe "
                         "never-raw redaction."),
        }
    path = env_dir / DRAFTS_DIR / "treatments" / f"{label.removeprefix('class.')}.json"
    write_drafts(env_dir, {path: render(draft)}, force)
    return path


# ── draft-genie-content ─────────────────────────────────────────────────────

def _text(value) -> str:
    if isinstance(value, list):
        return "".join(str(v) for v in value).strip()
    return str(value or "").strip()


def genie_content(payload: dict) -> dict:
    """The instructions, sample questions and benchmarks a captured agent already has."""
    instructions = [_text(item.get("content")) for item in
                    ((payload.get("instructions") or {}).get("text_instructions") or []) if isinstance(item, dict)]
    questions = [_text(item.get("question")) for item in
                 ((payload.get("config") or {}).get("sample_questions") or []) if isinstance(item, dict)]
    benchmarks = [_text(item.get("question")) for item in
                  ((payload.get("benchmarks") or {}).get("questions") or []) if isinstance(item, dict)]
    return {"instructions": [t for t in instructions if t], "sample_questions": [q for q in questions if q],
            "benchmarks": [b for b in benchmarks if b]}


def _table_columns(env_dir: Path, tables: list[str]) -> dict[str, dict[str, str]]:
    """Column names and types of the agent's tables, from the DDL generate fetched (if any)."""
    from governance_policies import column_types_from_ddl

    ddl = env_dir / "ddl" / "_fetched.sql"
    types = column_types_from_ddl(read_file(env_dir, ddl, under_drafts=False)) if ddl.exists() or ddl.is_symlink() else {}
    result = {}
    for table in tables:
        prefix = table.lower() + "."
        result[table] = {key[len(prefix):]: value for key, value in sorted(types.items()) if key.startswith(prefix)}
    return result


def _strings(reply: dict, key: str) -> list[str]:
    value = reply.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise Refused(f"the model's {key} is not a non-empty list of text; nothing was written. Re-run to retry.")
    return [v.strip() for v in value[:MAX_ITEMS]]


def _benchmarks(reply: dict) -> list[dict]:
    value = reply.get("benchmarks")
    if not isinstance(value, list) or not value:
        raise Refused("the model gave no benchmarks; nothing was written. Re-run to retry.")
    result = []
    for item in value[:MAX_ITEMS]:
        question = item.get("question") if isinstance(item, dict) else None
        sql = item.get("sql") if isinstance(item, dict) else None
        if not (isinstance(question, str) and question.strip() and isinstance(sql, str)
                and re.match(r"\s*(select|with)\b", sql, flags=re.IGNORECASE) and ";" not in sql.strip().rstrip(";")):
            raise Refused("a drafted benchmark is not {question, sql} with one SELECT query; nothing was written. "
                          "Re-run to retry.")
        result.append({"question": question.strip(), "sql": sql.strip().rstrip(";").strip()})
    return result


def draft_genie_content(env_dir: Path, space: str, client: ModelClient, force: bool = False) -> Path:
    import agent_files

    try:
        capture = agent_files.capture_path(env_dir, space)
    except agent_files.AgentFileError as exc:
        raise Refused(str(exc)) from exc
    if not capture.exists() and not capture.is_symlink():
        raise Refused(f"agent {space!r} has no capture ({_rel(env_dir, capture)}); run make capture "
                      f'ENV={env_dir.name} SPACE="{space}" first.')
    try:
        payload = json.loads(read_file(env_dir, capture, under_drafts=False))
    except json.JSONDecodeError as exc:
        raise Refused(f"{_rel(env_dir, capture)} is not valid JSON: {exc}") from exc
    payload = payload if isinstance(payload, dict) else {}
    have = genie_content(payload)
    missing = [part for part, items in have.items() if not items]
    if not missing:
        raise Refused(f"agent {space!r} already has instructions, sample questions and benchmarks; "
                      "nothing to draft (edit them in the dev Genie UI).")
    tables = sorted({str(t.get("identifier")) for t in ((payload.get("data_sources") or {}).get("tables") or [])
                     if isinstance(t, dict) and t.get("identifier")})
    if not tables:
        raise Refused(f"agent {space!r} has no tables in its capture; add them in the dev Genie UI and re-capture.")
    shapes = {"instructions": '"instructions": ["<one instruction per item>"]',
              "sample_questions": '"sample_questions": ["<a question a business user would ask>"]',
              "benchmarks": '"benchmarks": [{"question": "<question>", "sql": "<one SELECT over these tables>"}]'}
    # Schema only: table names and column types, plus which parts exist. The
    # agent's name and its existing text (instructions, questions, benchmark
    # SQL) are never sent; they can hold sample values or secrets.
    present = sorted(set(have) - set(missing))
    prompt = (
        "Draft Genie agent content for an agent that reads only these Unity Catalog tables "
        f"(columns and types): {json.dumps(_table_columns(env_dir, tables), sort_keys=True)}\n"
        f"Parts it already has (their text is not shown): {json.dumps(present)}\n"
        f"Draft only {', '.join(missing)}, at most {MAX_ITEMS} items each. Never ask for or reveal "
        "individual personal data. Reply as {" + ", ".join(shapes[p] for p in missing) + "}.")
    reply = reply_json(client.complete(SYSTEM_PROMPT, prompt))
    drafted = {part: (_benchmarks(reply) if part == "benchmarks" else _strings(reply, part)) for part in missing}
    draft = {
        "agent": space, "tables": tables, "drafted": drafted,
        "not_drafted": present,
        "to_adopt": (f"add what you keep to {space!r} in the dev Genie UI, then make capture ENV={env_dir.name} "
                     f'SPACE="{space}" and open a PR. This draft never changes the agent or its files.'),
        "drafted_by": _drafted_by("draft-genie-content", client),
    }
    path = env_dir / DRAFTS_DIR / "genie" / f"{agent_files.check_name(space)}.json"
    write_drafts(env_dir, {path: render(draft)}, force)
    return path


# ── Custom masks ────────────────────────────────────────────────────────────

def _mask_name(value: str) -> str:
    name = (value or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise Refused(f"TREATMENT={value!r} is not a custom mask name (lowercase letters, digits and '_', "
                      "starting with a letter)")
    if name in load_library()["treatments"]:
        raise Refused(f"TREATMENT={name} is a mask-library treatment; a custom mask needs a new name.")
    return name


def _mask_type(value: str) -> str:
    try:
        sql_type = normalize_sql_type(value)
    except DeterministicGovernanceError as exc:
        raise Refused(f"TYPE={value!r}: {exc}") from exc
    if type_family(sql_type) not in MASKABLE_FAMILIES:
        raise Refused(f"TYPE={sql_type} only ever gets the full (redacted or NULL) version; nothing to draft. "
                      "Types: STRING, numeric, DATE, TIMESTAMP, TIMESTAMP_NTZ.")
    return sql_type


def mask_paths(env_dir: Path, name: str) -> tuple[Path, Path, Path]:
    folder = env_dir / DRAFTS_DIR / "custom_masks"
    return folder / f"{name}.sql", folder / f"{name}.json", folder / f"{name}.passed.json"


def _strip_sql(body: str) -> str:
    """The expression's tokens without comments and string literals, for the token rules.

    It uses the same fail-closed tokenizer as generation, so a '--' or '/*'
    inside a literal can't hide what follows it. SQL that can't be tokenized
    (an open literal or comment) keeps a ';', which every rule refuses.
    """
    from sql_tokenizer import SqlTokenizeError, sql_tokens

    try:
        tokens = sql_tokens(body)
    except SqlTokenizeError:
        return ";"
    return " ".join("''" if token[:1] in {"'", '"', "$"} else token.lower() for token in tokens)


def mask_expression(sql_text: str) -> str:
    """The mask expression in a draft .sql file (comment lines dropped)."""
    return "\n".join(line for line in sql_text.splitlines() if not line.lstrip().startswith("--")).strip()


def expression_problems(expression: str) -> list[str]:
    """The mask library's rules for a mask body, checked offline."""
    if not expression.strip():
        return ["the mask has no expression"]
    bare = _strip_sql(expression).lower()
    problems = []
    if ";" in bare:
        problems.append("the mask must be one expression (no ';', and every literal and comment closed)")
    for word in STATEMENT_WORDS:
        if re.search(rf"\b{word}\b", bare):
            problems.append(f"the mask must be an expression over value, not a query or statement ({word.upper()})")
    if re.search(r"\bai_\w*\s*\(", bare):
        problems.append("the mask must not call a model: ai_* functions are not allowed")
    if not expression.isascii():
        problems.append("the mask must be ASCII (no look-alike letters)")
    for names, rule in ((CALLER_FUNCTIONS, "caller-independent (the tier policies decide who sees which version)"),
                        (NONDETERMINISTIC_FUNCTIONS, "deterministic"),
                        (SIDE_EFFECT_FUNCTIONS, "free of secrets, network and data access")):
        for name in names:
            # current_user and the other current_* forms are valid without parentheses.
            pattern = rf"\b{name}\b" if name.startswith(("current_", "session_")) else rf"\b{name}\s*\("
            if re.search(pattern, bare):
                problems.append(f"the mask must be {rule}: {name} is not allowed")
    if not re.search(r"\bvalue\b", bare):
        problems.append("the mask must read the column as value")
    return problems


def typed_expression(expression: str, sql_type: str) -> str:
    """The mask with the library's outer cast to exactly the column type."""
    cast = "TRY_CAST" if type_family(sql_type) == "NUMERIC" else "CAST"
    return f"{cast}(({expression}) AS {sql_type})"


def null_literal(sql_type: str) -> str:
    return f"CAST(NULL AS {sql_type})"


def _expected_ok(expected, family: str) -> bool:
    if expected is None:
        return True
    if family == "NUMERIC":
        return isinstance(expected, (int, float, Decimal, str)) and not isinstance(expected, bool) and \
            (not isinstance(expected, str) or re.fullmatch(r"-?\d+(\.\d+)?", expected) is not None)
    return isinstance(expected, str)


# A fixture input is data, never SQL: one literal from this grammar, ASCII
# only (no confusable letters), no identifiers, functions, operators,
# comments or backslash escapes. CAST(<literal> AS <scalar type>) is allowed
# so a case can be typed exactly (CAST('NaN' AS DOUBLE), BIGINT bounds).
_LITERAL_TYPE = (r"(?:STRING|TINYINT|SMALLINT|INT|INTEGER|BIGINT|FLOAT|DOUBLE|DATE|TIMESTAMP|TIMESTAMP_NTZ"
                 r"|DECIMAL\s*\(\s*\d{1,2}\s*(?:,\s*\d{1,2}\s*)?\))")
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?"
_TEXT = r"'(?:[ !#-&(-\[\]-~]|'')*'"  # printable ASCII; a quote only doubled; no backslash
_DATE = r"DATE\s*'\d{4}-\d{2}-\d{2}'"
_TIMESTAMP = r"TIMESTAMP(?:_NTZ)?\s*'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?'"
_SCALAR = rf"(?:NULL|{_NUMBER}|{_TEXT}|{_DATE}|{_TIMESTAMP})"
TYPED_LITERAL = re.compile(rf"\s*(?:{_SCALAR}|CAST\s*\(\s*(?:NULL|{_NUMBER}|{_TEXT})\s+AS\s+{_LITERAL_TYPE}\s*\))\s*",
                           re.IGNORECASE | re.ASCII)


def typed_literal(text) -> str | None:
    """text stripped, if it is exactly one typed SQL literal; else None."""
    if not isinstance(text, str) or not text.isascii() or not TYPED_LITERAL.fullmatch(text):
        return None
    return text.strip()


def fixture_problems(fixture, name: str, sql_type: str) -> list[str]:
    if not isinstance(fixture, dict):
        return ["the fixture must be a JSON object"]
    problems = []
    if fixture.get("treatment") != name:
        problems.append(f"the fixture's treatment must be {name}")
    if fixture.get("type") != sql_type:
        problems.append(f"the fixture's type must be {sql_type}")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        return problems + ["the fixture needs cases: [{input, expected}]"]
    family = type_family(sql_type)
    for i, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("input"), str) or "expected" not in case:
            problems.append(f"case {i}: needs input (a SQL literal) and expected")
            continue
        if typed_literal(case["input"]) is None:
            problems.append(f"case {i}: input {case['input']!r} must be one typed SQL literal (NULL, a number, "
                            "'text', DATE'...', TIMESTAMP'...', TIMESTAMP_NTZ'...', or CAST of one of those AS a "
                            "scalar type); fixtures never run functions, identifiers or queries")
        if not _expected_ok(case["expected"], family):
            problems.append(f"case {i}: expected {case['expected']!r} is not a {family} value or null")
    if not any(isinstance(c, dict) and re.sub(r"\s+", " ", str(c.get("input", "")).strip()).upper()
               == null_literal(sql_type).upper() and c.get("expected") is None for c in cases):
        problems.append(f"the fixture must check NULL in gives NULL out: input {null_literal(sql_type)}, expected null")
    if sum(1 for c in cases if isinstance(c, dict) and c.get("expected") is not None) < 2:
        problems.append("the fixture needs at least two cases with a non-null expected value")
    return problems


def _mask_sql(name: str, sql_type: str, description: str, expression: str) -> str:
    return (
        f"-- Draft custom mask {name} for {sql_type} columns (make draft-custom-mask; a model wrote it).\n"
        f"-- {description}\n"
        "-- One caller-independent expression over `value`. GenieRails never reads drafts/;\n"
        f"-- make test-custom-mask ENV=<env> TREATMENT={name} must pass before anyone adopts it.\n"
        f"{expression.strip()}\n"
    )


def draft_custom_mask(env_dir: Path, name: str, sql_type: str, client: ModelClient, force: bool = False) -> tuple[Path, Path]:
    name, sql_type = _mask_name(name), _mask_type(sql_type)
    family = type_family(sql_type)
    from scripts.live_mask_library import RENDER_ZONE

    prompt = (
        f"Draft the partial version of a column mask named {name} for {sql_type} columns, as one Databricks SQL "
        "expression over the column, which is called value. Rules: NULL in gives NULL out; malformed input gives "
        f"the full version ({'the text [REDACTED]' if family == 'STRING' else 'NULL'}); numbers round half away from "
        "zero; never raise an error (use try_cast, try_divide); no current_user, group checks, randomness, "
        "current time, queries or statements.\n"
        "Also give exact-output test cases: each input is one SQL literal of that type, each expected is the JSON "
        f"value to_json renders (timestamps in {RENDER_ZONE}). Include at least two ordinary and one malformed case.\n"
        'Reply as {"description": "<one line>", "sql": "<expression>", "cases": [{"input": "<SQL literal>", '
        '"expected": <JSON value or null>, "note": "<why>"}]}.')
    reply = reply_json(client.complete(SYSTEM_PROMPT, prompt))
    expression = reply.get("sql") if isinstance(reply.get("sql"), str) else ""
    description = " ".join(str(reply.get("description") or f"Partial version of {name}").split())
    cases = reply.get("cases") if isinstance(reply.get("cases"), list) else []
    if not any(isinstance(c, dict) and str(c.get("input", "")).strip().upper() == null_literal(sql_type).upper()
               for c in cases):
        cases = [{"input": null_literal(sql_type), "expected": None, "note": "NULL in gives NULL out"}] + cases
    fixture = {
        "treatment": name, "type": sql_type, "cases": cases, "rendered_in": RENDER_ZONE,
        "drafted_by": _drafted_by("draft-custom-mask", client),
        "review": "check every expected value yourself: these are the test's answers",
    }
    problems = expression_problems(expression) + fixture_problems(fixture, name, sql_type)
    if problems:
        raise Refused("the model's draft breaks the mask library's rules; nothing was written:\n  - "
                      + "\n  - ".join(problems) + "\nRe-run to retry.")
    sql_path, json_path, passed = mask_paths(env_dir, name)
    write_drafts(env_dir, {sql_path: _mask_sql(name, sql_type, description, expression), json_path: render(fixture)}, force)
    remove_draft(env_dir, passed)  # a new draft has not passed
    return sql_path, json_path


def _warehouse(env_dir: Path):
    """The dev workspace client and SQL warehouse ID for the live cases."""
    cfg = _env_cfg(env_dir)
    warehouse_id = str(cfg.get("sql_warehouse_id") or "").strip()
    if not warehouse_id:
        raise Refused(f"make test-custom-mask runs the fixture on a SQL warehouse; set sql_warehouse_id in "
                      f"envs/{env_dir.name}/env.auto.tfvars. There is no offline SQL engine, so it can't pass without one.")
    return workspace_client(env_dir / "auth.auto.tfvars"), warehouse_id


def check_custom_mask(env_dir: Path, name: str, warehouse: tuple | None = None) -> dict:
    """Exact-output checks for one draft; on a pass, records the digests it tested.

    Only the reviewed mask expression is SQL. Every fixture input must be one
    typed literal (typed_literal), checked offline and again right before its
    statement is built, so a fixture can never run a function or query.
    """
    from scripts.live_mask_library import TO_JSON_OPTIONS, _execute, comparable

    name = _mask_name(name)
    sql_path, json_path, passed = mask_paths(env_dir, name)
    for path in (sql_path, json_path):
        if not path.exists() and not path.is_symlink():
            raise Refused(f"{_rel(env_dir, path)} does not exist; run make draft-custom-mask ENV={env_dir.name} "
                          f"TREATMENT={name} TYPE=<sql-type> first.")
    sql_text, json_text = read_file(env_dir, sql_path), read_file(env_dir, json_path)
    remove_draft(env_dir, passed)  # only a run that passes now leaves a record
    try:
        fixture = json.loads(json_text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise Refused(f"{_rel(env_dir, json_path)} is not valid JSON: {exc}") from exc
    sql_type = _mask_type(str(fixture.get("type") or "")) if isinstance(fixture, dict) else "STRING"
    expression = mask_expression(sql_text)
    problems = expression_problems(expression) + fixture_problems(fixture, name, sql_type)
    if problems:
        return {"passed": False, "cases": 0, "problems": problems, "mismatches": []}
    client, warehouse_id = warehouse or _warehouse(env_dir)
    typed = typed_expression(expression, sql_type)
    mismatches = []
    for case in fixture["cases"]:
        literal = typed_literal(case["input"])
        if literal is None:  # unreachable after fixture_problems; never build a statement from it
            raise Refused(f"fixture input {case['input']!r} is not one typed SQL literal")
        statement = (f"SELECT to_json(named_struct('v', {typed}), {TO_JSON_OPTIONS}) "
                     f"FROM (SELECT {literal} AS value)")
        try:
            rows = _execute(client, warehouse_id, statement)
            actual = json.loads(rows[0][0], parse_float=Decimal).get("v") if rows else "<no row>"
        except Exception as exc:  # a mask that errors fails, like one that returns the wrong value
            actual = f"<error: {exc}>"
        if comparable(actual) != comparable(case["expected"]):
            mismatches.append({"input": literal, "expected": case["expected"], "actual": actual})
    result = {"passed": not mismatches, "cases": len(fixture["cases"]), "problems": [], "mismatches": mismatches}
    if not mismatches:
        write_drafts(env_dir, {passed: render({
            "treatment": name, "type": sql_type, "cases": len(fixture["cases"]),
            "sql_sha256": _digest(sql_text), "fixture_sha256": _digest(json_text),
            "checked": "mask library exact-output rules: one caller-independent, deterministic expression; "
                       "NULL in gives NULL out; every fixture input one typed literal; every case exact on the "
                       "dev SQL warehouse",
        })}, force=True)
    return result


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def passed_record(env_dir: Path, name: str) -> dict | None:
    """The pass record, only if it matches the draft's current bytes."""
    sql_path, json_path, passed = mask_paths(env_dir, name)
    try:
        record = json.loads(read_file(env_dir, passed))
        return record if record.get("sql_sha256") == _digest(read_file(env_dir, sql_path)) and \
            record.get("fixture_sha256") == _digest(read_file(env_dir, json_path)) else None
    except (OSError, ValueError, AttributeError, Refused):
        return None


# ── CLI ─────────────────────────────────────────────────────────────────────

def _client(env_dir: Path, command: str) -> ModelClient:
    return DatabricksModel(endpoint(env_dir, command), env_dir / "auth.auto.tfvars")


def run(args, client_factory: Callable[[Path, str], ModelClient] = _client) -> int:
    command = args.command
    refuse_ci(command)
    env_dir = helper_env(args.cloud_dir, args.envs_dir, args.env, args.env_dir, command)
    if command == "test-custom-mask":
        result = check_custom_mask(env_dir, args.treatment)
        name = _mask_name(args.treatment)
        for problem in result["problems"]:
            print(f"  FAIL {problem}", file=sys.stderr)
        for miss in result["mismatches"]:
            print(f"  FAIL input {miss['input']}: expected {miss['expected']!r}, got {miss['actual']!r}", file=sys.stderr)
        if not result["passed"]:
            print(f"test-custom-mask: {name} FAILED; it can't be adopted until this passes.", file=sys.stderr)
            return 1
        print(f"test-custom-mask: {name} PASSED ({result['cases']} exact-output cases). Recorded in "
              f"{_rel(env_dir, mask_paths(env_dir, name)[2])}; commit it with the draft for review.")
        return 0
    if command == "suggest-treatment" and not (args.class_name or "").strip():
        raise Refused("CLASS=<class> is required, e.g. make suggest-treatment ENV=dev CLASS=loyalty_id")
    if command == "draft-genie-content" and not (args.space or "").strip():
        raise Refused('SPACE="<agent>" is required')
    if command == "draft-custom-mask":
        if not (args.treatment or "").strip() or not (args.type or "").strip():
            raise Refused("TREATMENT=<name> and TYPE=<sql-type> are required, e.g. make draft-custom-mask "
                          "ENV=dev TREATMENT=postcode_area TYPE=STRING")
        _mask_name(args.treatment), _mask_type(args.type)  # refuse before calling the model
    client = client_factory(env_dir, command)
    if command == "suggest-treatment":
        written = [suggest_treatment(env_dir, args.class_name, client, args.force)]
    elif command == "draft-genie-content":
        written = [draft_genie_content(env_dir, args.space, client, args.force)]
    else:
        written = list(draft_custom_mask(env_dir, args.treatment, args.type, client, args.force))
    for path in written:
        print(f"DRAFTED {_rel(env_dir, path)}")
    print(f"  Review it and commit it like any change. GenieRails never reads drafts/; see to_adopt in the draft"
          + (f", then make test-custom-mask ENV={env_dir.name} TREATMENT={args.treatment}." if command == "draft-custom-mask" else "."))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["suggest-treatment", "draft-genie-content", "draft-custom-mask",
                                            "test-custom-mask"])
    parser.add_argument("--env", required=True)
    parser.add_argument("--env-dir", type=Path, required=True)
    parser.add_argument("--envs-dir", type=Path, required=True)
    parser.add_argument("--cloud-dir", default="", help="GENIERAILS_CLOUD_DIR: the dir the cloud Makefile is in")
    parser.add_argument("--class", dest="class_name", default="")
    parser.add_argument("--space", default="")
    parser.add_argument("--treatment", default="")
    parser.add_argument("--type", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Refused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
