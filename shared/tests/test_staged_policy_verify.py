"""The live check run after each staged policy phase (scripts/staged_policy_verify.py)."""

import pytest

from scripts import staged_policy_verify as spv

VALUE = "email_partial__string__0a1b2c3d"
RENDERED = {
    "fgac_policies": [{
        "name": f"gr_mask_cat_{VALUE}_full", "catalog": "cat", "policy_type": "POLICY_TYPE_COLUMN_MASK",
        "to_principals": ["account users"], "except_principals": ["raw"],
        "match_condition": f"hasTagValue('gr_treatment', '{VALUE}')",
        "function_catalog": "cat", "function_schema": "sch", "function_name": f"gr_mask_{VALUE}_full",
    }],
    "tags": {"cat.sch.t.email": VALUE},
}


def _live(**overrides):
    policy = {
        "name": f"cat_gr_mask_cat_{VALUE}_full", "policy_type": "COLUMN_MASK",
        "to_principals": ["account users"], "except_principals": ["raw"],
        "column_mask": {"function_name": f"cat.sch.gr_mask_{VALUE}_full"},
        "match_columns": [{"condition": f"hasTagValue('gr_treatment', '{VALUE}')", "alias": "masked_col"}],
        "on_securable_fullname": "cat",
    }
    return policy | overrides


def _problems(catalog=None, table=None, functions=None, tags=None, untagged=()):
    catalog = [_live()] if catalog is None else catalog
    return spv.live_problems(
        RENDERED, {"cat": catalog}, {"cat.sch.t": catalog if table is None else table},
        {f"cat.sch.gr_mask_{VALUE}_full"} if functions is None else functions,
        {"cat.sch.t.email": VALUE} if tags is None else tags, set(untagged))


def test_live_matching_the_render_passes():
    assert _problems() == []


@pytest.mark.parametrize("kwargs, says", [
    ({"catalog": []}, "is not live"),
    ({"catalog": [_live(except_principals=[])]}, "live except"),
    ({"catalog": [_live(to_principals=["masked"])]}, "live to"),
    ({"catalog": [_live(column_mask={"function_name": "cat.sch.other"})]}, "live function"),
    ({"catalog": [_live(match_columns=[{"condition": "hasTagValue('gr_treatment', 'x')"}])]}, "live match"),
    ({"catalog": [_live(), _live(name=f"cat_gr_mask_cat_{VALUE[:-8]}deadbeef_full",
                               column_mask={"function_name": f"cat.sch.gr_mask_{VALUE[:-8]}deadbeef_full"})]},
     "not in this stage's render"),
    ({"table": [_live(), _live(name="someone_elses_mask", on_securable_fullname="cat.sch.t")]}, "effective mask"),
    ({"functions": set()}, "doesn't exist"),
    ({"tags": {}}, "live treatment none"),
    ({"tags": {"cat.sch.t.email": VALUE, "cat.sch.t.ip": VALUE}, "untagged": ["cat.sch.t.ip"]}, "rendered none"),
])
def test_every_difference_is_reported(kwargs, says):
    problems = _problems(**kwargs)
    assert any(says in p for p in problems), problems


def test_sdk_objects_normalize_like_api_dicts():
    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    sdk = Obj(name="n", policy_type=Obj(value="COLUMN_MASK"), to_principals=["b", "a"], except_principals=None,
              column_mask=Obj(function_name="C.S.F"), match_columns=[Obj(condition="hasTagValue('k', 'v')")],
              on_securable_fullname="cat")
    assert spv.normalize_policy(sdk) == {"name": "n", "policy_type": "COLUMN_MASK", "to": ["a", "b"], "except": [],
                                         "function": "c.s.f", "conditions": ["hasTagValue('k','v')"], "on": "cat"}


def test_wrong_or_missing_stage_record_fails(tmp_path):
    assert spv.verify(tmp_path, {"switch"}, reader=None) == ["no staged apply is recorded"]
