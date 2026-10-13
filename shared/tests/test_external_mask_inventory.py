"""Masks GenieRails didn't create on governed columns stop every deterministic run."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import external_mask_inventory as inv
from scripts.sync_tag_policies import retained_deterministic_values

SHARED = Path(__file__).resolve().parents[1]
VALUE = "email_partial__string__0a1b2c3d"
OLD_VALUE = "email_partial__string__99999999"


def _policy(name, function, policy_type="COLUMN_MASK", where="cat"):
    return {"name": name, "function_name": function, "policy_type": policy_type, "on_securable_fullname": where}


def test_own_policies_of_any_generation_are_recognised():
    assert inv.is_own_policy(f"cat_gr_mask_cat_{VALUE}_full", f"cat.sch.gr_mask_{VALUE}_full")
    assert inv.is_own_policy(f"cat_gr_mask_cat_{OLD_VALUE}_partial", f"cat.sch.gr_mask_{OLD_VALUE}_partial")
    # A legacy GenieRails mask, a lookalike name or a mismatched function is not.
    assert not inv.is_own_policy("cat_gr_mask_cat_email_partial", "cat.sch.mask_email")
    assert not inv.is_own_policy(f"cat_gr_mask_cat_{VALUE}_full", "cat.sch.mask_email")
    assert not inv.is_own_policy(f"cat_gr_mask_other_{VALUE}_full", f"cat.sch.gr_mask_{VALUE}_full")


def test_external_masks_on_governed_columns_and_tables_are_reported():
    governed = {"cat.sch.t.email"}
    direct = [["cat", "sch", "t", "email", "sec.fn.legacy_mask"],
              ["cat", "sch", "t", "other", "sec.fn.unrelated"]]
    policies = {"cat.sch.t": [
        _policy(f"cat_gr_mask_cat_{VALUE}_full", f"cat.sch.gr_mask_{VALUE}_full"),
        _policy("team_mask", "cat.sec.mask_it", where="cat.sch"),
        _policy("team_filter", "cat.sec.only_anz", policy_type="ROW_FILTER"),
    ]}
    problems = inv.external_masks(governed, direct, policies)
    assert problems == [
        "cat.sch.t.email: table-attached mask sec.fn.legacy_mask",
        "cat.sch.t: column-mask policy 'team_mask' on cat.sch (function cat.sec.mask_it)",
    ]
    assert inv.external_masks(governed, [], {"cat.sch.t": policies["cat.sch.t"][:1]}) == []


@pytest.mark.parametrize("row", [
    ["cat", "sch", "t", "email"],
    ["cat", "sch", "t", "email", "sec.fn.mask", "extra"],
    ["cat", "sch", "t", "email", None],
])
def test_column_mask_rows_fail_closed_on_unexpected_shapes(row):
    with pytest.raises(ValueError, match="expected five non-empty strings"):
        inv.external_masks({"cat.sch.t.email"}, [row], {})


class _Policies:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def list_policies(self, securable_type, fullname, include_inherited=None):
        self.calls.append((securable_type, fullname, include_inherited))
        return [SimpleNamespace(name=r["name"], policy_type=SimpleNamespace(value=r["policy_type"]),
                                on_securable_fullname=r["on_securable_fullname"],
                                column_mask=SimpleNamespace(function_name=r["function_name"]))
                for r in self.rows]


def test_inventory_reads_inherited_policies_and_column_masks(tmp_path, monkeypatch):
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "abac.auto.tfvars").write_text(
        f'tag_assignments = [{{ entity_type = "columns", entity_name = "cat.sch.t.email", '
        f'tag_key = "gr_treatment", tag_value = "{VALUE}" }}]\n')
    (tmp_path / "env.auto.tfvars").write_text('governance_mode = "deterministic"\n')
    (tmp_path / "generated" / "governed_tables.json").write_text('{"tables":["cat.sch.t"]}\n')
    client = SimpleNamespace(policies=_Policies([_policy("ext", "cat.sch.ext")]))
    sql = []
    import scripts.audit_schema_drift as drift
    monkeypatch.setattr(drift, "_get_sdk_client", lambda env_dir: client)
    monkeypatch.setattr(drift, "_get_warehouse_id", lambda env_dir, w: "wh")
    monkeypatch.setattr(drift, "_run_sql", lambda w, wh, statement: sql.append(statement) or [])
    assert inv.inventory(tmp_path) == ["cat.sch.t: column-mask policy 'ext' on cat (function cat.sch.ext)"]
    assert client.policies.calls == [("TABLE", "cat.sch.t", True)]
    assert "system.information_schema.column_masks" in sql[0] and "'cat.sch.t.email'" in sql[0]
    assert "mask_name" in sql[0]
    assert "mask_catalog" not in sql[0] and "mask_schema" not in sql[0]
    assert inv.main(["--env-dir", str(tmp_path)]) == 1


def test_inventory_failure_is_its_own_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "inventory", lambda env_dir: (_ for _ in ()).throw(RuntimeError("no creds")))
    assert inv.main(["--env-dir", str(tmp_path)]) == 2


def _recipe(target: str) -> str:
    text = (SHARED / "Makefile.shared").read_text()
    match = re.search(rf"^{re.escape(target)}:.*?\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert match, target
    return match.group(1)


def test_inventory_runs_before_every_rehearse_release_and_maintain_in_deterministic_envs_only():
    # rehearse reaches apply through _derive-before-exposure, like plan and apply.
    for target in ("_derive-before-exposure", "release"):
        recipe = _recipe(target)
        assert 'if [ "$$' in recipe and "= deterministic ]; then python3 \"$(SHARED_ROOT)/scripts/external_mask_inventory.py\"" in recipe, target
        assert "external_mask_inventory.py\" --env-dir \"$(ENV_DIR)\" || exit 1" in recipe, target
    # maintain, release-plan and the manifest release run it through a variable tests can stub.
    makefile = (SHARED / "Makefile.shared").read_text()
    assert '_EXTERNAL_MASK_INVENTORY = python3 "$(SHARED_ROOT)/scripts/external_mask_inventory.py" --env-dir "$(ENV_DIR)"' in makefile
    assert "= deterministic ]; then $(_EXTERNAL_MASK_INVENTORY) || exit 1" in _recipe("maintain")
    for target in ("release-plan", "_release-manifest"):
        assert "$(_EXTERNAL_MASK_INVENTORY) || exit 1" in _recipe(target), target
    assert "$(MAKE) _derive-before-exposure" in _recipe("rehearse").split("$(MAKE) apply")[0]


def test_tag_sync_never_drops_a_live_deterministic_value():
    current = {"redact", VALUE, OLD_VALUE}
    assert retained_deterministic_values("gr_treatment", current) == {VALUE, OLD_VALUE}
    assert retained_deterministic_values("gr_treatment_ab12", current) == {VALUE, OLD_VALUE}
    assert retained_deterministic_values("pii_level", current) == set()
    # Legacy values keep being synced exactly to the desired list.
    assert retained_deterministic_values("gr_treatment", {"redact", "email_partial"}) == set()
