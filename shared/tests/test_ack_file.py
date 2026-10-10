import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ack_file


def test_parse_every_supported_kind_and_environment():
    entries = ack_file.parse_text("""
      # reviewed exceptions
      prod:weaken:cat.sch.tab.col:etl-sp
      prod:weaken:cat.sch.tab:etl-sp
      prod:weaken:email:partial
      prod:unclassified:cat.sch.tab.col
      prod:reader:cat.sch.tab:BI users
      prod:rollback:0123456789abcdef
      prod:revoke:cat.sch.tab:old-reader
    """, "prod")
    assert [entry.key for entry in entries] == [
        "prod:weaken:cat.sch.tab.col:etl-sp",
        "prod:weaken:cat.sch.tab:etl-sp",
        "prod:weaken:email:partial",
        "prod:unclassified:cat.sch.tab.col",
        "prod:reader:cat.sch.tab:BI users",
        "prod:rollback:0123456789abcdef",
        "prod:revoke:cat.sch.tab:old-reader",
    ]


def test_environment_mismatch_is_refused():
    with pytest.raises(ack_file.AckFileError, match="does not match checked env"):
        ack_file.parse_text("dev:reader:c.s.t:p\n", "prod")


@pytest.mark.parametrize("line, message", [
    ("reader:c.s.t:p", "must begin <env>:<kind>"),
    ("prod:surprise:cat.sch.tab", "unknown acknowledgement kind"),
    ("prod:reader:cat.sch.tab.col:p", "3 valid identifier"),
    ("prod:unclassified:cat.sch.tab.col:p", "expected <env>:unclassified"),
    ("prod:rollback:not-a-sha", "7-64 hex commit"),
    ("prod:reader:cat.sch.tab:", "invalid principal"),
    ("prod:reader:cat.sch.tab:p # why", "inline comments"),
])
def test_strict_validation(line, message):
    with pytest.raises(ack_file.AckFileError, match=message):
        ack_file.parse_text(line, "prod")


def test_duplicate_entries_are_refused():
    with pytest.raises(ack_file.AckFileError, match="duplicate.*first declared"):
        ack_file.parse_text("prod:reader:c.s.t:p\nprod:reader:c.s.t:p\n", "prod")


def test_reconcile_once_accepts_entries_from_different_checks():
    entries = ack_file.parse_text(
        "prod:reader:c.s.t:p\nprod:unclassified:c.s.t.col\n", "prod"
    )
    consequences = ack_file.merge_consequences(
        {"prod:reader:c.s.t:p": "reader"},
        {"prod:unclassified:c.s.t.col": "class missing"},
    )
    matched, unmatched = ack_file.reconcile(entries, consequences)
    assert set(matched) == {entry.key for entry in entries}
    assert unmatched == ()


def test_reconcile_reports_truly_stale_entry():
    entries = ack_file.parse_text("prod:rollback:abcdef0\n", "prod")
    _, unmatched = ack_file.reconcile(entries, {})
    assert unmatched == entries


def test_cli_refuses_unmatched_ack(tmp_path, capsys):
    ack = tmp_path / "ack.txt"; ack.write_text("prod:reader:c.s.t:p\n")
    consequences = tmp_path / "consequences.json"; consequences.write_text(json.dumps({}))
    assert ack_file.main([str(ack), "--env", "prod", "--consequences", str(consequences)]) == 1
    assert "matches nothing" in capsys.readouterr().err


def test_cli_reports_wrong_environment(tmp_path, capsys):
    ack = tmp_path / "ack.txt"; ack.write_text("dev:reader:c.s.t:p\n")
    assert ack_file.main([str(ack), "--env", "prod"]) == 2
    assert "does not match checked env" in capsys.readouterr().err


def test_cli_merges_repeated_consequence_files(tmp_path, capsys):
    ack = tmp_path / "ack.txt"; ack.write_text("prod:reader:c.s.t:p\nprod:weaken:c.s.t.col:q\n")
    one = tmp_path / "one.json"; one.write_text(json.dumps({"prod:reader:c.s.t:p": "reader"}))
    two = tmp_path / "two.json"; two.write_text(json.dumps({"prod:weaken:c.s.t.col:q": "redacted -> raw"}))
    assert ack_file.main([str(ack), "--env", "prod", "--consequences", str(one), "--consequences", str(two)]) == 0
    assert ack_file.main([str(ack), "--env", "prod", "--consequences", str(one)]) == 1
    assert "matches nothing: prod:weaken:c.s.t.col:q" in capsys.readouterr().err


def test_cli_rejects_malformed_consequences(tmp_path, capsys):
    ack = tmp_path / "ack.txt"; ack.write_text("")
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps(["not", "an", "object"]))
    assert ack_file.main([str(ack), "--env", "prod", "--consequences", str(bad)]) == 2
    assert "JSON object" in capsys.readouterr().err
