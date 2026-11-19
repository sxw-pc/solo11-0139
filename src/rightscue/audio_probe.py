"""Independent offline capture of synthetic audio-device samples."""
import json
import math
from .store import ingest, decide


def run(db, data):
    state = ingest(db, "audio", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    output = []
    for item in data["probes"]:
        samples = item["samples"]
        if not samples or item["rate"] <= 0 or any(not -1 <= x <= 1 for x in samples):
            raise ValueError("invalid normalized audio samples")
        raw = json.dumps(samples, separators=(",", ":"))
        old = db.execute("SELECT samples_json FROM audio_probe WHERE id=?", (item["id"],)).fetchone()
        if old and old["samples_json"] != raw:
            raise ValueError("same audio probe ID changed samples")
        if not old:
            db.execute(
                "INSERT INTO audio_probe VALUES (?,?,?,?,?,?,?)",
                (item["id"], item["show_id"], item["device_id"], item["starts"],
                 item["rate"], raw, data["source_id"]),
            )
        output.append(summarize(db, item["id"]))
    return {"source_id": data["source_id"], "probes": output}


def summarize(db, probe_id):
    row = db.execute("SELECT * FROM audio_probe WHERE id=?", (probe_id,)).fetchone()
    if not row:
        return {"id": probe_id, "status": "missing"}
    samples = json.loads(row["samples_json"])
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    clipped = sum(abs(value) == 1 for value in samples)
    show = db.execute("SELECT id FROM show_slot WHERE id=?", (row["show_id"],)).fetchone()
    status = "pending_show" if not show else "clipped" if clipped else "silent" if rms < 0.05 else "captured"
    db.execute(
        "INSERT INTO audio_health VALUES (?,?,?,?,?) ON CONFLICT(probe_id) DO UPDATE SET "
        "peak=excluded.peak,rms=excluded.rms,clipped=excluded.clipped,status=excluded.status",
        (probe_id, peak, rms, clipped, status),
    )
    decide(db, "audio-health", probe_id, status, f"peak {peak:.3f}, rms {rms:.3f}", [row["source_id"]])
    return {"id": probe_id, "peak": peak, "rms": rms, "clipped": clipped, "status": status}

