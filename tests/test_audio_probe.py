import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rightscue import audio_probe, show
from rightscue.store import connect


def fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def test_audio_capture_independent_of_royalty(tmp_path):
    db = connect(tmp_path / "a.sqlite")
    with db:
        show.run(db, fixture("show-ok.json"))
        result = audio_probe.run(db, fixture("audio-ok.json"))
    assert [p["status"] for p in result["probes"]] == ["captured", "clipped"]
    assert db.execute("SELECT COUNT(*) FROM royalty_case").fetchone()[0] == 0
    assert audio_probe.run(db, fixture("audio-ok.json"))["status"] == "duplicate"
    db.close()


def test_audio_before_show_is_reclassified(tmp_path):
    db = connect(tmp_path / "b.sqlite")
    with db:
        result = audio_probe.run(db, fixture("audio-ok.json"))
    assert result["probes"][0]["status"] == "pending_show"
    with db:
        show.run(db, fixture("show-ok.json"))
    assert db.execute("SELECT status FROM audio_health WHERE probe_id='ap-1'").fetchone()[0] == "captured"
    db.close()


def test_invalid_samples_roll_back_batch(tmp_path):
    db = connect(tmp_path / "c.sqlite")
    altered = fixture("audio-ok.json")
    altered["probes"][1]["samples"] = [2]
    try:
        with db:
            audio_probe.run(db, altered)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid sample should fail")
    assert db.execute("SELECT COUNT(*) FROM audio_probe").fetchone()[0] == 0
    db.close()

