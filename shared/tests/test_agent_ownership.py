"""Rollout step 11: capture, per-agent files, JSON-aware remap, prod ownership, detach.

Offline throughout: the Genie API is an in-memory fake that records every
call, and the Terraform runner is a stub script.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import agent_files
from agent_files import AgentFileError
from scripts import agent_ownership as ownership
from scripts import agent_promote

SHARED = Path(__file__).resolve().parents[1]
ROOT = SHARED.parent
FIXTURE = SHARED / "tests" / "fixtures" / "genie_roundtrip" / "get_space_response.json"


def _response() -> dict:
    return json.loads(FIXTURE.read_text())


def _payload() -> dict:
    return json.loads(_response()["serialized_space"])


def _env(tmp_path: Path, name: str = "dev", env_cfg: str = "", **agents: str) -> Path:
    env_dir = tmp_path / "envs" / name
    (env_dir / "agents").mkdir(parents=True)
    (env_dir / "env.auto.tfvars").write_text(env_cfg)
    for agent, text in agents.items():
        (env_dir / "agents" / f"{agent.replace('_', ' ')}.auto.tfvars").write_text(text)
    return env_dir


class FakeGenie:
    """In-memory Genie spaces API: GET/PATCH/POST/DELETE on /api/2.0/genie/spaces."""

    def __init__(self, spaces: dict[str, dict] | None = None, reject: str = ""):
        self.spaces = copy.deepcopy(spaces or {})
        self.calls: list[tuple[str, str, dict]] = []
        self.reject = reject
        self._next = 0

    def do(self, method, path, query=None, body=None):
        self.calls.append((method, path, {"query": query, "body": body}))
        space_id = path.rstrip("/").rsplit("/", 1)[-1]
        if method == "GET":
            space = self.spaces[space_id]
            return {"space_id": space_id, "etag": "1", "serialized_space": json.dumps(space)}
        if method == "PATCH":
            sent = json.loads(body["serialized_space"])
            if self.reject and self.reject in sent:
                raise RuntimeError(f"INVALID_PARAMETER_VALUE: unknown field {self.reject}")
            self.spaces[space_id] = sent
            return {"space_id": space_id}
        if method == "POST":
            self._next += 1
            new_id = f"scratch-{self._next}"
            self.spaces[new_id] = json.loads(body["serialized_space"])
            return {"space_id": new_id}
        if method == "DELETE":
            self.spaces.pop(space_id, None)
            return {}
        raise AssertionError(method)

    def methods(self):
        return [call[0] for call in self.calls]


def _all_ids(value) -> list[str]:
    if isinstance(value, dict):
        found = [value["id"]] if isinstance(value.get("id"), str) else []
        return found + [i for item in value.values() for i in _all_ids(item)]
    if isinstance(value, list):
        return [i for item in value for i in _all_ids(item)]
    return []


# ── Loader ───────────────────────────────────────────────────────────────────

def test_loader_merges_agent_files_into_genie_spaces(tmp_path):
    env = _env(tmp_path, env_cfg='governance_mode = "deterministic"\n',
               Sales='genie_space_id = "id-1"\nacl_groups = ["analysts"]\n',
               Ops='acl_groups = []\nuc_tables = ["c.s.t"]\nsql_warehouse_id = "wh"\n')
    spaces = agent_files.merged_config(env)["genie_spaces"]
    assert spaces == [
        {"name": "Ops", "genie_space_id": "", "sql_warehouse_id": "wh", "uc_tables": ["c.s.t"],
         "acl_groups": [], "detached": False},
        {"name": "Sales", "genie_space_id": "id-1", "sql_warehouse_id": "", "uc_tables": [],
         "acl_groups": ["analysts"], "detached": False},
    ]
    # Idempotent: merging an already merged config changes nothing.
    assert agent_files.merged_config(env, agent_files.merged_config(env))["genie_spaces"] == spaces


def test_loader_takes_tables_from_the_capture(tmp_path):
    env = _env(tmp_path, Sales='genie_space_id = "id-1"\nacl_groups = ["a"]\n')
    ownership.write_payload(_payload(), agent_files.capture_path(env, "Sales"))
    assert agent_files.load(env, "Sales")["uc_tables"] == ["dev_sales.crm.accounts", "dev_sales.crm.opportunities"]


@pytest.mark.parametrize("text, message", [
    ('genie_space_id = "x"\nowner = "me"\n', "unknown setting"),
    ('detached = true\nacl_groups = ["a"]\n', "acl_groups = \\[\\]"),
    ('detached = true\ndelete = true\nacl_groups = []\n', "not both"),
    ('acl_groups = "a"\n', "list of group names"),
    ('detached = "yes"\n', "true or false"),
])
def test_loader_refuses_bad_agent_files(tmp_path, text, message):
    env = _env(tmp_path, Sales=text)
    with pytest.raises(AgentFileError, match=message):
        agent_files.merged_config(env)


def test_loader_refuses_an_agent_defined_twice_or_an_orphan_capture(tmp_path):
    env = _env(tmp_path, env_cfg='genie_spaces = [{ name = "Sales", genie_space_id = "x" }]\n',
               Sales='acl_groups = ["a"]\n')
    with pytest.raises(AgentFileError, match="keep only the agent file"):
        agent_files.merged_config(env)
    other = _env(tmp_path, name="prod")
    (other / "agents" / "Lost.space.json").write_text("{}")
    with pytest.raises(AgentFileError, match="captured config without settings for Lost"):
        agent_files.merged_config(other)


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", "", "x\"y"])
def test_agent_names_are_never_paths(name):
    with pytest.raises(AgentFileError):
        agent_files.settings_path(Path("/tmp"), name)


def test_delete_true_leaves_genie_spaces(tmp_path):
    env = _env(tmp_path, Sales='genie_space_id = "id-1"\nacl_groups = []\ndelete = true\n')
    assert agent_files.merged_config(env)["genie_spaces"] == []


def _state(env_dir: Path, *keys: str) -> None:
    resources = [{"module": "module.workspace", "mode": "managed", "type": "null_resource",
                  "name": "genie_space_acls", "instances": [{"index_key": k} for k in keys]}]
    (env_dir / "terraform.tfstate").write_text(json.dumps({"resources": resources}))


def test_terraform_spaces_keep_a_detached_agent_only_while_state_holds_it(tmp_path):
    env = _env(tmp_path, Sales='genie_space_id = "id-1"\nacl_groups = []\ndetached = true\n')
    _state(env, "sales")
    assert [s["name"] for s in agent_files.terraform_spaces(env, "workspace")] == ["Sales"]
    _state(env)
    assert agent_files.terraform_spaces(env, "workspace") == []
    # Its tables stay governed.
    assert [s["name"] for s in agent_files.terraform_spaces(env, "data_access")] == ["Sales"]


def test_terraform_spaces_mark_captured_agents_for_the_workspace_layer_only(tmp_path):
    env = _env(tmp_path, Sales='genie_space_id = "id-1"\nacl_groups = ["a"]\n', Ops='acl_groups = ["a"]\n')
    ownership.write_payload({"version": 2}, agent_files.capture_path(env, "Sales"))
    workspace = {s["name"]: s for s in agent_files.terraform_spaces(env, "workspace")}
    assert workspace["Sales"]["captured"] is True and workspace["Ops"]["captured"] is False
    assert all("captured" not in s for s in agent_files.terraform_spaces(env, "data_access"))
    assert agent_files.terraform_spaces(tmp_path / "none", "workspace") is None


def _fake_terraform(tmp_path: Path) -> tuple[dict, Path]:
    log = tmp_path / "terraform.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    terraform = bin_dir / "terraform"
    # Record the args and the agent var-file's content (it is gone after the run).
    terraform.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> \"{log}\"\n"
        "for a in \"$@\"; do case \"$a\" in -var-file=*agents.tfvars.json) "
        f"cat \"${{a#-var-file=}}\" > \"{tmp_path}/agents.json\";; esac; done\n")
    terraform.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return env, log


@pytest.mark.parametrize("layer", ["workspace", "data_access"])
def test_layer_runner_passes_the_merge_after_env_tfvars(tmp_path, layer):
    env_dir = _env(tmp_path, env_cfg="", Sales='genie_space_id = "id-1"\nacl_groups = ["a"]\n')
    layer_dir = env_dir if layer == "workspace" else env_dir / "data_access"
    layer_dir.mkdir(exist_ok=True)
    (layer_dir / "env.auto.tfvars").write_text("")
    env, log = _fake_terraform(tmp_path)
    result = subprocess.run([SHARED / "scripts" / "terraform_layer.sh", layer, "dev", "plan"],
                            text=True, capture_output=True, env={**env, "LAYER_ENV_DIR": str(layer_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    args = shlex.split(log.read_text().splitlines()[1])
    agent_arg = next(a for a in args if a.endswith("agents.tfvars.json"))
    assert args.index(f"-var-file={layer_dir / 'env.auto.tfvars'}") < args.index(agent_arg)
    assert json.loads((tmp_path / "agents.json").read_text())["genie_spaces"][0]["name"] == "Sales"
    assert not Path(agent_arg.split("=", 1)[1]).exists(), "the merged var-file is temporary"


def test_layer_runner_stops_on_a_bad_agent_file(tmp_path):
    env_dir = _env(tmp_path, Sales='bogus = 1\n')
    env, log = _fake_terraform(tmp_path)
    result = subprocess.run([SHARED / "scripts" / "terraform_layer.sh", "workspace", "dev", "plan"],
                            text=True, capture_output=True, env={**env, "LAYER_ENV_DIR": str(env_dir)})
    assert result.returncode != 0 and "unknown setting" in result.stderr
    assert all(" plan " not in f" {line} " for line in log.read_text().splitlines()[1:])


def test_validate_and_table_readers_read_the_same_merge(tmp_path):
    from table_readers import agents

    env = _env(tmp_path, env_cfg='governance_mode = "deterministic"\n', Sales='acl_groups = ["a"]\nbogus = 1\n')
    result = subprocess.run(["python3", SHARED / "scripts" / "validate_env_config.py", env / "env.auto.tfvars"],
                            text=True, capture_output=True)
    assert result.returncode == 1 and "unknown setting" in result.stderr
    (env / "agents" / "Sales.auto.tfvars").write_text('acl_groups = ["a"]\nuc_tables = ["c.s.t"]\n')
    assert agents(env) == [{"name": "Sales", "acl_groups": ["a"], "tables": ["c.s.t"]}]


# ── Capture ──────────────────────────────────────────────────────────────────

def test_capture_strips_outer_fields_and_keeps_every_inner_id(tmp_path):
    env = _env(tmp_path, Sales='genie_space_id = "01f0aa11bb22cc33dd44ee55ff660001"\nacl_groups = ["a"]\n')
    calls = []
    getter = lambda path, query: calls.append((path, query)) or _response()  # noqa: E731
    before = {p.name for p in env.rglob("*")}
    assert ownership.capture(env, "dev", "Sales", getter) is True
    saved = json.loads(agent_files.capture_path(env, "Sales").read_text())
    assert saved == _payload()
    assert not {"space_id", "etag", "access_control_list", "title", "warehouse_id"} & set(saved)
    assert sorted(_all_ids(saved)) == sorted(_all_ids(_payload())) and len(_all_ids(saved)) == 10
    # Only the capture file was written; one GET, nothing else.
    assert {p.name for p in env.rglob("*")} - before == {"Sales.space.json"}
    assert calls == [("/api/2.0/genie/spaces/01f0aa11bb22cc33dd44ee55ff660001", {"include_serialized_space": "true"})]
    # Idempotent bytes.
    assert ownership.capture(env, "dev", "Sales", getter) is False


def test_capture_drops_outer_fields_even_inside_the_payload(tmp_path):
    response = {"serialized_space": json.dumps({"space_id": "x", "etag": "y", "version": 2})}
    assert ownership.payload_from_response(response) == {"version": 2}


@pytest.mark.parametrize("response, message", [
    ({"space_id": "x"}, "no serialized_space"),
    ({"serialized_space": ""}, "empty serialized_space"),
    ({"serialized_space": "not-json"}, "not valid JSON"),
    ({"serialized_space": "[1]"}, "JSON object"),
])
def test_capture_refuses_a_bad_export(response, message):
    with pytest.raises(ownership.AgentError, match=message):
        ownership.payload_from_response(response)


def test_capture_is_dev_only_and_needs_an_id(tmp_path):
    env = _env(tmp_path, Sales='acl_groups = ["a"]\n')
    with pytest.raises(ownership.AgentError, match="only with ENV=dev"):
        ownership.capture(env, "prod", "Sales", lambda *a: {})
    with pytest.raises(ownership.AgentError, match="has no genie_space_id"):
        ownership.capture(env, "dev", "Sales", lambda *a: {})


def test_capture_never_drafts_governance_calls_a_model_or_writes_to_genie():
    import inspect

    module = (SHARED / "scripts" / "agent_ownership.py").read_text()
    assert "generate_abac" not in module and "serving" not in module
    source = inspect.getsource(ownership.capture) + inspect.getsource(ownership.import_new)
    assert '"GET"' in source and not any(m in source for m in ('"PATCH"', '"POST"', '"PUT"', '"DELETE"'))


def test_generate_imports_only_agents_not_yet_captured(tmp_path):
    env = _env(tmp_path, New='genie_space_id = "new-1"\nacl_groups = ["a"]\n',
               Old='genie_space_id = "old-1"\nacl_groups = ["a"]\n', Draft='acl_groups = ["a"]\n')
    old = agent_files.capture_path(env, "Old")
    old.write_text('{"curated": "in git"}\n')
    calls = []
    getter = lambda path, query: calls.append(path) or _response()  # noqa: E731
    assert ownership.import_new(env, "dev", getter) == ["New"]
    assert calls == ["/api/2.0/genie/spaces/new-1"]
    assert old.read_text() == '{"curated": "in git"}\n', "an agent already in git is never re-captured"
    assert ownership.import_new(env, "dev", getter) == [] and len(calls) == 1
    assert ownership.import_new(env, "prod", getter) == []


# ── Remap ────────────────────────────────────────────────────────────────────

def test_remap_changes_identifiers_in_values_never_keys_or_prose():
    source = {
        "dev_sales.key": "dev_sales.crm.accounts",
        "items": [
            "SELECT * FROM dev_sales.crm.accounts JOIN `dev_sales`.crm.o USING (id)",
            "the dev_sales team owns this agent",
            "mydev_sales.crm.t and x.dev_sales.t",
            "dev_sales",
            "warehouse a1b2c3d4e5f60718 / a1b2c3d4e5f60718x",
        ],
        "n": 3, "flag": True, "none": None,
    }
    assert agent_promote.remap_payload(source, {"dev_sales": "prod_sales"}, {"a1b2c3d4e5f60718": "ffff0000"}) == {
        "dev_sales.key": "prod_sales.crm.accounts",
        "items": [
            "SELECT * FROM prod_sales.crm.accounts JOIN `prod_sales`.crm.o USING (id)",
            "the dev_sales team owns this agent",
            "mydev_sales.crm.t and x.dev_sales.t",
            "prod_sales",
            "warehouse ffff0000 / a1b2c3d4e5f60718x",
        ],
        "n": 3, "flag": True, "none": None,
    }


def test_remap_prefers_the_longest_catalog_and_keeps_every_id():
    payload = _payload()
    mapped = agent_promote.remap_payload(payload, {"dev": "prod", "dev_sales": "prod_sales"})
    text = json.dumps(mapped)
    assert "dev_sales." not in text.replace("dev_sales team", "")
    assert "prod_sales.crm.accounts" in text and "`prod_sales`.crm.opportunities" in text
    assert "the dev_sales team owns this agent" in text  # prose
    assert _all_ids(mapped) == _all_ids(payload)
    assert agent_promote.remap_payload(mapped, {"prod_sales": "dev_sales"}) == payload


# ── promote-to ───────────────────────────────────────────────────────────────

def _promote_envs(tmp_path):
    dev = _env(tmp_path, env_cfg='sql_warehouse_id = "a1b2c3d4e5f60718"\n',
               Sales='genie_space_id = "dev-id"\nacl_groups = ["sales_analysts"]\n',
               Draft='genie_space_id = "draft-id"\nacl_groups = ["a"]\n')
    ownership.write_payload(_payload(), agent_files.capture_path(dev, "Sales"))
    prod = tmp_path / "envs" / "prod"
    prod.mkdir()
    (prod / "env.auto.tfvars").write_text('sql_warehouse_id = "prodwh0000000001"\n')
    return dev, prod


def test_promote_seeds_settings_once_and_rewrites_the_capture(tmp_path):
    dev, prod = _promote_envs(tmp_path)
    assert agent_promote.promote(dev, prod, {"dev_sales": "prod_sales"}) == ["Sales"]
    settings = prod / "agents" / "Sales.auto.tfvars"
    seeded = agent_files.load(prod, "Sales")
    assert seeded["genie_space_id"] == "" and seeded["acl_groups"] == ["sales_analysts"]
    assert "dev-id" not in settings.read_text()
    assert not (prod / "agents" / "Draft.space.json").exists(), "an uncaptured agent is not promoted"
    promoted = json.loads((prod / "agents" / "Sales.space.json").read_text())
    assert promoted == agent_promote.remap_payload(_payload(), {"dev_sales": "prod_sales"},
                                                   {"a1b2c3d4e5f60718": "prodwh0000000001"})
    # Env-owned: a prod edit survives the next promotion; the capture is rewritten.
    settings.write_text('genie_space_id = ""\nacl_groups = ["prod_sales_readers"]\n')
    (prod / "agents" / "Sales.space.json").write_text("{}")
    agent_promote.promote(dev, prod, {"dev_sales": "prod_sales"})
    assert agent_files.load(prod, "Sales")["acl_groups"] == ["prod_sales_readers"]
    assert json.loads((prod / "agents" / "Sales.space.json").read_text()) == promoted


def test_promote_is_a_pure_function_of_the_committed_files(tmp_path, monkeypatch):
    dev, prod = _promote_envs(tmp_path)
    monkeypatch.setattr(ownership, "_api", lambda *a: pytest.fail("promote-to read live Databricks"))
    (prod / "terraform.tfstate").write_text("not read")
    agent_promote.promote(dev, prod, {"dev_sales": "prod_sales"})
    first = {p.name: p.read_bytes() for p in (prod / "agents").iterdir()}
    shutil.rmtree(prod / "agents")
    agent_promote.promote(dev, prod, {"dev_sales": "prod_sales"})
    assert {p.name: p.read_bytes() for p in (prod / "agents").iterdir()} == first


def test_promote_keeps_agents_only_in_the_target(tmp_path, capsys):
    dev, prod = _promote_envs(tmp_path)
    (prod / "agents").mkdir()
    (prod / "agents" / "Legacy.auto.tfvars").write_text('acl_groups = []\n')
    agent_promote.promote(dev, prod, {})
    assert (prod / "agents" / "Legacy.auto.tfvars").is_file()
    assert "Legacy: only in prod; kept" in capsys.readouterr().out


# ── Prod ownership ───────────────────────────────────────────────────────────

def _prod(tmp_path, settings='genie_space_id = ""\nacl_groups = ["a"]\n', payload=None) -> Path:
    prod = _env(tmp_path, name="prod", Sales=settings)
    ownership.write_payload(payload or _payload(), agent_files.capture_path(prod, "Sales"))
    (prod / ".genie_space_id_sales").write_text("prod-1\n")
    return prod


def test_sync_pushes_the_full_capture_once_and_reports_each_field(tmp_path, capsys):
    prod = _prod(tmp_path)
    live = copy.deepcopy(_payload())
    live["instructions"]["text_instructions"][0]["content"] = ["edited in prod UI"]
    del live["benchmarks"]
    live["config"]["sample_questions"].pop()
    api = FakeGenie({"prod-1": live})
    reports = ownership.sync(prod, "prod", api)
    assert api.methods() == ["GET", "PATCH"]
    assert api.calls[1][1] == "/api/2.0/genie/spaces/prod-1", "never a new agent or another ID"
    assert api.spaces["prod-1"] == _payload(), "full replacement"
    assert reports["Sales"] == [
        "benchmarks (added)",
        "config.sample_questions[id=01f0sq000000000000000000000002] (added)",
        "instructions.text_instructions[id=01f0ti000000000000000000000001].content",
    ]
    out = capsys.readouterr().out
    assert "overwrote instructions.text_instructions[id=01f0ti000000000000000000000001].content" in out


def test_sync_fails_when_any_field_is_rejected_and_never_retries_without_it(tmp_path):
    prod = _prod(tmp_path)
    api = FakeGenie({"prod-1": {}}, reject="benchmarks")
    with pytest.raises(ownership.AgentError, match="rejected the captured config"):
        ownership.sync(prod, "prod", api)
    assert api.methods() == ["GET", "PATCH"]


def test_sync_refuses_an_answer_for_another_agent(tmp_path):
    prod = _prod(tmp_path)

    class Moved(FakeGenie):
        def do(self, method, path, query=None, body=None):
            result = super().do(method, path, query, body)
            return {"space_id": "other"} if method == "PATCH" else result

    with pytest.raises(ownership.AgentError, match="not prod-1"):
        ownership.sync(prod, "prod", Moved({"prod-1": {}}))


def test_dev_agent_config_is_never_overwritten(tmp_path):
    dev = _env(tmp_path, Sales='genie_space_id = "dev-1"\nacl_groups = ["a"]\n')
    ownership.write_payload(_payload(), agent_files.capture_path(dev, "Sales"))
    api = FakeGenie({"dev-1": {"curated": True}})
    assert ownership.sync(dev, "dev", api) == {}
    assert api.calls == []


def test_sync_skips_detached_agents_and_needs_an_id(tmp_path):
    prod = _prod(tmp_path, settings='acl_groups = []\ndetached = true\n')
    api = FakeGenie({"prod-1": {}})
    assert ownership.sync(prod, "prod", api) == {} and api.calls == []
    other = _prod(tmp_path / "x")
    (other / ".genie_space_id_sales").unlink()
    with pytest.raises(ownership.AgentError, match="has no ID after apply"):
        ownership.sync(other, "prod", FakeGenie())


def test_delete_true_deletes_an_agent_configured_by_id(tmp_path):
    prod = _env(tmp_path, name="prod", Old='genie_space_id = "old-1"\nacl_groups = []\ndelete = true\n')
    api = FakeGenie({"old-1": {}})
    assert ownership.delete_agents(prod, "prod", api) == ["Old"] and "old-1" not in api.spaces


def _git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "c", "--no-verify"], check=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, text=True,
                          capture_output=True).stdout.strip()


def test_receipt_outside_git_records_no_commit(tmp_path, capsys):
    prod = _prod(tmp_path)
    assert ownership.write_receipt(prod, "prod")["commit"] == ""
    assert "records no commit" in capsys.readouterr().out


def test_receipt_keeps_prod_ids_out_of_git_and_restores_lost_id_files(tmp_path):
    prod = _prod(tmp_path)
    head = _git_repo(tmp_path)
    tracked_before = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"],
                                    text=True, capture_output=True).stdout
    receipt = ownership.write_receipt(prod, "prod")
    assert receipt["commit"] == head and receipt["agents"]["Sales"]["genie_space_id"] == "prod-1"
    assert "prod-1" not in (prod / "agents" / "Sales.auto.tfvars").read_text()
    (prod / ".genie_space_id_sales").unlink()
    assert ownership.restore_ids(prod) == ["Sales"]
    assert (prod / ".genie_space_id_sales").read_text() == "prod-1\n"
    assert ownership.restore_ids(prod) == [], "an ID file in place is never overwritten"
    # Only ignored/untracked runtime files changed: no committed file was written.
    assert subprocess.run(["git", "-C", str(tmp_path), "diff", "--stat"], text=True,
                          capture_output=True).stdout == ""
    assert tracked_before == ""


# ── Detach ───────────────────────────────────────────────────────────────────

STATE_LIST = "\n".join([
    'module.workspace.null_resource.genie_space_acls_created["sales"]',
    'module.workspace.terraform_data.genie_space["sales"]',
    'module.workspace.null_resource.genie_space_config["sales"]',
    'module.workspace.terraform_data.genie_space["sales_eu"]',
    'module.workspace.null_resource.genie_space_acls["salesx"]',
    'module.data_access.databricks_grant.table["sales"]',
])


def _runner(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "runner.log"
    runner = tmp_path / "runner.sh"
    runner.write_text("#!/bin/sh\n"
                      f"printf '%s\\n' \"$*\" >> \"{log}\"\n"
                      f"if [ \"$3\" = state-list ]; then cat <<'EOF'\n{STATE_LIST}\nEOF\nfi\n")
    runner.chmod(0o755)
    return runner, log


def _released_detached(tmp_path):
    prod = _prod(tmp_path, settings='genie_space_id = ""\nacl_groups = []\ndetached = true\n')
    head = _git_repo(tmp_path)
    ownership.write_receipt(prod, "prod", commit=head)
    return prod, head


def test_detach_removes_exactly_the_agents_addresses(tmp_path):
    prod, head = _released_detached(tmp_path)
    runner, log = _runner(tmp_path)
    removed = ownership.detach(prod, "prod", "Sales", runner, commit=head, clean=True)
    assert removed == [
        'module.workspace.null_resource.genie_space_acls_created["sales"]',
        'module.workspace.terraform_data.genie_space["sales"]',
        'module.workspace.null_resource.genie_space_config["sales"]',
    ]
    calls = log.read_text().splitlines()
    assert calls[0] == "workspace prod state-list"
    assert calls[1:] == [f"workspace prod state-rm {a}" for a in removed]
    assert all("destroy" not in c and "apply" not in c for c in calls)


def test_detach_refuses_unless_the_detached_change_is_the_released_head(tmp_path):
    runner, log = _runner(tmp_path)
    attached = _prod(tmp_path / "a")
    with pytest.raises(ownership.AgentError, match="not detached"):
        ownership.detach(attached, "prod", "Sales", runner, commit="x", clean=True)
    prod, head = _released_detached(tmp_path / "b")
    with pytest.raises(ownership.AgentError, match="not the last released commit"):
        ownership.detach(prod, "prod", "Sales", runner, commit="0" * 40, clean=True)
    with pytest.raises(ownership.AgentError, match="uncommitted changes"):
        ownership.detach(prod, "prod", "Sales", runner, commit=head, clean=False)
    # Released while still attached, detached only in the checkout.
    receipt = json.loads((prod / ownership.RECEIPT).read_text())
    receipt["agents"]["Sales"].update(detached=False, acl_groups=["a"])
    (prod / ownership.RECEIPT).write_text(json.dumps(receipt))
    with pytest.raises(ownership.AgentError, match="did not have Sales detached"):
        ownership.detach(prod, "prod", "Sales", runner, commit=head, clean=True)
    (prod / ownership.RECEIPT).unlink()
    with pytest.raises(ownership.AgentError, match="no release receipt"):
        ownership.detach(prod, "prod", "Sales", runner, commit=head, clean=True)
    ownership.write_receipt(prod, "prod", commit="")
    with pytest.raises(ownership.AgentError, match="recorded no commit"):
        ownership.detach(prod, "prod", "Sales", runner, commit=head, clean=True)
    assert not log.exists(), "no state command runs before every check passed"


def test_removing_an_agent_still_in_state_is_refused(tmp_path):
    env = _env(tmp_path, name="prod", env_cfg='governance_mode = "deterministic"\n',
               Sales='acl_groups = []\ndetached = true\n')
    _state(env, "sales", "ops")
    with pytest.raises(ownership.AgentError, match="still holds them: ops"):
        ownership.guard_removals(env)
    (env / "agents" / "Ops.auto.tfvars").write_text('acl_groups = []\ndelete = true\n')
    ownership.guard_removals(env)  # delete = true is the other way out
    (env / "agents" / "Ops.auto.tfvars").unlink()
    _state(env, "sales")
    ownership.guard_removals(env)
    (env / "agents" / "Sales.auto.tfvars").unlink()
    with pytest.raises(ownership.AgentError, match="make detach-agent"):
        ownership.guard_removals(env)
    _state(env)
    ownership.guard_removals(env)  # after detach-agent: removal plans no destroy


def test_legacy_envs_keep_remove_means_destroy(tmp_path):
    env = _env(tmp_path, env_cfg="")
    shutil.rmtree(env / "agents")
    _state(env, "gone")
    ownership.guard_removals(env)


# ── Round trip ───────────────────────────────────────────────────────────────

def test_round_trip_fidelity_for_every_field_type(tmp_path):
    """Export, import into a scratch space, export again: the diff is empty."""
    source = _payload()
    # Every field type the format has is in the recorded fixture.
    assert set(source) == {"version", "config", "data_sources", "instructions", "benchmarks"}
    assert set(source["instructions"]) == {"text_instructions", "example_question_sqls", "sql_functions",
                                           "join_specs", "sql_snippets"}
    assert set(source["instructions"]["sql_snippets"]) == {"filters", "expressions", "measures"}
    assert set(source["data_sources"]) == {"tables", "metric_views"}

    api = FakeGenie({"01f0aa11bb22cc33dd44ee55ff660001": source})
    dev = _env(tmp_path, Sales='genie_space_id = "01f0aa11bb22cc33dd44ee55ff660001"\nacl_groups = ["a"]\n')
    ownership.capture(dev, "dev", "Sales", lambda path, query: api.do("GET", path, query=query))
    exported = json.loads(agent_files.capture_path(dev, "Sales").read_text())

    scratch = api.do("POST", "/api/2.0/genie/spaces", body={"serialized_space": json.dumps(exported)})["space_id"]
    prod = _env(tmp_path, name="prod", Sales='acl_groups = ["a"]\n')
    (prod / "agents" / "Sales.space.json").write_text(ownership.render(exported))
    (prod / ".genie_space_id_sales").write_text(scratch + "\n")
    assert ownership.sync(prod, "prod", api)["Sales"] == []
    again = ownership.payload_from_response(api.do("GET", f"/api/2.0/genie/spaces/{scratch}"))
    assert ownership.changed_fields(exported, again) == []
    assert ownership.render(again) == ownership.render(exported)
    api.do("DELETE", f"/api/2.0/genie/spaces/{scratch}")
    assert scratch not in api.spaces


def test_live_round_trip_script_is_gated_and_cleans_up():
    script = (SHARED / "scripts" / "genie_roundtrip_live.py").read_text()
    assert 'DATABRICKS_LIVE_TESTS' in script and "finally:" in script and '"DELETE"' in script
    result = subprocess.run(["python3", SHARED / "scripts" / "genie_roundtrip_live.py", "--space-id", "x"],
                            text=True, capture_output=True, env={k: v for k, v in os.environ.items()
                                                                 if k != "DATABRICKS_LIVE_TESTS"})
    assert result.returncode == 2 and "DATABRICKS_LIVE_TESTS=1" in result.stderr


@pytest.mark.skipif(os.environ.get("DATABRICKS_LIVE_TESTS") != "1", reason="live: set DATABRICKS_LIVE_TESTS=1")
def test_live_round_trip():  # pragma: no cover - live only
    space_id = os.environ.get("GENIE_ROUNDTRIP_SPACE_ID", "")
    if not space_id:
        pytest.skip("set GENIE_ROUNDTRIP_SPACE_ID to the agent to export")
    result = subprocess.run(["python3", SHARED / "scripts" / "genie_roundtrip_live.py", "--space-id", space_id],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


# ── Makefile ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("target", ["capture", "detach-agent"])
def test_make_dry_run_for_agent_targets(cloud, target, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "MAKEFLAGS", "MAKELEVEL")}
    result = subprocess.run(["make", "-n", "-C", str(ROOT / cloud), target, "ENV=dev", "SPACE=Sales"],
                            text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "agent_ownership.py" in result.stdout


def test_capture_target_refuses_other_envs_and_needs_space():
    env = {k: v for k, v in os.environ.items() if k not in ("MAKEFLAGS", "MAKELEVEL")}
    no_space = subprocess.run(["make", "-C", str(ROOT / "aws"), "capture", "ENV=dev"],
                              text=True, capture_output=True, env=env)
    assert no_space.returncode != 0 and 'SPACE="<agent>" is required' in no_space.stderr
    prod = subprocess.run(["make", "-C", str(ROOT / "aws"), "capture", "ENV=prod", "SPACE=Sales"],
                          text=True, capture_output=True, env=env)
    assert prod.returncode != 0 and "only with ENV=dev" in prod.stderr


def test_release_pushes_agents_after_apply_and_writes_the_receipt_last():
    makefile = (SHARED / "Makefile.shared").read_text()
    release = makefile[makefile.index("\nrelease:"):makefile.index("\nmaintain:")]
    order = [release.index(s) for s in ("restore-ids", '$(MAKE) apply ENV="$(ENV)"', "$(_AGENT_OWNERSHIP) sync",
                                        "verify-access ENV", "$(_AGENT_OWNERSHIP) receipt", "Release complete")]
    assert order == sorted(order)
    rehearse = makefile[makefile.index("\nrehearse:"):makefile.index("\n_RUN_AUDIT_RULEBOOK")]
    maintain = makefile[makefile.index("\nmaintain:"):makefile.index("\nvalidate:")]
    for recipe in (rehearse, maintain):
        assert "_AGENT_OWNERSHIP" not in recipe, "only release pushes agent config"
    generate = makefile[makefile.index("\ngenerate:"):makefile.index("\n# Agents (scripts/agent_ownership.py")]
    assert "import-new" in generate and " capture " not in generate


def test_detach_agent_is_an_operations_command_that_takes_the_env_lock():
    makefile = (SHARED / "Makefile.shared").read_text()
    recipe = makefile[makefile.index("\ndetach-agent:"):]
    recipe = recipe[:recipe.index("\n\n")]
    assert "_guard-not-ci-apply" in recipe.splitlines()[1] and "_ENV_LOCK,detach-agent" in recipe
