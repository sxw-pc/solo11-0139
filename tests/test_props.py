from pathlib import Path

from rightscue import props, report, show
from rightscue.store import connect, read


ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def test_direct_prop_pack_and_replay(tmp_path):
    db = connect(tmp_path / "props.sqlite")
    data = read(ROOT / "props-ok.json")
    with db:
        show.run(db, read(ROOT / "show-ok.json"))
        assert props.run(db, data)["status"] == "ready"
        assert props.run(db, data)["status"] == "duplicate"
    assert len(report.build(db)["prop_scan"]) == 2


def test_missing_direct_barcode(tmp_path):
    db = connect(tmp_path / "props.sqlite")
    data = read(ROOT / "props-ok.json")
    data["scans"] = data["scans"][:1]
    with db:
        show.run(db, read(ROOT / "show-ok.json"))
        assert props.run(db, data)["missing"] == ["banner-4"]
