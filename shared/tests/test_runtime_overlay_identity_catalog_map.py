"""kept_policy_names checks for an unmanaged policy even with no older name (S2).

A catalog mapped to itself, or a policy name that doesn't carry its catalog,
has no older form, so the collision check used to be skipped and prod's state
could adopt a policy it doesn't own (e.g. dev's, on a shared metastore).
"""

import pytest

from scripts import runtime_overlay as ro


def _policy(name, catalog):
    return {"name": name, "catalog": catalog, "policy_type": "POLICY_TYPE_COLUMN_MASK"}


def test_identity_map_refuses_the_source_envs_unmanaged_policy():
    with pytest.raises(ro.OverlayError) as exc:
        ro.kept_policy_names([_policy("gr_mask_paycat_v_full", "paycat")], {"paycat": "paycat"}, set(),
                             lambda c: {"paycat_gr_mask_paycat_v_full"})
    message = str(exc.value)
    assert "paycat already has a policy named paycat_gr_mask_paycat_v_full that GenieRails doesn't manage" in message
    assert "catalog_map maps paycat to itself" in message
    assert "map paycat to a distinct catalog (don't import it)" in message
    # The same name is fine once prod's state manages it, or while it isn't live.
    policy = [_policy("gr_mask_paycat_v_full", "paycat")]
    assert ro.kept_policy_names(policy, {"paycat": "paycat"}, {("paycat", "gr_mask_paycat_v_full")},
                                lambda c: {"paycat_gr_mask_paycat_v_full"}) == {}
    assert ro.kept_policy_names(policy, {"paycat": "paycat"}, set(), lambda c: set()) == {}


def test_policy_name_without_its_catalog_refuses_an_unmanaged_policy():
    with pytest.raises(ro.OverlayError, match="remove it or import it into the prod data_access state"):
        ro.kept_policy_names([_policy("pay", "ppay")], {"paycat": "ppay"}, set(), lambda c: {"ppay_pay"})


def test_no_older_name_fails_closed_when_policies_cant_be_listed():
    with pytest.raises(ro.OverlayError, match="is taken by a policy GenieRails doesn't manage"):
        ro.kept_policy_names([_policy("gr_mask_paycat_v_full", "paycat")], {"paycat": "paycat"}, set(),
                             lambda c: None)



def test_an_unmapped_unlistable_catalog_is_not_refused_but_is_still_checked_when_listed():
    """Only catalogs the map maps into can collide with a source policy; others keep the old skip."""
    policy = [_policy("gr_mask_prod_fin_v_full", "prod_fin")]
    assert ro.kept_policy_names(policy, {"dev_cat": "prod_cat"}, set(), lambda c: None) == {}
    with pytest.raises(ro.OverlayError, match="doesn't manage; remove it or import it"):
        ro.kept_policy_names(policy, {"dev_cat": "prod_cat"}, set(), lambda c: {"prod_fin_gr_mask_prod_fin_v_full"})
