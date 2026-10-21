import json
import sys
from pathlib import Path

import hcl2
import pytest

from scripts import remap_env_config


def test_repromote_preserves_destination_space_acl_and_access_tiers(tmp_path, monkeypatch, capsys):
    source, dest = tmp_path / "dev", tmp_path / "prod"
    source.mkdir(); dest.mkdir()
    (source / "env.auto.tfvars").write_text('''
genie_spaces = [{ name = "Sales" uc_tables = ["dev.s.t"] acl_groups = ["dev-users"] }]
uc_tables = ["dev.s.t"]
access_tier_groups = ["dev-admin", "dev-users"]
''')
    (dest / "env.auto.tfvars").write_text('''
genie_spaces = [{ name = "Sales" uc_tables = ["prod.s.t"] acl_groups = ["prod-only"] }]
uc_tables = ["prod.s.t"]
access_tier_groups = ["prod-admin", "prod-only"]
''')
    monkeypatch.setattr(sys, "argv", ["remap_env_config.py", str(source), str(dest), "dev=prod"])
    remap_env_config.main()
    import hcl2
    result = hcl2.load((dest / "env.auto.tfvars").open())
    assert result["genie_spaces"][0]["acl_groups"] == ["prod-only"]
    assert result["access_tier_groups"] == ["prod-admin", "prod-only"]
    output = capsys.readouterr().out
    assert "Preserved destination Genie space 'Sales' acl_groups=['prod-only']" in output
    assert "added in dev=['dev-users']; revoked in dev=['prod-only']" in output
    assert "edit envs/prod/env.auto.tfvars in a PR" in output


def test_repromote_preserves_destination_acl_omission(tmp_path, monkeypatch, capsys):
    source, dest = tmp_path / "dev", tmp_path / "prod"
    source.mkdir(); dest.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Sales" uc_tables = ["dev.s.t"] acl_groups = ["dev-users"] }]\n'
    )
    (dest / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Sales" uc_tables = ["prod.s.t"] }]\n'
    )
    monkeypatch.setattr(sys, "argv", ["remap_env_config.py", str(source), str(dest), "dev=prod"])
    remap_env_config.main()
    result = hcl2.load((dest / "env.auto.tfvars").open())
    assert "acl_groups" not in result["genie_spaces"][0]
    assert "omitted acl_groups" in capsys.readouterr().out


def test_table_only_promotion_preserves_and_remaps_top_level_uc_tables(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = []\n'
        'uc_tables = ["dev_catalog.genierails_e2e.customers"]\n'
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["remap_env_config.py", str(source), str(dest), "dev_catalog=prod_catalog"],
    )
    remap_env_config.main()

    with (dest / "env.auto.tfvars").open() as handle:
        config = hcl2.load(handle)
    assert config["genie_spaces"] == []
    assert config["uc_tables"] == ["prod_catalog.genierails_e2e.customers"]
    assert config["enable_classification"] is True
    assert "enable_auto_tagging" not in config
    # Promote writes no exposure flag: access follows the coverage gate.
    assert "business_access_enabled" not in config
    assert config["sql_warehouse_id"] == ""


def test_promotion_carries_verify_key_column_from_source(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text(
        'uc_tables = ["dev.sales.customers"]\nverify_key_column = "customer_id"\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["verify_key_column"] == "customer_id"


def test_promotion_preserves_destination_key_when_source_has_none(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text('uc_tables = ["dev.sales.customers"]\n')
    (dest / "env.auto.tfvars").write_text('verify_key_column = "prod_customer_id"\n')
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["verify_key_column"] == "prod_customer_id"


def test_promotion_does_not_copy_or_overwrite_environment_discovery(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "data_access").mkdir(parents=True)
    (dest / "data_access").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text("genie_spaces = []\nuc_tables = []\n")
    (source / "data_access" / "discovered_uc_tables.auto.tfvars").write_text(
        'discovered_uc_tables = ["dev_catalog.agent.orders"]\n'
        'discovered_table_agents = { "dev_catalog.agent.orders" = ["Dev agent"] }\n'
    )
    prod_fact = dest / "data_access" / "discovered_uc_tables.auto.tfvars"
    prod_fact.write_text(
        'discovered_uc_tables = ["prod_catalog.agent.orders"]\n'
        'discovered_table_agents = { "prod_catalog.agent.orders" = ["Prod agent"] }\n'
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["remap_env_config.py", str(source), str(dest), "dev_catalog=prod_catalog"],
    )
    remap_env_config.main()

    assert prod_fact.read_text() == (
        'discovered_uc_tables = ["prod_catalog.agent.orders"]\n'
        'discovered_table_agents = { "prod_catalog.agent.orders" = ["Prod agent"] }\n'
    )


def test_id_only_spaces_promote_with_distinct_canonical_titles(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "generated").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text('''genie_spaces = [
  { genie_space_id = "s1", uc_tables = ["paycat.s.t"] },
  { genie_space_id = "s2", uc_tables = ["hrcat.s.t"] },
  { name = "Named", genie_space_id = "s3", uc_tables = ["paycat.s.n"] },
]
''')
    (source / "generated" / "abac.auto.tfvars").write_text(
        'genie_space_id_to_name = { s1 = "Payments", s2 = "HR", s3 = "Ignored Title" }\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "paycat=ppay,hrcat=phr"
    ])
    remap_env_config.main()
    with (dest / "env.auto.tfvars").open() as handle:
        spaces = hcl2.load(handle)["genie_spaces"]
    assert [(s["name"], s["genie_space_id"]) for s in spaces] == [
        ("Payments", ""), ("HR", ""), ("Named", "")
    ]


def test_repromotion_preserves_destination_id_by_promoted_name_and_never_source_id(
    tmp_path, monkeypatch
):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Agent (dev_cat.s)", genie_space_id = "dev-id", '
        'uc_tables = ["dev_cat.s.t"] }]\n'
    )
    (dest / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Agent (prod_cat.s)", genie_space_id = "prod-id" }]\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev_cat=prod_cat"
    ])
    remap_env_config.main()
    space = hcl2.load((dest / "env.auto.tfvars").open())["genie_spaces"][0]
    assert space["genie_space_id"] == "prod-id"
    assert "dev-id" not in (dest / "env.auto.tfvars").read_text()


def test_repromotion_refuses_destination_id_copied_from_source(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Agent", genie_space_id = "dev-id", '
        'uc_tables = ["dev.s.t"] }]\n'
    )
    original = (
        'genie_spaces = [{ name = "Agent", genie_space_id = "dev-id" }]\n'
        'sql_warehouse_id = "prod-wh"\n'
    )
    (dest / "env.auto.tfvars").write_text(original)
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "which is also configured in the source environment" in output
    assert "nothing was written" in output
    assert (dest / "env.auto.tfvars").read_text() == original


def test_first_promotion_never_copies_source_genie_space_id(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Agent", genie_space_id = "dev-id", '
        'uc_tables = ["dev.s.t"] }]\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    space = hcl2.load((dest / "env.auto.tfvars").open())["genie_spaces"][0]
    assert space["genie_space_id"] == ""
    assert "dev-id" not in (dest / "env.auto.tfvars").read_text()


def test_promotion_carries_user_acl_overrides_including_explicit_empty(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "generated").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text('''genie_spaces = [
  { name = "Payments", uc_tables = ["dev.pay.t"], acl_groups = ["pay_group"] },
  { name = "Private", uc_tables = ["dev.private.t"], acl_groups = [] },
  { name = "Derived", uc_tables = ["dev.derived.t"] },
]
''')
    (source / "generated" / "abac.auto.tfvars").write_text("")
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    with (dest / "env.auto.tfvars").open() as handle:
        spaces = hcl2.load(handle)["genie_spaces"]
    assert spaces[0]["acl_groups"] == ["pay_group"]
    assert spaces[1]["acl_groups"] == []
    assert "acl_groups" not in spaces[2]


def test_duplicate_resolved_titles_fail_loud(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "generated").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text('''genie_spaces = [
  { genie_space_id = "s1", uc_tables = ["a.s.t"] },
  { genie_space_id = "s2", uc_tables = ["b.s.t"] },
]
''')
    (source / "generated" / "abac.auto.tfvars").write_text(
        'genie_space_id_to_name = { s1 = "Same", s2 = "Same" }\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "a=pa,b=pb"
    ])
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    assert "same canonical name" in capsys.readouterr().out
    assert not (dest / "env.auto.tfvars").exists()


def test_duplicate_effective_titles_after_remap_fail_before_write(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "generated").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text('''genie_spaces = [
  { name = "Payments", uc_tables = ["dev.s.pay"] },
  { name = "Orders", uc_tables = ["dev.s.orders"] },
]
''')
    (source / "generated" / "abac.auto.tfvars").write_text('''genie_space_configs = {
  Payments = { title = "Shared (prod.s)" }
  Orders = { title = "Shared (dev.s)" }
}
''')
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "'Payments' and 'Orders'" in output
    assert "same effective destination title 'Shared (prod.s)'" in output
    assert not (dest / "env.auto.tfvars").exists()


def test_canonical_title_is_hcl_escaped(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "generated").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ genie_space_id = "s1", uc_tables = ["a.s.t"] }]\n'
    )
    title = 'Finance "Quoted" \\ Analytics'
    (source / "generated" / "abac.auto.tfvars").write_text(
        "genie_space_id_to_name = { s1 = " + __import__("json").dumps(title) + " }\n"
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "a=pa"
    ])
    remap_env_config.main()
    with (dest / "env.auto.tfvars").open() as handle:
        assert hcl2.load(handle)["genie_spaces"][0]["name"] == title


def test_discovered_only_promotion_remaps_union(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "data_access").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Orders", genie_space_id = "s1" }]\n'
    )
    (source / "data_access/discovered_uc_tables.auto.tfvars").write_text(
        'discovered_uc_tables = ["dev.sales.orders"]\n'
        'discovered_table_agents = { "dev.sales.orders" = ["Orders"] }\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["uc_tables"] == ["prod.sales.orders"]
    assert config["genie_spaces"][0]["uc_tables"] == ["prod.sales.orders"]


def test_promotion_fails_closed_without_tables(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "Empty", genie_space_id = "s1" }]\n'
    )
    monkeypatch.setattr(remap_env_config, "_discover_from_genie_api", lambda *_: ("", []))
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    assert "no tables found for agent s1" in capsys.readouterr().out
    assert not (dest / "env.auto.tfvars").exists()


def test_empty_environment_fails_closed(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text("genie_spaces = []\nuc_tables = []\n")
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    assert "no tables found for source environment" in capsys.readouterr().out
    assert not (dest / "env.auto.tfvars").exists()


def test_repromotion_preserves_destination_settings_and_never_closes_live_access(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text('uc_tables = ["dev.s.t"]\n')
    (dest / "env.auto.tfvars").write_text(
        'sql_warehouse_id = "warehouse-prod"\n'
        'enable_auto_tagging = true\n'
        'business_access_enabled = true\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["sql_warehouse_id"] == "warehouse-prod"
    assert config["enable_auto_tagging"] is True
    # Re-promoting into a released prod used to reset the flag to false, so
    # the next apply revoked live access. The retired flag is dropped instead.
    assert "business_access_enabled" not in config
    assert "business_access_enabled = false" not in (dest / "env.auto.tfvars").read_text()


def test_promote_refuses_legacy_explicit_false_before_writing(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text('uc_tables = ["dev.s.t"]\n')
    original = 'enable_auto_tagging = false\nsql_warehouse_id = "prod-wh"\n'
    (dest / "env.auto.tfvars").write_text(original)
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])

    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()

    assert exc.value.code == 1
    assert (dest / "env.auto.tfvars").read_text() == original
    assert "promotion refuses to preserve a setting that can disable UI-managed auto-tagging" in capsys.readouterr().out


def test_promote_explicit_false_requires_intentional_override(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text('uc_tables = ["dev.s.t"]\n')
    (dest / "env.auto.tfvars").write_text('enable_auto_tagging = false\n')
    monkeypatch.setenv("ALLOW_DISABLE_AUTO_TAGGING", "1")
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])

    remap_env_config.main()

    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["enable_auto_tagging"] is False


def test_repromotion_keeps_destination_coverage_acknowledgements_only(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text(
        'uc_tables = ["dev.s.t"]\n'
        'coverage_acknowledged_columns = ["dev.s.t.product_name"]\n'
    )
    (dest / "env.auto.tfvars").write_text(
        'coverage_acknowledged_columns = ["prod.s.t.merchant_name"]\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    # A destination's reviewed false positives survive re-promotion; the
    # source's are its own review and never become the destination's.
    assert config["coverage_acknowledged_columns"] == ["prod.s.t.merchant_name"]


def test_first_promotion_writes_no_coverage_acknowledgements(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text(
        'uc_tables = ["dev.s.t"]\n'
        'coverage_acknowledged_columns = ["dev.s.t.product_name"]\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    assert "coverage_acknowledged_columns" not in hcl2.load((dest / "env.auto.tfvars").open())


def test_legacy_single_space_fallback_uses_only_discovered_tables(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "data_access").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(
        'uc_tables = ["dev.admin.audit_log"]\n'
        'genie_spaces = [{ name = "A", genie_space_id = "a" }]\n'
    )
    (source / "data_access/discovered_uc_tables.auto.tfvars").write_text(
        'discovered_uc_tables = ["dev.s.t1"]\n'
    )
    monkeypatch.setattr(
        remap_env_config, "_discover_from_genie_api",
        lambda *_: pytest.fail("legacy discovered fallback should avoid the API"),
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["genie_spaces"][0]["uc_tables"] == ["prod.s.t1"]
    assert config["uc_tables"] == ["prod.admin.audit_log", "prod.s.t1"]


def test_renamed_attribution_uses_api_instead_of_widening(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "data_access").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(
        'uc_tables = ["dev.admin.audit_log"]\n'
        'genie_spaces = [{ name = "New Name", genie_space_id = "a" }]\n'
    )
    (source / "data_access/discovered_uc_tables.auto.tfvars").write_text(
        'discovered_uc_tables = ["dev.s.t1"]\n'
        'discovered_table_agents = { "dev.s.t1" = ["Old Name"] }\n'
    )
    calls = []
    monkeypatch.setattr(
        remap_env_config, "_discover_from_genie_api",
        lambda space_id, _auth: (calls.append(space_id) or ("New Name", ["dev.s.current"])),
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert calls == ["a"]
    assert config["genie_spaces"][0]["uc_tables"] == ["prod.s.current"]


def test_api_discovered_catalog_must_be_mapped(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "A", genie_space_id = "a" }]\n'
    )
    monkeypatch.setattr(
        remap_env_config, "_discover_from_genie_api",
        lambda *_: ("A", ["other.s.t"]),
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "missing mappings for resolved catalog(s): other" in output
    assert "make generate ENV=dev`" in output
    assert not (dest / "env.auto.tfvars").exists()


def test_attribution_key_missing_from_discovered_union_fails_closed(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "data_access").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "A", genie_space_id = "a" }]\n'
    )
    (source / "data_access/discovered_uc_tables.auto.tfvars").write_text(
        'discovered_uc_tables = ["dev.s.t1"]\n'
        'discovered_table_agents = { "other.s.x" = ["A"] }\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod,other=prod_other"
    ])
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "table keys absent from discovered_uc_tables: other.s.x" in output
    assert "make generate" in output
    assert not (dest / "env.auto.tfvars").exists()


def test_unmapped_three_part_inline_table_fails(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (source / "env.auto.tfvars").write_text(
        'uc_tables = ["s.t", "other.s.t"]\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    with pytest.raises(SystemExit):
        remap_env_config.main()
    assert "other" in capsys.readouterr().out


def test_two_spaces_use_discovered_agent_attribution(tmp_path, monkeypatch):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "data_access").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "A" }, { name = "B" }]\n'
    )
    (source / "data_access/discovered_uc_tables.auto.tfvars").write_text(
        'discovered_uc_tables = ["dev.s.t1", "dev.s.t2"]\n'
        'discovered_table_agents = {\n'
        '  "dev.s.t1" = ["A"]\n'
        '  "dev.s.t2" = ["A", "B"]\n'
        '}\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["genie_spaces"][0]["uc_tables"] == ["prod.s.t1", "prod.s.t2"]
    assert config["genie_spaces"][1]["uc_tables"] == ["prod.s.t2"]


def test_stale_destination_discovery_fails_before_write(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    (dest / "data_access").mkdir(parents=True)
    (source / "env.auto.tfvars").write_text('uc_tables = ["dev.s.current"]\n')
    original = 'sql_warehouse_id = "prod-wh"\n'
    (dest / "env.auto.tfvars").write_text(original)
    (dest / "data_access/discovered_uc_tables.auto.tfvars").write_text(
        'discovered_uc_tables = ["prod.s.removed"]\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    with pytest.raises(SystemExit):
        remap_env_config.main()
    assert (dest / "env.auto.tfvars").read_text() == original
    output = capsys.readouterr().out
    stale_path = dest / "data_access/discovered_uc_tables.auto.tfvars"
    assert f"Remove the stale tool-owned discovery file before promoting: {stale_path}" in output
    assert "Preserved destination" not in output
    assert "Reset destination" not in output


def test_preservation_messages_and_per_space_warehouse(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    source.mkdir()
    dest.mkdir()
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "A", uc_tables = ["dev.s.t"] }]\n'
    )
    (dest / "env.auto.tfvars").write_text(
        'genie_spaces = [{ name = "A", sql_warehouse_id = "space-wh" }]\n'
        'business_access_enabled = true\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev=prod"
    ])
    remap_env_config.main()
    output = capsys.readouterr().out
    assert "Preserved destination sql_warehouse_id" not in output
    assert "Preserved destination enable_auto_tagging" not in output
    assert "Reset destination business_access_enabled" not in output
    assert "Dropped the retired business_access_enabled setting from the destination" in output
    config = hcl2.load((dest / "env.auto.tfvars").open())
    assert config["genie_spaces"][0]["sql_warehouse_id"] == "space-wh"


def test_space_title_naming_the_catalog_is_renamed_like_the_generated_config(
    tmp_path, monkeypatch
):
    """The env name must match the genie_space_configs key remap_hcl rewrites."""
    from scripts.remap_generated_config import remap_hcl

    source = tmp_path / "dev"
    dest = tmp_path / "prod"
    (source / "generated").mkdir(parents=True)
    title = "Walkthrough (dev_cat.demo)"
    (source / "env.auto.tfvars").write_text(
        'genie_spaces = [\n  { genie_space_id = "s1", uc_tables = ["dev_cat.demo.t"] },\n]\n'
    )
    (source / "generated" / "abac.auto.tfvars").write_text(
        f'genie_space_id_to_name = {{ s1 = "{title}" }}\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev_cat=prod_cat"
    ])
    remap_env_config.main()

    with (dest / "env.auto.tfvars").open() as handle:
        name = hcl2.load(handle)["genie_spaces"][0]["name"]
    generated_key = remap_hcl(f'"{title}" = {{}}', [("dev_cat", "prod_cat")])
    assert name == "Walkthrough (prod_cat.demo)"
    assert f'"{name}"' in generated_key


def _two_space_source(tmp_path, titles, catalog_map_src="dev_cat"):
    source = tmp_path / "dev"
    (source / "generated").mkdir(parents=True)
    entries = "".join(
        f'  {{ genie_space_id = "s{i}", uc_tables = ["{catalog_map_src}.demo.t{i}"] }},\n'
        for i in range(len(titles))
    )
    (source / "env.auto.tfvars").write_text(f"genie_spaces = [\n{entries}]\n")
    id_to_name = " ".join(f's{i} = "{t}"' for i, t in enumerate(titles))
    (source / "generated" / "abac.auto.tfvars").write_text(
        f"genie_space_id_to_name = {{ {id_to_name} }}\n"
    )
    return source


def test_space_names_that_collide_after_remap_fail_with_both_sources(
    tmp_path, monkeypatch, capsys
):
    source = _two_space_source(tmp_path, ["Agent (dev_cat.demo)", "Agent (prod_cat.demo)"])
    dest = tmp_path / "prod"
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev_cat=prod_cat"
    ])

    with pytest.raises(SystemExit):
        remap_env_config.main()

    out = capsys.readouterr().out
    assert "'Agent (dev_cat.demo)', 'Agent (prod_cat.demo)'" in out
    assert "'Agent (prod_cat.demo)'" in out
    assert not (dest / "env.auto.tfvars").exists()


def _deployed_state(dest, key):
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "terraform.tfstate").write_text(json.dumps({"resources": [
        {"module": "module.workspace", "type": "null_resource", "name": name,
         "instances": [{"index_key": key}]}
        for name in ("genie_space_create", "genie_space_config")
    ]}))
    (dest / f".genie_space_id_{key}").write_text("01live\n")
    (dest / f".genie_adopted_{key}").touch()


def test_rename_of_an_already_deployed_space_refuses_with_state_mv_guidance(
    tmp_path, monkeypatch, capsys
):
    """A changed key would destroy genie_space_create, which trashes the live agent."""
    title = "Walkthrough (dev_cat.demo)"
    source = _two_space_source(tmp_path, [title])
    dest = tmp_path / "prod"
    old_key = "walkthrough_dev_cat_demo"
    _deployed_state(dest, old_key)
    (dest / "env.auto.tfvars").write_text("business_access_enabled = true\n")
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev_cat=prod_cat"
    ])

    with pytest.raises(SystemExit):
        remap_env_config.main()

    out = capsys.readouterr().out
    assert "would trash the deployed agent" in out
    for name in ("genie_space_create", "genie_space_config"):
        assert (
            f"state-mv 'module.workspace.null_resource.{name}[\"{old_key}\"]' "
            f"'module.workspace.null_resource.{name}[\"walkthrough_prod_cat_demo\"]'"
        ) in out
    assert ".genie_space_id_walkthrough_prod_cat_demo" in out
    assert ".genie_adopted_walkthrough_prod_cat_demo" in out
    assert (dest / "env.auto.tfvars").read_text() == "business_access_enabled = true\n"


def test_deployed_space_whose_key_is_unchanged_promotes_normally(tmp_path, monkeypatch):
    title = "Walkthrough (dev_cat.demo)"
    source = _two_space_source(tmp_path, [title])
    dest = tmp_path / "prod"
    _deployed_state(dest, "walkthrough_prod_cat_demo")  # earlier promote already renamed it
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev_cat=prod_cat"
    ])

    remap_env_config.main()

    with (dest / "env.auto.tfvars").open() as handle:
        assert hcl2.load(handle)["genie_spaces"][0]["name"] == "Walkthrough (prod_cat.demo)"


def test_names_that_normalize_to_one_terraform_key_after_remap_fail(tmp_path, monkeypatch, capsys):
    """Distinct names can share a for_each key once lowercased and punctuation-collapsed."""
    source = _two_space_source(tmp_path, ["Agent (dev_cat.demo)", "agent prod_cat demo"])
    dest = tmp_path / "prod"
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev_cat=prod_cat"
    ])

    with pytest.raises(SystemExit):
        remap_env_config.main()

    out = capsys.readouterr().out
    assert "'Agent (dev_cat.demo)', 'agent prod_cat demo'" in out
    assert "Terraform key 'agent_prod_cat_demo'" in out
    assert not (dest / "env.auto.tfvars").exists()


def test_same_key_names_that_the_rename_does_not_merge_still_promote(tmp_path, monkeypatch):
    """Pre-existing same-key names are disambiguated by roots/workspace, as before."""
    source = _two_space_source(tmp_path, ["Pay Ops", "pay-ops"])
    dest = tmp_path / "prod"
    monkeypatch.setattr(sys, "argv", [
        "remap_env_config.py", str(source), str(dest), "dev_cat=prod_cat"
    ])

    remap_env_config.main()

    with (dest / "env.auto.tfvars").open() as handle:
        assert [s["name"] for s in hcl2.load(handle)["genie_spaces"]] == ["Pay Ops", "pay-ops"]


# ── A deterministic destination's own tiers need a group_map (decision 8) ────

TIER_SOURCE = '''governance_mode = "deterministic"
deployer_principal_id = "11111111-0000-0000-0000-000000000001"
access_tier_groups = ["dev_ops", "dev_analysts"]
genie_spaces = [{ name = "Payments", uc_tables = ["dev.s.t"], acl_groups = ["dev_ops", "dev_analysts"] }]
uc_tables = ["dev.s.t"]
table_readers = { "dev.s.t" = ["dev_ops", "dev_analysts"] }
'''
TIER_DEST = 'governance_mode = "deterministic"\ndeployer_principal_id = "22222222-0000-0000-0000-000000000002"\n'


def _tier_promote(tmp_path, monkeypatch, dest_text, source_text=TIER_SOURCE):
    source, dest = tmp_path / "dev", tmp_path / "prod"
    source.mkdir(exist_ok=True); dest.mkdir(exist_ok=True)
    (source / "env.auto.tfvars").write_text(source_text)
    (dest / "env.auto.tfvars").write_text(dest_text)
    monkeypatch.setattr(sys, "argv", ["remap_env_config.py", str(source), str(dest), "dev=prod"])
    return dest / "env.auto.tfvars"


def test_own_destination_tiers_without_group_map_are_refused_with_the_line_to_add(tmp_path, monkeypatch, capsys):
    # README Prod step 1 used to say: replace prod's access_tier_groups placeholders.
    dest_text = TIER_DEST + 'access_tier_groups = ["prod_ops", "prod_analysts"]\n'
    dest = _tier_promote(tmp_path, monkeypatch, dest_text)
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert dest.read_text() == dest_text, "nothing was written"
    assert "access_tier_groups promoted" not in out
    assert ("ERROR: destination access_tier_groups ['prod_ops', 'prod_analysts'] name prod's own group(s) "
            "prod_ops, prod_analysts, but promotion would replace them with ['dev_ops', 'dev_analysts'] from dev") in out
    assert "prod's tiers, table_readers and acl_groups would name dev's groups" in out
    assert 'group_map = { "dev_analysts" = "prod_analysts", "dev_ops" = "prod_ops" }' in out

    # The printed line is the fix: tiers, readers and ACLs then name prod's groups.
    (tmp_path / "prod" / "env.auto.tfvars").write_text(
        dest_text + 'group_map = { "dev_analysts" = "prod_analysts", "dev_ops" = "prod_ops" }\n')
    remap_env_config.main()
    result = hcl2.load(dest.open())
    assert result["access_tier_groups"] == ["prod_ops", "prod_analysts"]
    assert result["table_readers"] == {"prod.s.t": ["prod_ops", "prod_analysts"]}
    assert result["genie_spaces"][0]["acl_groups"] == ["prod_ops", "prod_analysts"]


def test_a_partial_group_map_is_completed_in_the_suggestion(tmp_path, monkeypatch, capsys):
    _tier_promote(tmp_path, monkeypatch, TIER_DEST + 'access_tier_groups = ["prod_ops", "prod_analysts"]\n'
                                                     'group_map = { dev_ops = "prod_ops" }\n')
    with pytest.raises(SystemExit):
        remap_env_config.main()
    out = capsys.readouterr().out
    assert "name prod's own group(s) prod_analysts," in out
    assert 'group_map = { "dev_analysts" = "prod_analysts", "dev_ops" = "prod_ops" }' in out


def test_a_different_tier_count_is_refused_with_a_template(tmp_path, monkeypatch, capsys):
    dest = _tier_promote(tmp_path, monkeypatch, TIER_DEST + 'access_tier_groups = ["prod_ops"]\n')
    before = dest.read_text()
    with pytest.raises(SystemExit):
        remap_env_config.main()
    out = capsys.readouterr().out
    assert dest.read_text() == before
    assert "dev has 2 tier(s) and prod 1" in out
    assert 'group_map = { "dev_ops" = "<prod group>", "dev_analysts" = "<prod group>" }' in out


@pytest.mark.parametrize("tiers", [
    None,                                                 # unset
    '["<most-privileged-group>", "<least-privileged-group>"]',  # the setup template's placeholders
    '["dev_ops", "dev_analysts"]',                        # the same names as dev
])
def test_destination_tiers_that_promotion_may_write_are_not_refused(tmp_path, monkeypatch, tiers):
    dest = _tier_promote(tmp_path, monkeypatch, TIER_DEST + (f"access_tier_groups = {tiers}\n" if tiers else ""))
    remap_env_config.main()
    assert hcl2.load(dest.open())["access_tier_groups"] == ["dev_ops", "dev_analysts"]


RENAMED_SOURCE = TIER_SOURCE.replace('"dev_analysts"', '"dev_viewers"')
RECORD = Path("generated") / "promoted_access_tiers.json"


def _first_promote_then_rename(tmp_path, monkeypatch):
    """A first promote into placeholder tiers records what it wrote; then dev renames a tier."""
    placeholders = TIER_DEST + 'access_tier_groups = ["<most-privileged-group>", "<least-privileged-group>"]\n'
    dest = _tier_promote(tmp_path, monkeypatch, placeholders, source_text=RENAMED_SOURCE)
    remap_env_config.main()
    (tmp_path / "dev" / "env.auto.tfvars").write_text(TIER_SOURCE)
    return dest


def test_the_first_promote_into_placeholders_records_the_tiers_it_wrote(tmp_path, monkeypatch):
    dest = _tier_promote(tmp_path, monkeypatch, TIER_DEST + 'access_tier_groups = ["<a>", "<b>"]\n')
    remap_env_config.main()
    assert hcl2.load(dest.open())["access_tier_groups"] == ["dev_ops", "dev_analysts"]
    record = json.loads((tmp_path / "prod" / RECORD).read_text())
    assert record["access_tier_groups"] == ["dev_ops", "dev_analysts"]
    assert record["env"] == "prod" and record["source_env"] == "dev"
    assert remap_env_config._recorded_promoted_tiers(str(tmp_path / "prod")) == ({"dev_ops", "dev_analysts"}, "")


def test_tiers_a_previous_promotion_recorded_still_follow_the_source(tmp_path, monkeypatch, capsys):
    # A dev tier rename is promoted governance, not prod's own tiers being replaced.
    dest = _first_promote_then_rename(tmp_path, monkeypatch)
    assert hcl2.load(dest.open())["access_tier_groups"] == ["dev_ops", "dev_viewers"]
    capsys.readouterr()
    remap_env_config.main()
    assert hcl2.load(dest.open())["access_tier_groups"] == ["dev_ops", "dev_analysts"]
    assert "access_tier_groups promoted: ['dev_ops', 'dev_viewers'] -> ['dev_ops', 'dev_analysts']" in capsys.readouterr().out
    assert json.loads((tmp_path / "prod" / RECORD).read_text())["access_tier_groups"] == ["dev_ops", "dev_analysts"]


def _assert_refused(tmp_path, dest, capsys, own, reason=None):
    capsys.readouterr()
    before = dest.read_text()
    record = tmp_path / "prod" / RECORD
    record_before = record.read_bytes() if record.exists() else None
    with pytest.raises(SystemExit) as exc:
        remap_env_config.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert dest.read_text() == before, "nothing was written"
    assert (record.read_bytes() if record.exists() else None) == record_before
    assert f"name prod's own group(s) {own}, but promotion would replace them" in out
    assert "access_tier_groups promoted" not in out
    assert "group_map = { " in out
    if reason:
        assert f"(envs/prod/generated/promoted_access_tiers.json {reason}, so no tier there counts" in out
    return out


def test_generated_groups_naming_prod_tiers_do_not_bypass_the_refusal(tmp_path, monkeypatch, capsys):
    # Codex's repro: prod's own tiers also appear in prod's generated config
    # (a hand edit, or any other reason). Without a promotion record they stay
    # prod's own, so promotion must not swap in dev's names.
    dest = _tier_promote(tmp_path, monkeypatch, TIER_DEST + 'access_tier_groups = ["prod_ops", "prod_analysts"]\n')
    (tmp_path / "prod" / "generated").mkdir()
    (tmp_path / "prod" / "generated" / "abac.auto.tfvars").write_text(
        'groups = { prod_ops = {}, prod_analysts = {} }\n')
    out = _assert_refused(tmp_path, dest, capsys, "prod_ops, prod_analysts", "is missing")
    assert 'group_map = { "dev_analysts" = "prod_analysts", "dev_ops" = "prod_ops" }' in out


def test_a_missing_record_leaves_promoted_looking_tiers_prod_owned(tmp_path, monkeypatch, capsys):
    # Tiers that look like dev's but carry no record could have been typed in.
    dest = _tier_promote(tmp_path, monkeypatch, TIER_DEST + 'access_tier_groups = ["dev_ops", "dev_viewers"]\n')
    _assert_refused(tmp_path, dest, capsys, "dev_viewers", "is missing")


@pytest.mark.parametrize("tamper", [
    "tiers",       # the record's tiers edited, digest left as it was
    "digest",      # the digest edited
    "other_env",   # a valid record copied from another env
    "unreadable",  # not JSON
    "version",     # an unknown record version
])
def test_a_record_that_does_not_verify_vouches_for_nothing(tmp_path, monkeypatch, capsys, tamper):
    dest = _first_promote_then_rename(tmp_path, monkeypatch)
    path = tmp_path / "prod" / RECORD
    record = json.loads(path.read_text())
    if tamper == "tiers":
        record["access_tier_groups"] = ["prod_ops", "prod_analysts"]
    elif tamper == "digest":
        record["sha256"] = "0" * 64
    elif tamper == "other_env":
        record["env"] = "staging"
        record["sha256"] = remap_env_config._promoted_tiers_digest("staging", record["access_tier_groups"])
    elif tamper == "version":
        record["version"] = 99
    path.write_text("{not json" if tamper == "unreadable" else json.dumps(record))
    if tamper == "tiers":
        dest.write_text(dest.read_text().replace('"dev_ops", "dev_viewers"', '"prod_ops", "prod_analysts"'))
        _assert_refused(tmp_path, dest, capsys, "prod_ops, prod_analysts", "does not verify")
    else:
        _assert_refused(tmp_path, dest, capsys, "dev_viewers", "does not verify")


def test_prod_tiers_edited_after_a_recorded_promote_are_prod_owned(tmp_path, monkeypatch, capsys):
    # The record only vouches for the groups it lists.
    dest = _first_promote_then_rename(tmp_path, monkeypatch)
    dest.write_text(dest.read_text().replace('"dev_ops", "dev_viewers"', '"prod_ops", "dev_viewers"'))
    out = _assert_refused(tmp_path, dest, capsys, "prod_ops")
    assert "so no tier there counts" not in out


def test_legacy_destination_tiers_are_still_kept(tmp_path, monkeypatch):
    legacy = TIER_SOURCE.replace('governance_mode = "deterministic"\n', "")
    dest = _tier_promote(tmp_path, monkeypatch, 'access_tier_groups = ["prod_ops", "prod_analysts"]\n',
                         source_text=legacy)
    remap_env_config.main()
    assert hcl2.load(dest.open())["access_tier_groups"] == ["prod_ops", "prod_analysts"]
