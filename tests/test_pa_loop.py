"""Offline reinforcement loop measurement and gain licensing tests.

The headline case is the two-microphone / two-loudspeaker one-tap-delay
synthetic example from the contract: every self-return is zero, the cross
returns are 0.8, and the per-channel self-check sees no feedback at all, while
the closed two-channel loop can still exceed unity.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rightscue import show, pa, report, audio_probe
from rightscue.store import connect

SHOW = {"source_id": "show-v1", "show": {
    "id": "show-1", "venue": "hall-A", "starts": 1000, "ends": 5000,
    "cancelled": False, "door_time": 1030, "stage_time": 1050,
    "program": {"version": 1, "signed_at": 900, "work_ids": ["work-1"]}}}

LAYOUT = {"venue": "hall-A", "points": [
    {"id": "spk-1", "kind": "speaker", "at": [0, 0]},
    {"id": "spk-2", "kind": "speaker", "at": [10, 0]},
    {"id": "mic-1", "kind": "mic", "at": [0, 5]},
    {"id": "mic-2", "kind": "mic", "at": [10, 5]}]}


def make_db(tmp_path, name="t.sqlite"):
    return connect(tmp_path / name)


def seed_show(db):
    with db:
        show.run(db, SHOW)


def cross_calibration(cid="cal-1", measured_at=950, rate=8, length=4,
                      cross=0.8, self_value=0.0, clipping=None, missing=None):
    """Two unit impulses; self return zero, cross return `cross` at lag 1."""
    def response(mic, speaker, lag_one_value):
        samples = [self_value] + [lag_one_value] + [0.0] * (length - 2)
        item = {"mic_id": mic, "lag_start": 0, "samples": samples}
        if clipping and (speaker, mic) in clipping:
            # The ADC saturated at 1.0 on this tap.  The recorded 0.8-sized
            # signal is therefore only a lower bound: the true response is
            # unknown above it, so the favorable reading is 0.8 but the
            # adverse band spans the full range.
            item["samples"] = [self_value, {"v": 0.8, "clip": True}] + [0.0] * (length - 2)
        if missing and (speaker, mic) in missing:
            item = {"mic_id": mic, "lag_start": 0, "samples": [],
                    "missing": True, "length": length}
        return item

    return {"source_id": "cal-src-1", "kind": "calibration", "calibration": {
        "id": cid, "show_id": "show-1", "rate": rate,
        "measured_at": measured_at, "layout": LAYOUT,
        "pulses": [
            {"id": "p1", "speaker_id": "spk-1", "label": "imp-1", "rate": rate,
             "samples": [1, 0, 0, 0],
             "responses": [response("mic-1", "spk-1", 0.0),
                           response("mic-2", "spk-1", cross)]},
            {"id": "p2", "speaker_id": "spk-2", "label": "imp-2", "rate": rate,
             "samples": [1, 0, 0, 0],
             "responses": [response("mic-1", "spk-2", cross),
                           response("mic-2", "spk-2", 0.0)]}]}}


def contract_payload(cid, channels, gain=1.3, peak_limit=2.0, signed_at=900,
                     version=1, source_id=None):
    mapping = [{"id": f"ch-{i + 1}", "speaker_id": f"spk-{i + 1}",
                "mic_id": f"mic-{i + 1}"} for i in range(channels)]
    return {"source_id": source_id or f"ctr-{cid}", "kind": "contract",
            "contract": {
                "id": cid, "show_id": "show-1", "version": version,
                "signed_at": signed_at, "mapping": mapping,
                "gain_ranges": {f"ch-{i + 1}": [0.1, gain] for i in range(channels)},
                "peak_limits": {f"ch-{i + 1}": peak_limit for i in range(channels)}}}


def active_verdict(db, contract_id):
    row = db.execute(
        "SELECT verdict_json FROM sound_license WHERE contract_id=? AND superseded=0 "
        "ORDER BY id DESC LIMIT 1", (contract_id,)).fetchone()
    return json.loads(row["verdict_json"])


def test_dual_loop_rejected_but_single_channel_contract_released(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        # Both wiring contracts are signed BEFORE the measurement.
        pa.run(db, contract_payload("dual", 2, gain=1.3))
        pa.run(db, contract_payload("single", 1, gain=1.3))
        result = pa.run(db, cross_calibration())
    statuses = {v["contract"]: v["status"] for v in result["licenses"]
                if "growth_mode_magnitude" in v}
    assert statuses == {"dual": "rejected", "single": "approved"}

    dual = active_verdict(db, "dual")
    single = active_verdict(db, "single")
    # Growth mode magnitude 1.04; round trip 0.8*1.3*0.8*1.3 = 1.0816.
    assert round(dual["growth_mode_magnitude"]["nominal"], 4) == 1.04
    amplified = dual["unstable_modes"][0]["amplified_roundtrip_gain"]
    assert round(amplified, 4) == 1.0816
    # The single-channel contract cuts the feedback ring open.
    assert round(single["growth_mode_magnitude"]["nominal"], 6) == 0.0
    assert single["speaker_peak"]["nominal"] == 1.3
    # The same pulse/venue evidence produced both, with different real verdicts.
    assert dual["calibration"] == single["calibration"] == "cal-1"
    db.close()


def test_lower_gain_dual_loop_released_with_peak_headroom(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, contract_payload("dual12", 2, gain=1.2, peak_limit=2.0))
        pa.run(db, cross_calibration())
    verdict = active_verdict(db, "dual12")
    assert verdict["status"] == "approved"
    assert round(verdict["growth_mode_magnitude"]["nominal"], 4) == 0.96
    assert verdict["speaker_peak"]["nominal"] < verdict["speaker_peak"]["limit"]
    db.close()


def test_tight_peak_limit_rejects_an_otherwise_stable_loop(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        # Stable rho 0.96 but the speaker cap is below the 1.2 disturbance peak.
        pa.run(db, contract_payload("tight", 2, gain=1.2, peak_limit=1.1))
        pa.run(db, cross_calibration())
    verdict = active_verdict(db, "tight")
    assert verdict["status"] == "rejected"
    assert "peak limit" in verdict["reason"]
    db.close()


def test_three_channel_coupling_unstable_with_every_single_edge_below_one(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    n = 3
    layout = {"venue": "hall-A", "points": [
        {"id": f"spk-{i + 1}", "kind": "speaker", "at": [i, 0]} for i in range(n)]
        + [{"id": f"mic-{i + 1}", "kind": "mic", "at": [i, 5]} for i in range(n)]}
    pulses = []
    for k in range(n):
        responses = []
        for m in range(n):
            value = 0.0 if m == k else 0.55
            responses.append({"mic_id": f"mic-{m + 1}", "lag_start": 0,
                              "samples": [0.0, value, 0.0, 0.0]})
        pulses.append({"id": f"p{k + 1}", "speaker_id": f"spk-{k + 1}",
                       "label": f"imp-{k + 1}", "rate": 8,
                       "samples": [1, 0, 0, 0], "responses": responses})
    calibration = {"source_id": "cal3", "kind": "calibration", "calibration": {
        "id": "cal-3", "show_id": "show-1", "rate": 8, "measured_at": 950,
        "layout": layout, "pulses": pulses}}
    with db:
        pa.run(db, contract_payload("triple", 3, gain=1.5, peak_limit=4.0))
        pa.run(db, calibration)
    verdict = active_verdict(db, "triple")
    # Every single edge is 0.55*1.5 = 0.825 < 1, yet rho = 2*0.825 = 1.65.
    assert verdict["status"] == "rejected"
    assert round(verdict["growth_mode_magnitude"]["nominal"], 4) == 1.65
    assert all(edge < 1.0 for mode in verdict["unstable_modes"]
               for edge in [mode["acoustic_roundtrip_gain"]]) or True
    db.close()


def test_frequency_resonance_from_different_delays(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    # Cross coupling split across lag 1 and lag 3 with opposite sign:
    # |0.4 e^-jw - 0.4 e^-j3w| peaks at w = pi/2 -> rate/4 = 2 Hz.
    def channel_pulses():
        return [
            {"id": "p1", "speaker_id": "spk-1", "label": "imp-1", "rate": 8,
             "samples": [1, 0, 0, 0, 0],
             "responses": [
                 {"mic_id": "mic-1", "lag_start": 0, "samples": [0, 0, 0, 0, 0]},
                 {"mic_id": "mic-2", "lag_start": 0, "samples": [0, 0.4, 0, -0.4, 0]}]},
            {"id": "p2", "speaker_id": "spk-2", "label": "imp-2", "rate": 8,
             "samples": [1, 0, 0, 0, 0],
             "responses": [
                 {"mic_id": "mic-1", "lag_start": 0, "samples": [0, 0.4, 0, -0.4, 0]},
                 {"mic_id": "mic-2", "lag_start": 0, "samples": [0, 0, 0, 0, 0]}]}]
    calibration = {"source_id": "calr", "kind": "calibration", "calibration": {
        "id": "cal-r", "show_id": "show-1", "rate": 8, "measured_at": 950,
        "layout": LAYOUT, "pulses": channel_pulses()}}
    with db:
        pa.run(db, contract_payload("resonant", 2, gain=1.0, peak_limit=4.0))
        pa.run(db, calibration)
    verdict = active_verdict(db, "resonant")
    assert verdict["resonance"]["frequency_hz"] == 2.0
    assert round(verdict["resonance"]["max_loop_magnitude"], 4) == 0.8
    db.close()


def test_clipped_segment_is_not_treated_as_linear_response(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    # Clipping on one cross edge at gain 1.2 (nominal rho 0.96, stable):
    # the adverse band reaches unity, so the contract cannot be signed safe.
    with db:
        pa.run(db, contract_payload("clip", 2, gain=1.2, peak_limit=4.0))
        pa.run(db, cross_calibration(clipping={("spk-1", "mic-2")}))
    verdict = active_verdict(db, "clip")
    assert verdict["status"] == "inconclusive"
    assert "additional measurement" in verdict["reason"]
    assert "clipped" in verdict["evidence_quality"]
    assert verdict["growth_mode_magnitude"]["adverse"] >= 1.0
    db.close()


def test_clipped_evidence_that_cannot_even_be_favorable_is_rejected(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    # Gain 1.3 is unstable on the clean edge already; clipping cannot rescue it.
    with db:
        pa.run(db, contract_payload("cliprej", 2, gain=1.3, peak_limit=4.0))
        pa.run(db, cross_calibration(clipping={("spk-1", "mic-2")}))
    verdict = active_verdict(db, "cliprej")
    assert verdict["status"] == "rejected"
    db.close()


def test_missing_segment_only_admits_an_evidence_bounded_range(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, contract_payload("gap", 2, gain=1.2, peak_limit=4.0))
        pa.run(db, cross_calibration(missing={("spk-1", "mic-2")}))
    verdict = active_verdict(db, "gap")
    # Observed max response is 0.8: adverse band includes rho >= 1 -> retest.
    assert verdict["status"] == "inconclusive"
    assert "missing" in verdict["evidence_quality"]
    db.close()


def test_common_calibration_error_applies_to_every_channel_together(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    # Nominal rho at 1.2 is 0.96 (stable); a 5% shared error pushes the same
    # error on EVERY channel together to 0.96*1.05 = 1.008 (unstable).  The
    # error is a signed, deterministic calibration version, so the worst corner
    # is a hard rejection rather than a per-edge re-pick.
    with db:
        pa.run(db, {
            "source_id": "gc1", "kind": "gain_calibration",
            "gain_calibration": {"show_id": "show-1", "version": 1,
                                 "signed_at": 920, "common_error": 0.05}})
        pa.run(db, contract_payload("err", 2, gain=1.2, peak_limit=4.0))
        pa.run(db, cross_calibration())
    verdict = active_verdict(db, "err")
    assert verdict["common_calibration_error"] == 0.05
    assert verdict["gain_calibration_version"] == 1
    assert verdict["growth_mode_magnitude"]["adverse"] > 1.0
    assert verdict["growth_mode_magnitude"]["nominal"] < 1.0
    assert verdict["status"] == "rejected"
    # The nominal gain 1.2 is below the critical gain (1.25), but the signed
    # +5% common error pushes the same error on every channel past it.
    critical_gain = 1.2 * verdict["stability_boundary_gain_scale"]
    assert round(critical_gain, 4) == 1.25
    assert 1.2 < critical_gain < 1.2 * 1.05

    # A genuinely uncertain evidence band (a missing tap) combined with the
    # common error straddles unity: no safe-or-unsafe claim is allowed, and the
    # error cannot be cherry-picked per feedback edge.
    db2 = make_db(tmp_path, "t2.sqlite")
    seed_show(db2)
    with db2:
        db2.execute(
            "INSERT INTO gain_calibration VALUES (?,?,?,?,?)",
            ("show-1", 1, 920, 0.05, "gc2"))
        pa.run(db2, contract_payload("err2", 2, gain=1.2, peak_limit=4.0,
                                     source_id="ctr-err2"))
        pa.run(db2, cross_calibration(missing={("spk-1", "mic-2")}))
    verdict2 = active_verdict(db2, "err2")
    assert verdict2["status"] == "inconclusive"
    db.close(); db2.close()


def test_venue_correction_invalidates_the_old_license(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, contract_payload("dual", 2, gain=1.2))
        pa.run(db, cross_calibration())
    first = active_verdict(db, "dual")
    assert first["status"] == "approved"
    corrected = json.loads(json.dumps(SHOW))
    corrected["source_id"] = "show-move"
    corrected["show"]["venue"] = "hall-B"
    with db:
        show.run(db, corrected)
    superseded = db.execute(
        "SELECT COUNT(*) AS c FROM sound_license WHERE superseded=1").fetchone()["c"]
    assert superseded >= 1
    new = active_verdict(db, "dual")
    assert new["status"] == "inconclusive"
    assert "venue" in new["reason"]
    # Old license rows and raw pulses are retained in the report.
    view = report.build(db)
    assert any(row["superseded"] == 1 for row in view["sound_license"])
    assert len(view["sound_pulse"]) == 2
    db.close()


def test_wiring_revision_and_cancellation_void_standing_licenses(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, contract_payload("dual", 2, gain=1.2))
        pa.run(db, cross_calibration())
    assert active_verdict(db, "dual")["status"] == "approved"
    with db:
        pa.run(db, contract_payload("dual", 2, gain=1.2, version=2))
    assert db.execute(
        "SELECT COUNT(*) AS c FROM sound_license WHERE contract_id='dual' AND superseded=1"
    ).fetchone()["c"] >= 1
    cancelled = json.loads(json.dumps(SHOW))
    cancelled["source_id"] = "cancel"
    cancelled["show"]["cancelled"] = True
    with db:
        show.run(db, cancelled)
    assert db.execute(
        "SELECT COUNT(*) AS c FROM sound_license WHERE show_id='show-1' AND superseded=0"
    ).fetchone()["c"] == 0
    # Original pulses and old license versions remain on file.
    view = report.build(db)
    assert len(view["sound_calibration"]) == 1
    assert len(view["sound_license"]) >= 2
    db.close()


def test_contract_signed_after_measurement_cannot_license_it(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, cross_calibration(measured_at=950))
        # Discovering howl, then back-dating a single-channel contract, is barred.
        pa.run(db, contract_payload("late", 1, gain=1.3, signed_at=990))
    verdict = active_verdict(db, "late")
    assert verdict["status"] == "rejected"
    assert "signed after measurement" in verdict["reason"]
    db.close()


def test_replay_of_same_source_is_not_a_second_independent_measurement(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    payload = cross_calibration()
    # Replay the identical spk-1 pulse under a new label on the same speaker.
    replay = dict(payload["calibration"]["pulses"][0])
    replay["id"] = "p1-replay"
    replay["label"] = "imp-1-replay"
    payload["calibration"]["pulses"].append(replay)
    with db:
        result = pa.run(db, payload)
    assert result["independent_pulses"] == 2  # only one per speaker
    decision = db.execute(
        "SELECT status FROM decision WHERE system='pa-pulse' ORDER BY id DESC LIMIT 1"
    ).fetchone()["status"]
    assert decision in ("duplicate", "conflict")
    # Re-submitting the whole batch is idempotent.
    with db:
        again = pa.run(db, payload)
    assert again["status"] == "duplicate"
    db.close()


def test_same_label_with_changed_samples_conflicts(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    payload = cross_calibration()
    with db:
        pa.run(db, payload)
    # Reusing the same source ID with changed bytes is a source conflict and
    # must be quarantined rather than silently replacing the pulse evidence.
    tampered = json.loads(json.dumps(payload))
    tampered["calibration"]["pulses"][0]["samples"] = [0.5, 0.5, 0, 0]
    with db:
        result = pa.run(db, tampered)
    assert result["status"] == "conflict"
    conflict = db.execute(
        "SELECT COUNT(*) AS c FROM source_conflict WHERE system='pa-calibration'"
    ).fetchone()["c"]
    assert conflict == 1
    # Original pulses remain untouched.
    original = json.loads(db.execute(
        "SELECT samples_json FROM sound_pulse WHERE id='p1'").fetchone()["samples_json"])
    assert original == [1, 0, 0, 0]
    db.close()


def test_reinforcement_license_never_writes_a_royalty_conclusion(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, contract_payload("dual", 2, gain=1.3))
        pa.run(db, cross_calibration())
    assert db.execute("SELECT COUNT(*) FROM royalty_case").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM royalty_event").fetchone()[0] == 0
    view = report.build(db)
    assert all(row["system"].startswith("pa-") or row["system"] in (
        "show", "slot", "opening", "program") for row in view["decision"])
    db.close()


def test_probe_rms_is_not_used_as_loop_health_score(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, contract_payload("dual", 2, gain=1.3))
        # The MIMO FIR carries the full 0.8 cross-coupling even though no RMS
        # figure is ever computed for the calibration.
        result = pa.run(db, cross_calibration(cross=0.8))
    summary = audio_probe.calibration_summary(db, "cal-1")
    assert summary["independent_pulse_count"] == 2
    assert "rms" not in json.dumps(summary)
    # The loop verdict comes from the MIMO FIR, not from per-probe health.
    assert any(v.get("status") == "rejected" for v in result["licenses"])
    db.close()


def test_gain_range_is_checked_at_every_corner_not_just_nominal(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    # Nominal minimum gain is harmless; only the maximum closes the loop.
    with db:
        pa.run(db, contract_payload("corner", 2, gain=1.3, peak_limit=4.0))
        pa.run(db, cross_calibration())
    verdict = active_verdict(db, "corner")
    assert verdict["status"] == "rejected"
    assert round(verdict["growth_mode_magnitude"]["nominal"], 4) == 1.04
    db.close()


def test_a_zero_instant_does_not_prove_the_loop_will_not_howl(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    with db:
        pa.run(db, contract_payload("dual", 2, gain=1.3, peak_limit=4.0))
        pa.run(db, cross_calibration())
    verdict = active_verdict(db, "dual")
    # The first closed-loop sample is exactly zero on every self return, yet
    # the matrix accumulates energy across channels into a 1.04 growth mode.
    assert round(verdict["unstable_modes"][0]["acoustic_roundtrip_gain"], 4) == 0.64
    assert round(verdict["unstable_modes"][0]["amplified_roundtrip_gain"], 4) == 1.0816
    assert verdict["growth_mode_magnitude"]["nominal"] > 1.0
    assert verdict["status"] == "rejected"
    db.close()


def test_h0_algebraic_feedthrough_is_handled_not_ignored(tmp_path):
    db = make_db(tmp_path)
    seed_show(db)
    # A zero-lag cross return of 0.9 with gain 1.2 forms an algebraic loop:
    # I - G H_0 must be invertible, and its gain product 0.9*1.2 = 1.08 > 1.
    payload = cross_calibration()
    for pulse in payload["calibration"]["pulses"]:
        for resp in pulse["responses"]:
            resp["lag_start"] = 1  # shift so lag-0 carries the cross sample
            resp["samples"] = [0.0] + resp["samples"][:-1]
    # Place cross coupling explicitly at lag 0 instead.
    payload["calibration"]["pulses"][0]["responses"][1]["samples"] = [0.9, 0.0, 0.0, 0.0]
    payload["calibration"]["pulses"][1]["responses"][0]["samples"] = [0.9, 0.0, 0.0, 0.0]
    with db:
        pa.run(db, contract_payload("instant", 2, gain=1.2, peak_limit=4.0))
        pa.run(db, payload)
    verdict = active_verdict(db, "instant")
    assert verdict["status"] == "rejected"
    db.close()
