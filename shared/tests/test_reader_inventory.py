import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import reader_inventory as ri


def _owners(*tables):
    result = {}
    for table in tables:
        catalog, schema, _ = table.split(".")
        result[("CATALOG", catalog)] = f"{catalog}-owner"
        result[("SCHEMA", f"{catalog}.{schema}")] = f"{catalog}-{schema}-owner"
        result[("TABLE", table)] = f"{table}-owner"
    return result


def _source(**kwargs):
    kwargs.setdefault("owners", _owners("c.s.t"))
    kwargs.setdefault("admins", {"meta-owner"})
    kwargs.setdefault("deployer", "c-owner")  # catalog owner sees every grant below it
    return ri.FakeSource(**kwargs)


def _principals(items):
    return {item.principal for item in items}


def test_table_schema_catalog_select_and_both_all_privilege_spellings_are_readers():
    source = _source(grant_rows=[
        ri.Grant("TABLE", "c.s.t", "table-reader", "SELECT"),
        ri.Grant("SCHEMA", "c.s", "schema-reader", "ALL PRIVILEGES"),
        ri.Grant("CATALOG", "c", "catalog-reader", "ALL_PRIVILEGES"),
        ri.Grant("TABLE", "c.s.t", "ignored", "MODIFY"),
    ])
    readers = _principals(ri.inventory_readers(source, "C.S.T").readers)
    assert {"table-reader", "schema-reader", "catalog-reader"} <= readers


def test_nested_groups_expand_transitively_and_cycles_terminate():
    source = _source(
        grant_rows=[ri.Grant("TABLE", "c.s.t", "outer", "SELECT")],
        groups={"outer": {"inner", "direct-user"}, "inner": {"leaf", "outer"}},
    )
    assert {"outer", "inner", "direct-user", "leaf"} <= _principals(
        ri.inventory_readers(source, "c.s.t").readers
    )


def test_views_of_views_inherited_grants_and_all_owners_are_readers_and_granters():
    owners = _owners("c.s.t", "vcat.vs.v1", "other.os.v2")
    source = ri.FakeSource(
        grant_rows=[ri.Grant("SCHEMA", "vcat.vs", "inherited", "SELECT")],
        owners=owners,
        dependencies={"c.s.t": {"vcat.vs.v1"}, "vcat.vs.v1": {"other.os.v2"}},
        admins={"deployer"}, deployer="deployer",
    )
    report = ri.inventory_readers(source, "c.s.t")
    assert report.dependent_views == ("other.os.v2", "vcat.vs.v1")
    expected = {
        "vcat.vs.v1-owner", "vcat-vs-owner", "vcat-owner",
        "other.os.v2-owner", "other-os-owner", "other-owner",
    }
    assert expected <= _principals(report.readers)
    assert expected <= _principals(report.granters)


def test_exact_genierails_grant_excluded_but_other_path_remains():
    managed = ri.Grant("TABLE", "c.s.t", "p", "SELECT")
    source = _source(grant_rows=[managed, ri.Grant("SCHEMA", "c.s", "p", "SELECT")])
    evidence = next(x for x in ri.inventory_readers(source, "c.s.t", [managed]).readers if x.principal == "p")
    assert evidence.reasons == ("SELECT on SCHEMA c.s",)


def test_manage_is_granter_not_reader_and_metastore_owner_is_both():
    source = _source(
        grant_rows=[ri.Grant("TABLE", "c.s.t", "manager", "MANAGE")], admins={"meta-owner"}
    )
    report = ri.inventory_readers(source, "c.s.t")
    assert "manager" not in _principals(report.readers)
    assert {"manager", "meta-owner"} <= _principals(report.granters)
    assert "meta-owner" in _principals(report.readers)


def test_empty_dependencies_and_groups_are_valid_when_table_is_visible():
    report = ri.inventory_readers(_source(dependencies={}, groups={}), "c.s.t")
    assert report.dependent_views == () and report.group_closure == {}


def test_missing_table_owner_fails_closed():
    with pytest.raises(RuntimeError, match="does not exist, is not visible"):
        ri.inventory_readers(ri.FakeSource(), "c.s.t")


def test_missing_parent_or_view_owner_fails_closed():
    source = ri.FakeSource(owners={("TABLE", "c.s.t"): "owner"}, admins={"a"}, deployer="a")
    with pytest.raises(RuntimeError, match="cannot determine owner"):
        ri.inventory_readers(source, "c.s.t")


def test_reader_check_allows_tier_transitive_members_exemptions_and_ack():
    source = _source(
        grant_rows=[
            ri.Grant("TABLE", "c.s.t", "tier", "SELECT"),
            ri.Grant("TABLE", "c.s.t", "exempt", "SELECT"),
            ri.Grant("TABLE", "c.s.t", "external", "SELECT"),
        ],
        groups={"tier": {"tier-user"}},
    )
    inventory = ri.inventory_readers(source, "c.s.t")
    key = "prod:reader:c.s.t:external"
    owner_tiers = ["c-owner", "c-s-owner", "c.s.t-owner", "meta-owner"]
    check = ri.check_readers(inventory, "prod", ["tier", *owner_tiers], ["exempt"], {key})
    assert check.ok and key in check.consequences
    assert {"tier", "tier-user", "exempt"} <= set(check.authorized)


def test_reader_check_refuses_uncovered_reader():
    inventory = ri.inventory_readers(
        _source(grant_rows=[ri.Grant("TABLE", "c.s.t", "external", "SELECT")]), "c.s.t"
    )
    check = ri.check_readers(inventory, "prod", [], [], set())
    assert not check.ok
    assert any(item.principal == "external" for item in check.unacknowledged)


def _statement(state="SUCCEEDED", rows=None, chunks=1, error=None):
    return SimpleNamespace(
        status=SimpleNamespace(state=state, error=error), statement_id="id",
        result=SimpleNamespace(data_array=rows or []),
        manifest=SimpleNamespace(total_chunk_count=chunks),
    )


def test_databricks_source_uses_statement_api_and_pages_results():
    first = _statement(rows=[["p", "SELECT"]], chunks=2)
    execution = SimpleNamespace(
        execute_statement=lambda **kwargs: first,
        get_statement_result_chunk_n=lambda *args: SimpleNamespace(data_array=[["q", "ALL_PRIVILEGES"]]),
    )
    grants = list(ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution)).grants("TABLE", "c.s.t"))
    assert [(g.principal, g.normalized().privilege) for g in grants] == [("p", "SELECT"), ("q", "ALL PRIVILEGES")]


def test_failed_query_raises_instead_of_returning_empty():
    execution = SimpleNamespace(execute_statement=lambda **kwargs: _statement("FAILED", error="denied"))
    source = ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution))
    with pytest.raises(RuntimeError, match="FAILED.*denied"):
        list(source.grants("TABLE", "c.s.t"))


def test_query_timeout_raises_instead_of_returning_empty(monkeypatch):
    running = _statement("RUNNING")
    execution = SimpleNamespace(
        execute_statement=lambda **kwargs: running,
        get_statement=lambda *_: running,
        cancel_execution=lambda *_: None,
    )
    source = ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution), timeout_seconds=0)
    with pytest.raises(TimeoutError, match="timed out"):
        source._query("SELECT 1")


def test_owner_empty_result_returns_none_and_inventory_refuses_it():
    execution = SimpleNamespace(execute_statement=lambda **kwargs: _statement(rows=[]))
    source = ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution))
    assert source.owner("TABLE", "c.s.t") is None
    with pytest.raises(RuntimeError, match="does not exist, is not visible"):
        ri.inventory_readers(source, "c.s.t")


def test_catalog_grants_query_filters_catalog_name():
    seen = {}
    execution = SimpleNamespace(execute_statement=lambda **kwargs: (seen.update(kwargs) or _statement()))
    source = ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution))
    list(source.grants("CATALOG", "Sales"))
    assert "catalog_privileges" in seen["statement"]
    assert "catalog_name = 'Sales'" in seen["statement"]


def test_adapter_empty_dependent_views_is_a_valid_empty_result():
    execution = SimpleNamespace(execute_statement=lambda **kwargs: _statement(rows=[]))
    source = ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution))
    assert list(source.dependent_views("c.s.t")) == []


def test_adapter_empty_group_list_is_a_valid_empty_mapping():
    client = SimpleNamespace(groups=SimpleNamespace(list=lambda **kwargs: []))
    source = ri.DatabricksSource("wh", client)
    assert source.group_members() == {}


def test_metastore_owner_is_resolved_from_api():
    metastores = SimpleNamespace(
        current=lambda: SimpleNamespace(metastore_id="m1"),
        get=lambda value: SimpleNamespace(owner="governance-admins"),
    )
    source = ri.DatabricksSource("wh", SimpleNamespace(metastores=metastores))
    assert tuple(source.metastore_admins()) == ("governance-admins",)


def test_metastore_without_visible_owner_fails_closed():
    metastores = SimpleNamespace(
        current=lambda: SimpleNamespace(metastore_id="m1"),
        get=lambda value: SimpleNamespace(owner=None),
    )
    source = ri.DatabricksSource("wh", SimpleNamespace(metastores=metastores))
    with pytest.raises(RuntimeError, match="no visible owner"):
        tuple(source.metastore_admins())


def test_cli_error_path_is_nonzero(monkeypatch, capsys, tmp_path):
    ack = tmp_path / "ack.txt"; ack.write_text("")
    monkeypatch.setattr(ri, "DatabricksSource", lambda *_: (_ for _ in ()).throw(RuntimeError("denied")))
    assert ri.main(["c.s.t", "--warehouse-id", "w", "--env", "prod", "--ack-file", str(ack)]) == 2
    assert "reader inventory failed: denied" in capsys.readouterr().err


def test_deployer_without_authority_fails_instead_of_partial_inventory():
    source = _source(deployer="nobody")
    with pytest.raises(RuntimeError, match="deployer nobody cannot see every grant on CATALOG c"):
        ri.inventory_readers(source, "c.s.t")


@pytest.mark.parametrize("kwargs", [
    {"deployer": "c-s-owner"},  # schema owner sees the schema and the table, not the catalog
    {"deployer": "c.s.t-owner"},
])
def test_owner_of_a_child_only_cannot_see_parent_grants(kwargs):
    with pytest.raises(RuntimeError, match="cannot see every grant on CATALOG c"):
        ri.inventory_readers(_source(**kwargs), "c.s.t")


def test_deployer_authority_via_manage_or_group_or_admin_group():
    manage = [ri.Grant("CATALOG", "c", "ops", "MANAGE")]
    ri.inventory_readers(_source(deployer="sp", grant_rows=manage, groups={"ops": {"sp"}}), "c.s.t")
    ri.inventory_readers(_source(deployer="u", admins={"admins"}, groups={"admins": {"nested"}, "nested": {"u"}}), "c.s.t")


def test_deployer_must_also_see_dependent_view_parents():
    owners = _owners("c.s.t", "v.vs.view")
    source = ri.FakeSource(owners=owners, dependencies={"c.s.t": {"v.vs.view"}}, admins={"meta"}, deployer="c-owner")
    with pytest.raises(RuntimeError, match="CATALOG v"):
        ri.inventory_readers(source, "c.s.t")


def test_missing_deployer_or_metastore_admin_fails_closed():
    with pytest.raises(RuntimeError, match="deployer principal"):
        ri.inventory_readers(_source(deployer=None), "c.s.t")
    with pytest.raises(RuntimeError, match="metastore admin"):
        ri.inventory_readers(_source(admins=set()), "c.s.t")


def test_metastore_admin_group_members_are_readers():
    report = ri.inventory_readers(_source(admins={"admins"}, groups={"admins": {"alice@example.com"}}), "c.s.t")
    assert {"admins", "alice@example.com"} <= _principals(report.readers)


def _member(kind, value):
    return SimpleNamespace(ref=f"{kind}/{value}", value=value)


def test_adapter_resolves_users_sps_and_nested_groups_by_name():
    client = SimpleNamespace(
        groups=SimpleNamespace(
            list=lambda **kwargs: [
                SimpleNamespace(id="g1", display_name="analysts", members=[
                    _member("Users", "u1"), _member("ServicePrincipals", "s1"), _member("Groups", "g2"),
                ]),
                SimpleNamespace(id="g2", display_name="inner", members=[_member("Users", "u1")]),
            ],
            get=lambda value: SimpleNamespace(display_name="inner"),
        ),
        users=SimpleNamespace(get=lambda value: SimpleNamespace(user_name="Alice@Example.com")),
        service_principals=SimpleNamespace(get=lambda value: SimpleNamespace(application_id="app-1")),
    )
    groups = ri.DatabricksSource("wh", client).group_members()
    assert groups == {"analysts": {"Alice@Example.com", "app-1", "inner"}, "inner": {"Alice@Example.com"}}


@pytest.mark.parametrize("member, message", [
    (SimpleNamespace(ref="Devices/1", value="1"), "unknown type"),
    (_member("Users", "u1"), "cannot resolve Users member"),
])
def test_adapter_unresolvable_group_member_fails_closed(member, message):
    client = SimpleNamespace(
        groups=SimpleNamespace(list=lambda **kwargs: [SimpleNamespace(id="g", display_name="g", members=[member])]),
        users=SimpleNamespace(get=lambda value: SimpleNamespace(user_name=None)),
    )
    with pytest.raises(RuntimeError, match=message):
        ri.DatabricksSource("wh", client).group_members()


def test_adapter_current_principal_from_api():
    client = SimpleNamespace(current_user=SimpleNamespace(me=lambda: SimpleNamespace(user_name="deployer-sp")))
    assert ri.DatabricksSource("wh", client).current_principal() == "deployer-sp"


def test_adapter_table_owner_query_and_none_owner():
    seen = {}
    execution = SimpleNamespace(execute_statement=lambda **kwargs: (seen.update(kwargs) or _statement(rows=[[None]])))
    source = ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution))
    assert source.owner("TABLE", "c.s.t") is None
    assert "table_owner" in seen["statement"] and "table_name='t'" in seen["statement"]


def test_query_polls_until_success(monkeypatch):
    monkeypatch.setattr(ri.time, "sleep", lambda *_: None)
    states = iter([_statement("RUNNING"), _statement(rows=[["p", "SELECT"]])])
    execution = SimpleNamespace(
        execute_statement=lambda **kwargs: _statement("PENDING"),
        get_statement=lambda *_: next(states),
    )
    source = ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution), timeout_seconds=30)
    assert source._query("SELECT 1") == [["p", "SELECT"]]


@pytest.mark.parametrize("state", ["CANCELED", "CLOSED"])
def test_non_success_terminal_states_raise(state):
    execution = SimpleNamespace(execute_statement=lambda **kwargs: _statement(state))
    with pytest.raises(RuntimeError, match=state):
        ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution))._query("SELECT 1")


def test_timeout_cancels_statement():
    cancelled = []
    running = _statement("RUNNING")
    execution = SimpleNamespace(
        execute_statement=lambda **kwargs: running, get_statement=lambda *_: running,
        cancel_execution=cancelled.append,
    )
    with pytest.raises(TimeoutError):
        ri.DatabricksSource("wh", SimpleNamespace(statement_execution=execution), timeout_seconds=0)._query("x")
    assert cancelled == ["id"]


class _CliSource(ri.FakeSource):
    def __init__(self, *_args, **_kwargs):
        owners = {**_owners("c.s.t"), **_owners("c.s.u")}
        super().__init__(
            grant_rows=[ri.Grant("TABLE", "c.s.u", "external", "SELECT")],
            owners=owners, admins={"meta"}, deployer="meta",
        )


def test_cli_checks_every_table_and_writes_consequences(monkeypatch, capsys, tmp_path):
    ack = tmp_path / "ack.txt"; out = tmp_path / "readers.json"
    ack.write_text("")
    monkeypatch.setattr(ri, "DatabricksSource", _CliSource)
    tiers = ["--tier", "c-owner", "--tier", "c-s-owner", "--tier", "c.s.t-owner", "--tier", "c.s.u-owner", "--tier", "meta"]
    code = ri.main(["c.s.t", "c.s.u", "--warehouse-id", "w", "--env", "prod", "--ack-file", str(ack),
                    "--consequences-out", str(out), *tiers])
    assert code == 1
    assert "unacknowledged prod:reader:c.s.u:external" in capsys.readouterr().err
    assert list(json.loads(out.read_text())) == ["prod:reader:c.s.u:external"]
    ack.write_text("prod:reader:c.s.u:external\n")
    assert ri.main(["c.s.t", "c.s.u", "--warehouse-id", "w", "--env", "prod", "--ack-file", str(ack), *tiers]) == 0


def test_cli_refuses_ack_for_another_env(monkeypatch, capsys, tmp_path):
    ack = tmp_path / "ack.txt"; ack.write_text("dev:reader:c.s.u:external\n")
    monkeypatch.setattr(ri, "DatabricksSource", _CliSource)
    assert ri.main(["c.s.u", "--warehouse-id", "w", "--env", "prod", "--ack-file", str(ack)]) == 2
    assert "does not match checked env" in capsys.readouterr().err
