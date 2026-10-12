import json
from pathlib import Path

import pytest

from scripts.agent_ownership import (
    AgentError, assemble_sidecars, capture, changed_fields, guard_removals, remap_payload, sync_one,
    write_capture,
)


def _env(tmp_path: Path, name="Sales", space_id="id-1") -> Path:
    env = tmp_path / "dev"
    agents = env / "agents"
    agents.mkdir(parents=True)
    (agents / f"{name}.auto.tfvars").write_text(
        f'genie_space_id = "{space_id}"\nacl_groups = ["analysts"]\n'
    )
    return env


def test_capture_strips_outer_fields_keeps_inner_ids_and_is_idempotent(tmp_path):
    env = _env(tmp_path)
    calls = []
    response = {
        "space_id": "outer", "etag": "etag", "access_control_list": ["acl"],
        "serialized_space": json.dumps({"version": 2, "instructions": {"items": [{"id": "keep-me"}]}}),
    }
    getter = lambda path, query: calls.append((path, query)) or response
    assert capture(env, "Sales", getter) is True
    assert capture(env, "Sales", getter) is False
    saved = json.loads((env / "agents" / "Sales.space.json").read_text())
    assert saved == {"version": 2, "instructions": {"items": [{"id": "keep-me"}]}}
    assert len(calls) == 2 and all(call[0].endswith("/id-1") for call in calls)


def test_capture_uses_get_only_and_never_exposes_a_write_callback(tmp_path):
    env = _env(tmp_path)
    methods = []
    capture(env, "Sales", lambda path, query: methods.append("GET") or {"serialized_space": "{}"})
    assert methods == ["GET"]


def test_write_capture_rejects_missing_or_invalid_serialized_space(tmp_path):
    with pytest.raises(AgentError):
        write_capture({"space_id": "x"}, tmp_path / "x")
    with pytest.raises(AgentError):
        write_capture({"serialized_space": "not-json"}, tmp_path / "x")


def test_json_remap_changes_values_on_identifier_boundaries_not_keys_or_prose():
    source = {
        "dev_cat.key": "dev_cat.schema.table",
        "items": ["SELECT * FROM dev_cat.schema.table", "dev_cat is discussed", "mydev_cat.schema.t"],
        "warehouse": "wh-dev",
    }
    assert remap_payload(source, {"dev_cat": "prod_cat"}, {"wh-dev": "wh-prod"}) == {
        "dev_cat.key": "prod_cat.schema.table",
        "items": ["SELECT * FROM prod_cat.schema.table", "dev_cat is discussed", "mydev_cat.schema.t"],
        "warehouse": "wh-prod",
    }


class API:
    def __init__(self, reject=False):
        self.calls = []
        self.reject = reject

    def do(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
            return {"serialized_space": json.dumps({"version": 1, "old": True})}
        if self.reject:
            raise RuntimeError("unknown field")
        return {}


def test_prod_sync_reports_changes_and_sends_one_full_patch(tmp_path, capsys):
    env = _env(tmp_path)
    (env / "agents" / "Sales.space.json").write_text('{"version": 2, "items": [{"id": "same"}]}')
    api = API()
    report = sync_one(env, "Sales", api)
    assert report == ["items", "old", "version"]
    assert [call[0] for call in api.calls] == ["GET", "PATCH"]
    assert "overwrite version" in capsys.readouterr().out
    sent = json.loads(api.calls[1][2]["body"]["serialized_space"])
    assert sent["items"][0]["id"] == "same"


def test_prod_sync_fails_if_any_captured_field_is_rejected(tmp_path):
    env = _env(tmp_path)
    (env / "agents" / "Sales.space.json").write_text('{"rejected": true}')
    api = API(reject=True)
    with pytest.raises(AgentError, match="rejected the full captured config"):
        sync_one(env, "Sales", api)
    assert [call[0] for call in api.calls] == ["GET", "PATCH"]


def test_sidecar_loader_puts_prod_id_in_each_agents_own_file(tmp_path):
    env = _env(tmp_path)
    assert assemble_sidecars(env) == 1
    aggregate = json.loads((env / "zz_agents.auto.tfvars.json").read_text())
    assert aggregate["genie_spaces"][0]["genie_space_id"] == "id-1"


def test_generate_recipe_does_not_recapture():
    makefile = (Path(__file__).parents[1] / "Makefile.shared").read_text()
    recipe = makefile[makefile.index("generate:"):makefile.index("capture:")]
    assert "agent_ownership.py" not in recipe
    assert "serialized_space" not in recipe


def test_changed_fields_is_field_level_and_stable():
    assert changed_fields({"b": 1, "a": {"x": 1}}, {"b": 2, "a": {"x": 1, "y": 3}}) == ["a.y", "b"]


def test_removing_agent_without_detach_is_refused(tmp_path, monkeypatch):
    env = tmp_path / "prod"
    (env / "agents").mkdir(parents=True)
    result = type("Result", (), {"stdout": 'module.workspace.null_resource.genie_space_acls["Sales"]\n'})()
    monkeypatch.setattr("scripts.agent_ownership._run", lambda *args, **kwargs: result)
    with pytest.raises(AgentError, match="before detach-agent: Sales"):
        guard_removals(env, "prod", Path("runner"))
    (env / "agents" / "Sales.detached").write_text("id-1\n")
    guard_removals(env, "prod", Path("runner"))
