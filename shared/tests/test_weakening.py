import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ack_file
import weakening as wk


ENV = "prod"
COLUMN = "cat.sch.tab.secret"


def protection(level, text=None):
    return wk.Protection(level, text or level)


def state(level="redacted", principal="analysts", column=COLUMN):
    return wk.GovernanceState(columns={column: {principal: protection(level)}})


@pytest.mark.parametrize("before,after", [
    ("redacted", "partial:v1"), ("NULL", "partial:v1"),
    ("partial:v1", "raw"), ("redacted", "raw"),
])
def test_every_downward_protection_transition_weakens(before, after):
    issues = wk.find_weakenings(state(before), state(after), ENV)
    assert [(i.category, i.before, i.after) for i in issues] == [("protection", before, after)]


@pytest.mark.parametrize("before,after", [
    ("raw", "partial:v1"), ("partial:v1", "redacted"),
    ("redacted", "NULL"), ("partial:v1", "partial:v2"),
])
def test_equal_or_stronger_protection_does_not_weaken(before, after):
    assert wk.find_weakenings(state(before), state(after), ENV) == ()


def test_union_detects_removed_column_and_removed_principal():
    removed_column = wk.find_weakenings(state(), wk.GovernanceState(), ENV)
    assert [(x.category, x.object_name) for x in removed_column] == [("missing_after", COLUMN)]
    before = wk.GovernanceState(columns={COLUMN: {"a": protection("redacted"), "b": protection("redacted")}})
    after = wk.GovernanceState(columns={COLUMN: {"a": protection("redacted")}})
    assert [(x.category, x.principal) for x in wk.find_weakenings(before, after, ENV)] == [("missing_after", "b")]


def test_union_detects_new_column_or_principal_as_unknown_before():
    assert wk.find_weakenings(wk.GovernanceState(), state("raw"), ENV)[0].category == "missing_before"
    assert wk.find_weakenings(wk.GovernanceState(), state("partial:v1"), ENV)[0].category == "missing_before"


@pytest.mark.parametrize("level", ["redacted", "NULL"])
def test_new_column_at_strongest_protection_is_not_weakening(level):
    assert wk.find_weakenings(wk.GovernanceState(), state(level), ENV) == ()


def test_identifiers_are_case_insensitive_and_case_only_raw_change_is_flagged():
    issues = wk.find_weakenings(
        state("redacted", column="CAT.SCH.TAB.SECRET"),
        state("raw", column="cat.sch.tab.secret"), ENV,
    )
    assert len(issues) == 1 and issues[0].category == "protection"
    assert issues[0].object_name == COLUMN


def test_case_colliding_unknown_data_is_refused():
    before = wk.GovernanceState(columns={
        "CAT.SCH.TAB.SECRET": {"p": protection("raw")},
        "cat.sch.tab.secret": {"p": protection("redacted")},
    })
    with pytest.raises(ValueError, match="case-colliding"):
        wk.find_weakenings(before, state(), ENV)


def test_raw_exempt_is_one_issue_per_readable_table_even_without_columns():
    before = wk.GovernanceState()
    after = wk.GovernanceState(
        raw_exempt_principals={"etl"},
        table_readers={"CAT.SCH.ONE": {"etl"}, "cat.sch.two": {"etl"}, "cat.sch.no": {"other"}},
    )
    issues = [x for x in wk.find_weakenings(before, after, ENV) if x.category == "raw_exempt_principal"]
    # Not a direct reader of cat.sch.no, but it could read it via a group or
    # outside grant, so every governed table is flagged.
    assert {x.object_name for x in issues} == {"cat.sch.one", "cat.sch.two", "cat.sch.no"}
    assert all(x.ack_key == f"prod:weaken:{x.object_name}:etl" for x in issues)


def test_raw_exempt_covers_tables_known_only_from_columns_or_filters():
    after = wk.GovernanceState(
        columns={COLUMN: {"t1": protection("redacted")}},
        row_filters={"c.s.f": {"g": wk.RowFilter(frozenset({"A"}))}},
        raw_exempt_principals={"etl"},
    )
    before = wk.GovernanceState(columns=after.columns, row_filters=after.row_filters)
    issues = wk.find_weakenings(before, after, ENV)
    assert {(x.category, x.object_name) for x in issues} == {
        ("raw_exempt_principal", "cat.sch.tab"), ("raw_exempt_principal", "c.s.f"),
    }


def test_existing_or_removed_raw_exempt_is_not_weakening():
    value = wk.GovernanceState(raw_exempt_principals={"etl"}, table_readers={"c.s.t": {"etl"}})
    assert wk.find_weakenings(value, value, ENV) == ()
    assert wk.find_weakenings(value, wk.GovernanceState(table_readers={"c.s.t": {"etl"}}), ENV) == ()


def test_partial_raw_is_standalone_without_principals_or_columns():
    before = wk.GovernanceState(partial_versions={"email": "mask"})
    after = wk.GovernanceState(partial_versions={"email": "raw"})
    issue = wk.find_weakenings(before, after, ENV)[0]
    assert (issue.category, issue.ack_key) == ("partial_raw", "prod:weaken:email:partial")


def test_missing_partial_after_fails_closed():
    before = wk.GovernanceState(partial_versions={COLUMN: "mask"})
    issue = wk.find_weakenings(before, wk.GovernanceState(), ENV)[0]
    assert issue.category == "missing_partial_after"


def test_removed_row_filter_is_table_level_without_columns():
    before = wk.GovernanceState(
        row_filters={"CAT.SCH.TAB": {"regional": wk.RowFilter(frozenset({"APAC"}))}}
    )
    issues = wk.find_weakenings(before, wk.GovernanceState(), ENV)
    assert {(x.category, x.object_name, x.ack_key) for x in issues} == {
        ("row_filter_removed", "cat.sch.tab", "prod:weaken:cat.sch.tab:regional"),
        # readers outside the filter went from zero rows to all rows
        ("row_filter_removed", "cat.sch.tab", "prod:weaken:cat.sch.tab:*"),
    }


def test_removed_filter_with_no_groups_is_still_flagged_per_table():
    before = wk.GovernanceState(row_filters={"c.s.t": {}})
    issues = wk.find_weakenings(before, wk.GovernanceState(), ENV)
    assert [(x.object_name, x.principal) for x in issues] == [("c.s.t", "*")]
    assert ack_file.parse_line("prod:weaken:c.s.t:*", 1, "prod").key == issues[0].ack_key


def test_group_removed_from_filter_is_widening_but_others_per_table_unaffected():
    before = wk.GovernanceState(row_filters={
        "c.s.a": {"g": wk.RowFilter(frozenset({"A"})), "h": wk.RowFilter(frozenset({"A"}))},
        "c.s.b": {"g": wk.RowFilter(frozenset({"A"}))},
    })
    after = wk.GovernanceState(row_filters={
        "c.s.a": {"h": wk.RowFilter(frozenset({"A"}))},
        "c.s.b": {"g": wk.RowFilter(frozenset({"A"}))},
    })
    issues = wk.find_weakenings(before, after, ENV)
    assert [(x.category, x.object_name, x.principal) for x in issues] == [("row_filter_removed", "c.s.a", "g")]


def test_widened_and_replaced_row_filter_are_flagged_but_narrowed_is_not():
    def filters(values):
        return wk.GovernanceState(row_filters={"cat.sch.tab": {"g": wk.RowFilter(values)}})
    base = filters(frozenset({"A"}))
    assert wk.find_weakenings(base, filters(frozenset({"A", "B"})), ENV)
    assert wk.find_weakenings(base, filters(frozenset({"B"})), ENV)
    assert wk.find_weakenings(base, filters(frozenset()), ENV) == ()


def test_new_filter_group_is_widening():
    before = wk.GovernanceState(row_filters={"c.s.t": {"old": wk.RowFilter(frozenset({"A"}))}})
    after = wk.GovernanceState(row_filters={"c.s.t": {
        "old": wk.RowFilter(frozenset({"A"})), "new": wk.RowFilter(frozenset({"B"})),
    }})
    issue = wk.find_weakenings(before, after, ENV)[0]
    assert (issue.category, issue.principal) == ("row_filter_new_group", "new")


def test_new_filter_group_with_unrestricted_values_is_widening_but_empty_is_not():
    before = wk.GovernanceState(row_filters={"c.s.t": {"old": wk.RowFilter(frozenset({"A"}))}})
    def with_new(values):
        return wk.GovernanceState(row_filters={"c.s.t": {
            "old": wk.RowFilter(frozenset({"A"})), "new": wk.RowFilter(values),
        }})
    assert wk.find_weakenings(before, with_new(None), ENV)[0].after == "all rows"
    assert wk.find_weakenings(before, with_new(frozenset()), ENV) == ()


def test_finite_filter_to_unrestricted_is_widening():
    def filters(values):
        return wk.GovernanceState(row_filters={"c.s.t": {"g": wk.RowFilter(values)}})
    issue = wk.find_weakenings(filters(frozenset({"A"})), filters(None), ENV)[0]
    assert (issue.category, issue.after) == ("row_filter_widened", "all rows")
    assert wk.find_weakenings(filters(None), filters(frozenset({"A"})), ENV) == ()


def test_first_filter_on_previously_unfiltered_table_is_tightening():
    after = wk.GovernanceState(
        row_filters={"c.s.t": {"new": wk.RowFilter(frozenset({"B"}))}}
    )
    assert wk.find_weakenings(wk.GovernanceState(), after, ENV) == ()


def test_unchanged_unrestricted_filter_is_not_flagged():
    value = wk.GovernanceState(row_filters={"c.s.t": {"g": wk.RowFilter(None)}})
    assert wk.find_weakenings(value, value, ENV) == ()


def test_new_table_reader_uses_env_scoped_reader_ack():
    before = wk.GovernanceState(table_readers={"cat.sch.tab": {"old"}})
    after = wk.GovernanceState(table_readers={"CAT.SCH.TAB": {"old", "new"}})
    issue = wk.find_weakenings(before, after, ENV)[0]
    assert issue.ack_key == "prod:reader:cat.sch.tab:new"


def test_missing_table_reader_map_fails_closed():
    before = wk.GovernanceState(table_readers={"cat.sch.tab": {"old"}})
    issue = wk.find_weakenings(before, wk.GovernanceState(), ENV)[0]
    assert issue.category == "missing_table_readers_after"


def test_exact_ack_subtracts_issue_without_individual_stale_check():
    key = f"prod:weaken:{COLUMN}:analysts"
    result = wk.check_weakenings(state("redacted"), state("raw"), ENV, {key})
    assert result.ok and result.acknowledged[0].consequence == "redacted -> raw"


def test_wrongly_scoped_ack_does_not_hide_issue():
    result = wk.check_weakenings(
        state("redacted"), state("raw"), ENV,
        {"prod:weaken:cat.sch.tab.other:analysts"},
    )
    assert not result.ok and len(result.unacknowledged) == 1


def test_unknown_protection_is_refused():
    with pytest.raises(ValueError, match="unknown protection level"):
        wk.find_weakenings(state("redacted"), state("mystery"), ENV)


def test_cli_reconciles_other_check_consequence_once(tmp_path, capsys):
    before = tmp_path / "before.json"; after = tmp_path / "after.json"
    ack = tmp_path / "ack.txt"; known = tmp_path / "known.json"
    before.write_text("{}")
    after.write_text("{}")
    ack.write_text("prod:reader:cat.sch.tab:p\n")
    known.write_text(json.dumps({"prod:reader:cat.sch.tab:p": "live external reader"}))
    assert wk.main([
        "--env", "prod", "--before", str(before), "--after", str(after),
        "--ack-file", str(ack), "--all-consequences", str(known),
    ]) == 0
    assert "no stale acknowledgement" in capsys.readouterr().out


def test_cli_nonzero_for_unacknowledged_and_truly_stale_ack(tmp_path, capsys):
    before = tmp_path / "before.json"; after = tmp_path / "after.json"; ack = tmp_path / "ack.txt"
    before.write_text(json.dumps({"columns": {COLUMN: {"p": {"level": "redacted", "consequence": "masked"}}}}))
    after.write_text(json.dumps({"columns": {COLUMN: {"p": {"level": "raw", "consequence": "clear text"}}}}))
    ack.write_text("prod:reader:cat.sch.tab:q\n")
    assert wk.main(["--env", "prod", "--before", str(before), "--after", str(after), "--ack-file", str(ack)]) == 1
    error = capsys.readouterr().err
    assert "masked -> clear text" in error and "matches nothing" in error


def test_cli_merges_several_consequence_files_and_reconciles_once(tmp_path, capsys):
    before = tmp_path / "before.json"; after = tmp_path / "after.json"; ack = tmp_path / "ack.txt"
    readers = tmp_path / "readers.json"; unclassified = tmp_path / "unclassified.json"
    before.write_text("{}"); after.write_text("{}")
    ack.write_text("prod:reader:c.s.t:p\nprod:unclassified:c.s.t.col\n")
    readers.write_text(json.dumps({"prod:reader:c.s.t:p": "reader"}))
    unclassified.write_text(json.dumps({"prod:unclassified:c.s.t.col": "class missing"}))
    args = ["--env", "prod", "--before", str(before), "--after", str(after), "--ack-file", str(ack)]
    assert wk.main([*args, "--all-consequences", str(readers), "--all-consequences", str(unclassified)]) == 0
    # Without the unclassified check's consequences that entry is stale.
    assert wk.main([*args, "--all-consequences", str(readers)]) == 1
    assert "matches nothing: prod:unclassified:c.s.t.col" in capsys.readouterr().err


def test_cli_refuses_other_env_ack_and_bad_input(tmp_path, capsys):
    before = tmp_path / "before.json"; after = tmp_path / "after.json"; ack = tmp_path / "ack.txt"
    before.write_text("{}"); after.write_text("{}")
    ack.write_text("dev:weaken:c.s.t.col:p\n")
    args = ["--env", "prod", "--before", str(before), "--after", str(after), "--ack-file", str(ack)]
    assert wk.main(args) == 2
    assert "does not match checked env" in capsys.readouterr().err
    ack.write_text("")
    after.write_text(json.dumps({"columns": {COLUMN: {"p": {"level": "mystery", "consequence": "?"}}}}))
    before.write_text(json.dumps({"columns": {COLUMN: {"p": {"level": "redacted", "consequence": "x"}}}}))
    assert wk.main(args) == 2
    assert "unknown protection level" in capsys.readouterr().err
