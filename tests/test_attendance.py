import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rightscue import attendance, show
from rightscue.store import connect


def fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def test_gate_scans_are_independent_of_royalty(tmp_path):
    db = connect(tmp_path / "a.sqlite")
    with db:
        show.run(db, fixture("show-ok.json"))
        result = attendance.run(db, fixture("attendance-ok.json"))
    assert result["cases"][0]["present"] == 1
    assert db.execute("SELECT COUNT(*) FROM royalty_case").fetchone()[0] == 0
    assert attendance.run(db, fixture("attendance-ok.json"))["status"] == "duplicate"
    db.close()


def test_scans_before_show_are_reconciled_when_show_arrives(tmp_path):
    db = connect(tmp_path / "b.sqlite")
    with db:
        result = attendance.run(db, fixture("attendance-ok.json"))
    assert result["cases"][0]["status"] == "pending_show"
    with db:
        show.run(db, fixture("show-ok.json"))
    assert db.execute("SELECT status FROM attendance_case").fetchone()[0] == "recorded"
    db.close()


def test_conflicting_scan_identity_rolls_back_batch(tmp_path):
    db = connect(tmp_path / "c.sqlite")
    with db:
        attendance.run(db, fixture("attendance-ok.json"))
    altered = fixture("attendance-ok.json")
    altered["source_id"] = "gate-batch-2"
    altered["scans"][0]["ticket_id"] = "DIFFERENT"
    try:
        with db:
            attendance.run(db, altered)
    except ValueError:
        pass
    else:
        raise AssertionError("scan identity conflict should fail")
    assert db.execute("SELECT COUNT(*) FROM attendance_scan").fetchone()[0] == 3
    db.close()

