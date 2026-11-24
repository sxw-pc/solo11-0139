"""Independent offline capture of synthetic audio-device samples.

Single probes stay independent device observations: ``run`` stores the raw
synthetic samples and ``summarize`` writes only per-probe peak, RMS and clip
counts (captured / silent / clipped).  Multi-channel reinforcement loop
analysis is a separate concern handled by :mod:`rightscue.pa`; probe RMS is
never used there as a feedback health score.
"""
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


def calibration_summary(db, calibration_id):
    """Per-probe-style summary of one MIMO calibration without loop verdicts.

    The loop verdict belongs to the signed wiring contract (``pa`` module);
    here we only restate which independent pulses and per-mic responses exist,
    keeping device capture separate from gain licensing.
    """
    cal = db.execute("SELECT * FROM sound_calibration WHERE id=?",
                     (calibration_id,)).fetchone()
    if not cal:
        return {"id": calibration_id, "status": "missing"}
    pulses = db.execute(
        "SELECT id,speaker_id,label,independent,conflict FROM sound_pulse "
        "WHERE calibration_id=? ORDER BY label", (calibration_id,)).fetchall()
    output = []
    for pulse in pulses:
        responses = db.execute(
            "SELECT mic_id,clipped,missing,length FROM sound_response WHERE pulse_id=?",
            (pulse["id"],)).fetchall()
        output.append({
            "id": pulse["id"],
            "speaker_id": pulse["speaker_id"],
            "label": pulse["label"],
            "independent": bool(pulse["independent"]),
            "replay_conflict": bool(pulse["conflict"]),
            "responses": [dict(r) for r in responses],
        })
    return {"id": calibration_id, "show_id": cal["show_id"], "rate": cal["rate"],
            "layout": json.loads(cal["layout_json"]),
            "independent_pulse_count": sum(1 for p in pulses if p["independent"]),
            "pulses": output,
            "status": "captured"}

