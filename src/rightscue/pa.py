"""Offline multi-channel reinforcement loop measurement and gain licensing.

A sound field calibration records, for one show and one venue layout, which
loudspeaker group emitted each independent test pulse and the per-sample
response captured by each microphone.  The pulses together form a
multi-input/multi-output finite impulse response (MIMO FIR); individual probe
RMS values are never treated as independent device-health scores for loop
purposes.

A signed wiring contract fixes, before measurement, which channels are
actually enabled and the contracted gain range.  For every enabled channel and
the whole licensed gain range (including one common device gain calibration
error shared by all channels) the closed-loop delayed system must be stable
and the loudspeaker peak limits must hold.  A loop is only released when both
hold under every admissible interpretation of the evidence.
"""
import hashlib
import json
import math

from .store import decide, ingest


# --------------------------------------------------------------------------
# Linear algebra: characteristic polynomial -> roots -> spectral radius.
# Pure standard library, matrices are small (n channels x n channels x L taps).
# --------------------------------------------------------------------------
def _mat_mul(a, b):
    n, m, k = len(a), len(b), len(b[0])
    return [[sum(a[i][t] * b[t][j] for t in range(m)) for j in range(k)] for i in range(n)]


def _mat_add(a, b):
    return [[a[i][j] + b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def _identity(n):
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def characteristic_polynomial(a):
    """Faddeev-LeVerrier: det(x I - A) coefficients, highest degree first."""
    n = len(a)
    m = [row[:] for row in a]
    c = [1.0]
    for k in range(1, n + 1):
        ck = -sum(m[i][i] for i in range(n)) / k
        c.append(ck)
        if k < n:
            m = _mat_mul(a, [[m[i][j] + (ck if i == j else 0.0)
                              for j in range(n)] for i in range(n)])
    return c


def _poly_eval(coeffs, z):
    out = 0.0j
    for c in coeffs:
        out = out * z + c
    return out


def _poly_roots(coeffs):
    """Durand-Kerner roots of a monic-ish real polynomial."""
    coeffs = [float(c) for c in coeffs]
    lead = coeffs[0]
    if lead == 0:
        raise ValueError("degenerate polynomial")
    coeffs = [c / lead for c in coeffs]
    degree = len(coeffs) - 1
    if degree == 0:
        return []
    radius = 1.01 * max((abs(c) ** (1.0 / (degree - k))
                         for k, c in enumerate(coeffs[:-1]) if c != 0), default=1.0)
    roots = [radius * complex(0.4 + 0.9 * math.cos(2 * math.pi * k / degree),
                               0.9 * math.sin(2 * math.pi * k / degree))
             for k in range(degree)]
    for _ in range(2000):
        moved = 0.0
        for i in range(degree):
            denom = 1.0 + 0.0j
            for j in range(degree):
                if j != i:
                    denom *= roots[i] - roots[j]
            if abs(denom) < 1e-18:
                break
            correction = _poly_eval(coeffs, roots[i]) / denom
            roots[i] -= correction
            moved = max(moved, abs(correction))
        if moved < 1e-12:
            break
    return roots


def spectral_radius(matrix):
    """Largest eigenvalue magnitude of a real square matrix."""
    n = len(matrix)
    if n == 0:
        return 0.0
    if n == 1:
        return abs(matrix[0][0])
    # Cheap power iteration first; KL polynomial method as the exact path.
    try:
        roots = _poly_roots(characteristic_polynomial(matrix))
        return max(abs(z) for z in roots)
    except Exception:
        x = [1.0] * n
        lam = 0.0
        for _ in range(4000):
            y = [sum(matrix[i][j] * x[j] for j in range(n)) for i in range(n)]
            norm = math.sqrt(sum(v * v for v in y)) or 1e-18
            x = [v / norm for v in y]
            new_lam = math.sqrt(sum(v * v for v in y))
            if abs(new_lam - lam) < 1e-13:
                break
            lam = new_lam
        return lam


# --------------------------------------------------------------------------
# Closed-loop delayed system.
#
# Mic signals are x, loudspeaker drive signals are u = G x + disturbance.
# The acoustic MIMO response is u_mic = sum_l H_l * u_spk delayed by l
# samples, so the state transition matrix is the block companion
# A = [[G H_1, G H_2, ..., G H_L], [I, 0, ...], ...].  Stability requires
# rho(A) < 1.  H_0 is an algebraic (zero-lag) feedthrough term.
# --------------------------------------------------------------------------
def _block_companion(blocks):
    """blocks = [H_1, ..., H_L] already scaled by the gain vector.

    State ordering s = [z_{t-1}, z_{t-2}, ..., z_{t-L}] (each of width n):
        z_t     = H_1 z_{t-1} + H_2 z_{t-2} + ... + H_L z_{t-L}
        z_{t-k} shifts into the next block row.
    """
    lags = len(blocks)
    n = len(blocks[0])
    if lags == 1:
        return [row[:] for row in blocks[0]]
    size = n * lags
    a = [[0.0] * size for _ in range(size)]
    # Top block row: H_l sits in block column l-1.
    for li, block in enumerate(blocks):
        for i in range(n):
            for j in range(n):
                a[i][li * n + j] = block[i][j]
    # Subdiagonal identity blocks: block row li receives block row li+1
    # (the older state shifts up one position each step).
    for li in range(lags - 1):
        for i in range(n):
            a[(li + 1) * n + i][li * n + i] = 1.0
    return a


def _scale(blocks, gains):
    # H[i][j]: row i is the receiving microphone (amplifier input i),
    # column j the emitting loudspeaker j.  Channel i's amplifier scales row i.
    out = []
    for h in blocks:
        n = len(h)
        out.append([[h[i][j] * gains[i] for j in range(n)] for i in range(n)])
    return out


def _select_blocks(taps, selector):
    """taps[l][i][j] -> dict(value, lo, hi, quality); selector chooses value."""
    out = []
    for layer in taps:
        out.append([[selector(layer[i][j]) for j in range(len(layer))]
                    for i in range(len(layer))])
    return out


def closed_loop_radius(taps, gains, h0=None, selector=None):
    selector = selector or (lambda cell: cell["value"])
    blocks = _scale(_select_blocks(taps, selector), gains)
    h0_numeric = _select_blocks([h0], selector)[0] if h0 is not None else None
    # Zero-lag algebraic term: (I - G H_0) x = ... requires I - G H_0
    # invertible with the inverse closed loop remaining contractive.
    if h0_numeric is not None:
        n = len(gains)
        gh0 = _scale([h0_numeric], gains)[0]
        eye = _identity(n)
        det0 = _det2(eye, gh0) if n == 2 else _det_generic(
            [[eye[i][j] - gh0[i][j] for j in range(n)] for i in range(n)])
        if abs(det0) < 1e-15:
            return math.inf
        factor = 1.0 / abs(det0) ** (1.0 / n)
    else:
        factor = 1.0
    return factor * spectral_radius(_block_companion(blocks))


def _det_generic(m):
    n = len(m)
    a = [row[:] for row in m]
    det = 1.0
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-18:
            return 0.0
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
            det = -det
        piv = a[col][col]
        det *= piv
        for r in range(col + 1, n):
            q = a[r][col] / piv
            for c in range(col, n):
                a[r][c] -= q * a[col][c]
    return det


def _det2(eye, gh0):
    m = [[eye[i][j] - gh0[i][j] for j in range(2)] for i in range(2)]
    return m[0][0] * m[1][1] - m[0][1] * m[1][0]


def peak_response(taps, gains, selector=None, horizon=128, h0=None):
    """Worst-case |loudspeaker drive| after a unit disturbance at any mic.

    Drive recursion (blocks are already G-scaled):
        u_t = G d_t + sum_l (G H_l) u_{t-l}
    The maximal |u_t[i]| over disturbance channels and time must stay under
    the contracted loudspeaker peak limit.  An unstable loop exceeds every
    finite limit, so growth is capped instead of iterating to overflow.
    """
    cap = 1.0e6
    selector = selector or (lambda cell: cell["value"])
    blocks = _scale(_select_blocks(taps, selector), gains)
    lags = len(blocks)
    n = len(gains)
    h0_numeric = _select_blocks([h0], selector)[0] if h0 is not None else None
    gh0 = _scale([h0_numeric], gains)[0] if h0_numeric is not None else None
    worst = 1.0
    for d in range(n):
        history = [[0.0] * n for _ in range(lags)]  # u_{t-1}, ..., u_{t-L}
        settled = 0
        previous_worst = worst
        for step in range(horizon):
            drive = [0.0] * n
            for li, block in enumerate(blocks):
                past = history[li]
                for i in range(n):
                    drive[i] += sum(block[i][j] * past[j] for j in range(n))
            if gh0 is not None and history:
                for i in range(n):
                    drive[i] += sum(gh0[i][j] * history[0][j] for j in range(n))
            if step == 0:
                drive[d] += gains[d]  # one unit disturbance at amplifier d
            peak = max(abs(v) for v in drive)
            if peak > worst:
                worst = peak
                settled = 0
            else:
                settled += 1
            # A stable impulse response has decayed once the peak stops
            # growing for a full system memory; only a genuinely unstable
            # interpretation keeps growing to the cap.
            if settled > lags * n * 2:
                break
            history = [drive] + history[:-1]
        if worst >= cap:
            return cap
    return worst


def resonance_scan(taps, gains, rate, selector=None, bins=256):
    """Per-frequency loop magnitude over [0, rate/2]: max rho(G H(e^{jw}))."""
    selector = selector or (lambda cell: cell["value"])
    blocks = _scale(_select_blocks(taps, selector), gains)
    n = len(gains)
    half = bins // 2
    peaks = []
    for b in range(half + 1):
        omega = 2 * math.pi * b / bins
        h = [[0.0j] * n for _ in range(n)]
        for li, block in enumerate(blocks, start=1):
            phase = math.cos(-omega * li) + 1j * math.sin(-omega * li)
            for i in range(n):
                for j in range(n):
                    h[i][j] += block[i][j] * phase
        mag = spectral_radius([[abs(v) for v in row] for row in h])
        peaks.append((mag, rate * b / bins))
    mag, freq = max(peaks, key=lambda item: item[0])
    return {"max_loop_magnitude": mag, "frequency_hz": freq, "rate": rate}


def simple_cycles(edges, threshold=1.0):
    """Simple directed cycles on n channels; edges[i][j] is the j->i magnitude.

    Every reported cycle contains at least two distinct channels; a self-loop
    edge is handled separately by the one-step spectral-radius check.
    """
    n = len(edges)
    found = []

    def dfs(start, node, visited, product):
        for j in range(n):
            w = edges[node][j]
            if w == 0.0:
                continue
            if j == start:
                if len(visited) >= 2:  # at least one other channel visited
                    found.append({"loop": visited + [start],
                                  "roundtrip_gain": product * w})
            elif j not in visited:
                dfs(start, j, visited + [j], product * w)

    for s in range(n):
        dfs(s, s, [s], 1.0)
    # De-duplicate rotations of the same cycle, keeping the smallest start.
    unique = {}
    for cycle in found:
        loop = cycle["loop"]
        key = tuple(loop[:-1])
        rotations = [tuple(loop[k:-1] + loop[:k]) for k in range(len(key))]
        canonical = min(rotations)
        unique.setdefault(canonical, cycle)
    return [c for c in unique.values() if c["roundtrip_gain"] >= threshold]


# --------------------------------------------------------------------------
# Ingestion: calibration sessions, wiring contracts, gain calibration.
# --------------------------------------------------------------------------
def _digest(samples):
    raw = json.dumps(samples, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def run_calibration(db, data):
    state = ingest(db, "pa-calibration", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    cal = data["calibration"]
    cid = cal["id"]
    rate = int(cal["rate"])
    if rate <= 0:
        raise ValueError("invalid sample rate")
    layout = cal["layout"]
    if not isinstance(layout, dict) or not layout.get("points") or not layout.get("venue"):
        raise ValueError("venue layout requires venue id and measurement points")
    next_ingested = 1 + (db.execute("SELECT COALESCE(MAX(ingested_at),0) AS m "
                                    "FROM sound_calibration").fetchone()["m"])
    db.execute("INSERT INTO sound_calibration VALUES (?,?,?,?,?,?,?)",
               (cid, cal["show_id"], rate, int(cal["measured_at"]),
                json.dumps(layout, sort_keys=True), next_ingested, data["source_id"]))
    seen_pulse_ids = set()
    for pulse in cal["pulses"]:
        if pulse["id"] in seen_pulse_ids:
            raise ValueError("duplicate pulse id within calibration: " + pulse["id"])
        seen_pulse_ids.add(pulse["id"])
        samples = pulse["samples"]
        if not samples or any(not -1 <= float(x) <= 1 for x in samples):
            raise ValueError("invalid normalized pulse samples")
        if int(pulse["rate"]) != rate:
            raise ValueError("all pulses in a calibration share one sample rate")
        digest = _digest(samples)
        label = pulse["label"]
        same_label = db.execute(
            "SELECT speaker_id,digest,independent FROM sound_pulse WHERE calibration_id=? AND label=?",
            (cid, label)).fetchone()
        if same_label:
            # A replay of the same source under the same label is not a second
            # independent measurement; identical label with changed bytes is a
            # source conflict.
            if same_label["digest"] != digest:
                raise ValueError("same pulse label changed samples")
            decide(db, "pa-pulse", f"{cid}:{label}", "conflict",
                   "same source replay cannot count as an independent measurement",
                   [cid, label, data["source_id"]])
            continue
        independent = 1
        dup = db.execute(
            "SELECT id FROM sound_pulse WHERE calibration_id=? AND digest=? AND speaker_id=?",
            (cid, digest, pulse["speaker_id"])).fetchone()
        if dup:
            independent = 0
            decide(db, "pa-pulse", f"{cid}:{label}", "duplicate",
                   "same-source replay on one speaker is not an independent measurement",
                   [cid, label, dup["id"], data["source_id"]])
        db.execute(
            "INSERT INTO sound_pulse(id,calibration_id,speaker_id,label,digest,samples_json,rate,"
            "independent,conflict,source_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pulse["id"], cid, pulse["speaker_id"], label, digest,
             json.dumps(samples, separators=(",", ":")), rate, independent, 0,
             data["source_id"]))
        for resp in pulse["responses"]:
            raw_samples = resp.get("samples")
            clipped_flag = int(bool(resp.get("clipped", False)))
            missing_flag = int(bool(resp.get("missing", False)))
            if raw_samples is None:
                raw_samples = []
            # Per-sample clip markers {"v": x, "clip": true} describe exactly
            # which taps saturated; a plain number is a linear sample.
            normalized, per_sample_clip = [], []
            for entry in raw_samples:
                if isinstance(entry, dict):
                    normalized.append(float(entry["v"]))
                    per_sample_clip.append(1 if entry.get("clip") else 0)
                else:
                    normalized.append(float(entry))
                    per_sample_clip.append(0)
            if any(not -1 <= x <= 1 for x in normalized):
                raise ValueError("invalid normalized response samples")
            if any(per_sample_clip):
                clipped_flag = 1
            db.execute(
                "INSERT INTO sound_response(pulse_id,mic_id,lag_start,samples_json,clipped,missing,"
                "length,source_id) VALUES (?,?,?,?,?,?,?,?)",
                (pulse["id"], resp["mic_id"], int(resp["lag_start"]),
                 json.dumps(normalized, separators=(",", ":")), clipped_flag, missing_flag,
                 int(resp.get("length", len(normalized))), data["source_id"]))
            if per_sample_clip:
                for offset, is_clip in enumerate(per_sample_clip):
                    if is_clip:
                        db.execute(
                            "INSERT INTO sound_response_clip VALUES (?,?,?,?)",
                            (pulse["id"], resp["mic_id"],
                             int(resp["lag_start"]) + offset, data["source_id"]))
    # Per-mic evidence bounds for missing segments; a calibration-wide bound
    # covers a microphone whose own other returns happen to be silent.
    mic_bounds = {}
    global_bound = 0.0
    for row in db.execute(
            "SELECT r.mic_id AS mic_id, r.samples_json AS samples_json "
            "FROM sound_response r JOIN sound_pulse p ON p.id=r.pulse_id "
            "WHERE p.calibration_id=?", (cid,)).fetchall():
        vals = [abs(float(x)) for x in json.loads(row["samples_json"])]
        if vals:
            local = max(vals)
            mic_bounds[row["mic_id"]] = max(mic_bounds.get(row["mic_id"], 0.0), local)
            global_bound = max(global_bound, local)
    _build_fir(db, cid, data["source_id"], mic_bounds, global_bound)
    decide(db, "pa-calibration", cid, "ingested",
           "MIMO impulse evidence retained; FIR built from independent pulses",
           [cid, data["source_id"]])
    licenses = reevaluate_show(db, cal["show_id"], data["source_id"])
    return {"source_id": data["source_id"], "calibration": cid,
            "independent_pulses": db.execute(
                "SELECT COUNT(*) AS c FROM sound_pulse WHERE calibration_id=? AND independent=1",
                (cid,)).fetchone()["c"],
            "licenses": licenses}


def _build_fir(db, cid, source_id, mic_bounds, global_bound=0.0):
    def bound_for(mic):
        return max(mic_bounds.get(mic, 0.0), global_bound)
    cal = db.execute("SELECT * FROM sound_calibration WHERE id=?", (cid,)).fetchone()
    rows = db.execute(
        "SELECT p.id AS id, p.speaker_id AS speaker_id, p.independent AS independent, "
        "r.mic_id AS mic_id, r.lag_start AS lag_start, r.samples_json AS samples_json, "
        "r.clipped AS clipped, r.missing AS missing, r.length AS length "
        "FROM sound_pulse p JOIN sound_response r ON r.pulse_id=p.id "
        "WHERE p.calibration_id=? ORDER BY p.speaker_id, r.mic_id, r.lag_start",
        (cid,)).fetchall()
    clip_rows = db.execute(
        "SELECT pulse_id,mic_id,lag FROM sound_response_clip").fetchall()
    clip_set = {(r["pulse_id"], r["mic_id"], r["lag"]) for r in clip_rows}
    grouped = {}
    for row in rows:
        key = (row["speaker_id"], row["mic_id"])
        grouped.setdefault(key, []).append(row)
    for (speaker, mic), entries in grouped.items():
        # Only independent pulses may contribute; copies of the same source
        # must never be averaged in as a second independent measurement.
        independent = [e for e in entries if e["independent"]]
        if not independent:
            continue
        by_lag = {}
        for e in independent:
            pulse_amp = max(abs(float(x)) for x in json.loads(
                db.execute("SELECT samples_json FROM sound_pulse WHERE id=?",
                           (e["id"],)).fetchone()["samples_json"])) or 1.0
            vals = json.loads(e["samples_json"])
            if e["missing"]:
                # The whole declared segment is absent: every tap is only
                # known within the per-mic observed evidence bound.
                bound = bound_for(mic)
                for k in range(e["length"]):
                    by_lag.setdefault(e["lag_start"] + k, []).append(
                        {"value": 0.0, "clipped": 0, "missing": 1, "bound": bound})
                continue
            for offset, raw in enumerate(vals):
                lag = e["lag_start"] + offset
                # Clipping is per-tap: only saturated samples lose their
                # linear-response status, neighbouring taps stay accurate.
                tap_clipped = 1 if (e["id"], mic, lag) in clip_set else 0
                by_lag.setdefault(lag, []).append(
                    {"value": float(raw) / pulse_amp, "clipped": tap_clipped,
                     "missing": 0, "bound": bound_for(mic)})
        for lag, observations in sorted(by_lag.items()):
            measured = [o for o in observations if not o["clipped"] and not o["missing"]]
            clipped = any(o["clipped"] for o in observations)
            missing = any(o["missing"] for o in observations)
            if clipped and not measured:
                # This tap itself saturated: the recorded value is only a
                # lower bound.  Favorable keeps the measured magnitude (the
                # true response cannot be smaller than what was captured),
                # adverse spans the full normalized range.
                value = sum(o["value"] for o in observations) / len(observations)
                lo, hi = -1.0, 1.0
                quality = "clipped"
            elif missing and not measured:
                bound = max((o["bound"] for o in observations if o["missing"]), default=0.0)
                value = 0.0
                lo, hi = -bound, bound
                quality = "missing"
            else:
                value = sum(o["value"] for o in measured) / len(measured)
                lo = min(o["value"] for o in measured)
                hi = max(o["value"] for o in measured)
                quality = "measured"
            db.execute(
                "INSERT INTO sound_fir(show_id,calibration_id,speaker_id,mic_id,lag,value,lo,hi,"
                "quality,source_id) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(calibration_id,speaker_id,mic_id,lag) DO UPDATE SET "
                "value=excluded.value,lo=excluded.lo,hi=excluded.hi,quality=excluded.quality",
                (cal["show_id"], cid, speaker, mic, lag, value, lo, hi, quality, source_id))


def run_contract(db, data):
    state = ingest(db, "pa-contract", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    contract = data["contract"]
    cid = contract["id"]
    version = int(contract["version"])
    if db.execute("SELECT 1 FROM wiring_contract WHERE id=? AND version=?",
                  (cid, version)).fetchone():
        raise ValueError("same contract version resubmitted with different bytes")
    mapping = contract["mapping"]
    for ch in mapping:
        if not ch.get("speaker_id") or not ch.get("mic_id"):
            raise ValueError("each channel maps one speaker group and one microphone")
    gain_ranges = contract.get("gain_ranges", {})
    for ch_id, span in gain_ranges.items():
        if float(span[0]) < 0 or float(span[1]) < float(span[0]):
            raise ValueError("invalid contracted gain range")
    db.execute(
        "INSERT INTO wiring_contract(id,show_id,version,signed_at,mapping_json,gain_ranges_json,"
        "peak_limits_json,source_id,status) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, contract["show_id"], version, int(contract["signed_at"]),
         json.dumps(mapping, sort_keys=True), json.dumps(gain_ranges, sort_keys=True),
         json.dumps(contract.get("peak_limits", {}), sort_keys=True),
         data["source_id"], "signed"))
    decide(db, "pa-contract", f"{cid}:v{version}", "signed",
           "wiring contract signed before measurement; enabled channels and gains fixed",
           [cid, version, data["source_id"]])
    # A wiring revision invalidates every prior license for this show.
    _invalidate(db, contract["show_id"], "wiring revision " + str(version), data["source_id"])
    licenses = reevaluate_show(db, contract["show_id"], data["source_id"])
    return {"source_id": data["source_id"], "contract": cid, "version": version,
            "licenses": licenses}


def run_gain_calibration(db, data):
    state = ingest(db, "pa-gaincal", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    gc = data["gain_calibration"]
    show_id, version = gc["show_id"], int(gc["version"])
    old = db.execute("SELECT version FROM gain_calibration WHERE show_id=? ORDER BY version DESC",
                     (show_id,)).fetchone()
    if old and version <= old["version"]:
        decide(db, "pa-gaincal", show_id, "stale",
               "older or equal gain calibration version cannot supersede", [version, data["source_id"]])
        return {"source_id": data["source_id"], "status": "stale"}
    db.execute("INSERT INTO gain_calibration VALUES (?,?,?,?,?)",
               (show_id, version, int(gc["signed_at"]), float(gc.get("common_error", 0.0)),
                data["source_id"]))
    decide(db, "pa-gaincal", show_id, "effective",
           "one common device gain and white-balance calibration applies to every channel",
           [version, data["source_id"]])
    # The shared calibration version changes: old licenses cannot stand.
    _invalidate(db, show_id, "gain calibration version " + str(version), data["source_id"])
    licenses = reevaluate_show(db, show_id, data["source_id"])
    return {"source_id": data["source_id"], "status": "effective", "licenses": licenses}


def _invalidate(db, show_id, reason, source_id):
    rows = db.execute(
        "SELECT id, contract_id, contract_version FROM sound_license "
        "WHERE show_id=? AND superseded=0", (show_id,)).fetchall()
    for row in rows:
        db.execute("UPDATE sound_license SET superseded=1, reason=? WHERE id=?", (reason, row["id"]))
        decide(db, "pa-license", f"{row['contract_id']}:v{row['contract_version']}",
               "superseded", reason, [show_id, row["id"], source_id])


# --------------------------------------------------------------------------
# License evaluation over the full contracted gain range.
# --------------------------------------------------------------------------
def evaluate(db, show_id, calibration, contract, gain_version, common_error, source_id):
    show = db.execute("SELECT * FROM show_slot WHERE id=?", (show_id,)).fetchone()

    def persist(verdict, reason, status):
        db.execute(
            "INSERT INTO sound_license(show_id,calibration_id,contract_id,contract_version,"
            "gain_version,status,gain_ranges_json,verdict_json,reason,source_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (show_id, calibration["id"] if calibration else None, contract["id"],
             int(contract["version"]), int(gain_version or 0), status,
             contract["gain_ranges_json"], json.dumps(verdict, sort_keys=True),
             reason, source_id))
        decide(db, "pa-license", f"{contract['id']}:v{contract['version']}", status, reason,
               [calibration["id"] if calibration else None, contract["id"],
                int(contract["version"]), source_id])
        return verdict

    if not show:
        return persist({"contract": contract["id"], "status": "pending_show",
                        "reason": "show slot not recorded yet"},
                       "show slot not recorded yet", "pending_show")
    if show["cancelled"]:
        return persist({"contract": contract["id"], "status": "cancelled",
                        "reason": "show cancelled; reinforcement license void"},
                       "show cancelled; reinforcement license void", "cancelled")
    layout = json.loads(calibration["layout_json"])
    if layout.get("venue") != show["venue"]:
        verdict = {"contract": contract["id"], "status": "inconclusive",
                   "reason": "venue of calibration layout does not match the corrected show venue"}
        return persist(verdict, verdict["reason"], "inconclusive")
    if int(contract["signed_at"]) > int(calibration["measured_at"]):
        verdict = {"contract": contract["id"], "status": "rejected",
                   "reason": "wiring contract signed after measurement cannot license that measurement"}
        return persist(verdict, verdict["reason"], "rejected")

    mapping = json.loads(contract["mapping_json"])
    channels = [ch["id"] for ch in mapping]
    n = len(channels)
    fir = db.execute("SELECT * FROM sound_fir WHERE calibration_id=?",
                     (calibration["id"],)).fetchall()
    lookup = {(f["speaker_id"], f["mic_id"], f["lag"]): f for f in fir}

    missing_coverage = sorted({ch["speaker_id"] for ch in mapping
                               if not any(f["speaker_id"] == ch["speaker_id"] for f in fir)})
    if missing_coverage:
        reason = "no independent pulse measured for enabled speaker " + ",".join(missing_coverage)
        return persist({"contract": contract["id"], "status": "inconclusive",
                        "reason": reason}, reason, "inconclusive")

    max_lag = max((f["lag"] for f in fir), default=0)
    taps, h0, quality_flags = [], None, set()
    nonzero_lag = 0
    for lag in range(0, max_lag + 1):
        layer = []
        for ch_out in mapping:  # rows: microphone side
            row = []
            for ch_in in mapping:  # cols: speaker side
                f = lookup.get((ch_in["speaker_id"], ch_out["mic_id"], lag))
                if f is None:
                    row.append({"value": 0.0, "lo": 0.0, "hi": 0.0,
                                "fav": 0.0, "quality": "measured"})
                else:
                    # For a saturated (clipped) tap the measured magnitude is a
                    # lower bound on the truth, so the favorable interpretation
                    # is the measured value, never a smaller invented number.
                    fav_value = f["value"] if f["quality"] == "clipped" else min(
                        abs(f["lo"]), abs(f["hi"])) * (1 if f["value"] >= 0 else -1)
                    cell = {"value": f["value"], "lo": f["lo"], "hi": f["hi"],
                            "fav": fav_value, "quality": f["quality"]}
                    quality_flags.add(f["quality"])
                    row.append(cell)
            layer.append(row)
        if lag == 0:
            h0 = layer
        else:
            taps.append(layer)
            if any(c["value"] != 0.0 or c["hi"] != 0.0 or c["lo"] != 0.0 for c in sum(layer, [])):
                nonzero_lag = max(nonzero_lag, lag)
    if not taps:
        taps = [[[{"value": 0.0, "lo": 0.0, "hi": 0.0, "fav": 0.0,
                   "quality": "measured"} for _ in range(n)] for _ in range(n)]]
    effective_memory = max(nonzero_lag, 1)

    def nominal(cell):
        return cell["value"]

    def _candidate(cell, want_large):
        # Pick the admissible value with the largest (adverse) or smallest
        # (favorable) magnitude, keeping the measured sign when both bounds
        # share it so a confirmed zero stays zero.
        if cell["lo"] >= 0:
            return cell["hi"] if want_large else cell["lo"]
        if cell["hi"] <= 0:
            return cell["lo"] if want_large else cell["hi"]
        return (cell["hi"] if abs(cell["hi"]) >= abs(cell["lo"]) else cell["lo"]) \
            if want_large else 0.0

    def favorable(cell):
        # A clipped tap carries an explicit measured lower bound.
        if cell.get("quality") == "clipped":
            return cell["fav"]
        return _candidate(cell, want_large=False)

    def adverse(cell):
        return _candidate(cell, want_large=True)

    algebraic_uncertain = any(
        c["quality"] != "measured" for layer in h0 for c in layer) if h0 else False

    gain_ranges = json.loads(contract["gain_ranges_json"])
    spans = []
    for ch_id in channels:
        span = gain_ranges.get(ch_id, [1.0, 1.0])
        spans.append((float(span[0]), float(span[1])))
    eps = float(common_error or 0.0)
    # Corners of the gain hypercube.  The single common calibration error is
    # one error shared by ALL channels (never re-picked per feedback edge):
    # nominal uses the contracted corners, the envelope applies the common
    # error in its two extreme directions.
    base_corners = []
    for mask in range(2 ** n):
        base_corners.append([spans[k][(mask >> k) & 1] for k in range(n)])
    envelope_corners = list(base_corners)
    if eps:
        for g in base_corners:
            envelope_corners.append([v * (1.0 + eps) for v in g])
            envelope_corners.append([v * (1.0 - eps) for v in g])

    def uniq(rows):
        out = []
        for g in rows:
            if not any(all(abs(a - b) < 1e-15 for a, b in zip(g, u)) for u in out):
                out.append(g)
        return out

    gain_corners = uniq(envelope_corners)
    nominal_corners = uniq(base_corners)

    def radii(selector, corners=None):
        corners = gain_corners if corners is None else corners
        return [closed_loop_radius(taps, g, h0=h0, selector=selector) for g in corners]

    def peaks(selector, corners=None):
        corners = nominal_corners if corners is None else corners
        return [peak_response(taps, g, selector=selector, h0=h0) for g in corners]

    rho_nom = max(radii(nominal, nominal_corners))
    # Both envelopes must hold at the WORST gain corner, so each is a max;
    # favorable/adverse differ only in the evidence interpretation.
    rho_fav = max(radii(favorable))
    rho_adv = max(radii(adverse))
    # Peak simulation is only meaningful for a stable interpretation: an
    # unstable impulse response grows without bound and its instability is
    # already decided by rho >= 1, so do not let a diverging time series turn
    # an "inconclusive stability" band into a hard peak rejection.
    peak_nom = max(peaks(nominal)) if rho_nom < 1.0 else math.inf
    peak_adv = max(peaks(adverse)) if rho_adv < 1.0 else math.inf
    # rho(A) is the per-sample growth; for an L-tap memory the per-roundtrip
    # modal gain is rho(A)^L (identical to rho when L=1).
    lags_count = len(taps)
    # rho(A) is the per-sample growth; each feedback trip takes as many
    # samples as the effective loop memory, so the per-roundtrip modal gain is
    # rho(A)^memory (equal to rho for a one-tap lag-1 system).
    modal = {
        "nominal": rho_nom, "favorable": rho_fav, "adverse": rho_adv,
        "roundtrip_nominal": rho_nom ** effective_memory,
        "roundtrip_adverse": rho_adv ** effective_memory,
        "taps": lags_count, "effective_memory_samples": effective_memory,
    }

    peak_limits = json.loads(contract["peak_limits_json"])
    limit = min(float(peak_limits.get(ch_id, 1.0)) for ch_id in channels)

    def json_safe(value):
        return None if value == math.inf else value

    # Stability boundary: largest common scaling of the contracted maximum
    # gains that keeps the adverse interpretation stable.
    g_max = [spans[k][1] for k in range(n)]
    boundary = _stability_boundary(taps, g_max, h0, adverse)
    resonance = resonance_scan(taps, g_max, int(calibration["rate"]), selector=nominal)
    edge_mag = [[abs(adverse(taps[0][i][j])) if len(taps) >= 1 else 0.0
                 for j in range(n)] for i in range(n)]
    # Report cycles whose acoustic round-trip gain is within an order of
    # magnitude of unity, i.e. the reviewable near-critical loops.  The
    # amplified product folds in each visited channel's maximum gain.
    raw_modes = simple_cycles(edge_mag, threshold=0.5)
    modes = []
    for cycle in raw_modes:
        loop = cycle["loop"]
        amplified = cycle["roundtrip_gain"]
        for idx in loop[:-1]:
            amplified *= spans[idx][1]
        modes.append({
            "channel_loop": [channels[k] for k in loop],
            "acoustic_roundtrip_gain": cycle["roundtrip_gain"],
            "amplified_roundtrip_gain": amplified,
        })

    if "clipped" in quality_flags or "missing" in quality_flags or algebraic_uncertain:
        if rho_fav >= 1.0 or (peak_nom != math.inf and peak_nom > limit):
            status = "rejected"
            reason = ("unstable (or over peak limit) even under the most favorable "
                      "interpretation of clipped/missing taps")
        elif rho_adv >= 1.0 or (peak_adv != math.inf and peak_adv > limit):
            status = "inconclusive"
            reason = ("evidence band contains both stable and unstable interpretations; "
                      "contract requires additional measurement")
        else:
            status = "approved"
            reason = "every admissible interpretation stays stable and within peak limits"
    else:
        # Clean measured FIR: the common calibration error is a signed,
        # deterministic version applied to every channel, so an envelope that
        # crosses unity is a hard rejection rather than an evidence ambiguity.
        if rho_nom >= 1.0 or rho_adv >= 1.0:
            status = "rejected"
            reason = ("closed-loop growth mode reaches unity within the licensed gain "
                      "range (nominal %.4f, worst %.4f)" % (rho_nom, rho_adv))
        elif peak_nom > limit:
            status = "rejected"
            reason = "loudspeaker peak limit exceeded: worst peak %.4f > %.4f" % (peak_nom, limit)
        else:
            status = "approved"
            reason = "stable across the whole contracted gain range with peak headroom"

    verdict = {
        "contract": contract["id"],
        "contract_version": int(contract["version"]),
        "calibration": calibration["id"],
        "gain_calibration_version": int(gain_version or 0),
        "status": status,
        "reason": reason,
        "channels": channels,
        "gain_ranges": {ch_id: list(spans[k]) for k, ch_id in enumerate(channels)},
        "common_calibration_error": eps,
        "growth_mode_magnitude": modal,
        "stability_boundary_gain_scale": boundary,
        "speaker_peak": {"nominal": json_safe(peak_nom),
                         "adverse": json_safe(peak_adv), "limit": limit},
        "resonance": resonance,
        "unstable_modes": modes,
        "measured_at": int(calibration["measured_at"]),
        "signed_at": int(contract["signed_at"]),
        "evidence_quality": sorted(quality_flags),
    }
    db.execute(
        "INSERT INTO sound_license(show_id,calibration_id,contract_id,contract_version,gain_version,"
        "status,gain_ranges_json,verdict_json,reason,source_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (show_id, calibration["id"], contract["id"], int(contract["version"]),
         int(gain_version or 0), status,
         json.dumps(verdict["gain_ranges"], sort_keys=True),
         json.dumps(verdict, ensure_ascii=False, sort_keys=True), reason, source_id))
    decide(db, "pa-license", f"{contract['id']}:v{contract['version']}", status, reason,
           [calibration["id"], contract["id"], int(contract["version"]),
            int(gain_version or 0), source_id])
    return verdict


def _stability_boundary(taps, g_max, h0, selector, iters=60):
    """Largest common scale s of g_max with rho(s * g_max) < 1."""
    if closed_loop_radius(taps, g_max, h0=h0, selector=selector) >= 1.0:
        lo, hi = 0.0, 1.0
    else:
        # Expand the upper bracket until the scaled system becomes unstable.
        lo = hi = 1.0
        for _ in range(20):
            hi *= 2.0
            if closed_loop_radius(taps, [v * hi for v in g_max], h0=h0,
                                  selector=selector) >= 1.0:
                break
    for _ in range(iters):
        mid = (lo + hi) / 2
        if closed_loop_radius(taps, [v * mid for v in g_max], h0=h0,
                              selector=selector) < 1.0:
            lo = mid
        else:
            hi = mid
    return lo


def reevaluate_show(db, show_id, source_id):
    """Re-license every active signed contract against the newest evidence."""
    out = []
    show = db.execute("SELECT * FROM show_slot WHERE id=?", (show_id,)).fetchone()
    if show and show["cancelled"]:
        # Cancellation voids every standing license; old versions stay on file.
        _invalidate(db, show_id, "show cancelled", source_id)
        return out
    calibration = db.execute(
        "SELECT * FROM sound_calibration WHERE show_id=? ORDER BY ingested_at DESC, id DESC LIMIT 1",
        (show_id,)).fetchone()
    gaincal = db.execute(
        "SELECT * FROM gain_calibration WHERE show_id=? ORDER BY version DESC LIMIT 1",
        (show_id,)).fetchone()
    common_error = gaincal["common_error"] if gaincal else 0.0
    gain_version = gaincal["version"] if gaincal else 0
    contracts = db.execute(
        "SELECT * FROM wiring_contract WHERE show_id=? ORDER BY version DESC", (show_id,)).fetchall()
    for contract in contracts:
        latest = db.execute(
            "SELECT id,status,superseded FROM sound_license WHERE show_id=? AND contract_id=? "
            "AND contract_version=? ORDER BY id DESC LIMIT 1",
            (show_id, contract["id"], contract["version"])).fetchone()
        # A still-standing final verdict is authoritative; a superseded verdict
        # (venue correction, wiring/gain revision) or a provisional row must be
        # re-evaluated against the current evidence.
        if latest and latest["superseded"] == 0 and latest["status"] not in (
                "pending_measurement", "pending_show"):
            continue
        if latest and latest["status"] in ("pending_measurement", "pending_show"):
            # Evidence has arrived: replace the provisional row by a verdict.
            db.execute("UPDATE sound_license SET superseded=1, reason=? WHERE id=?",
                       ("evidence arrived; provisional license replaced", latest["id"]))
        if not calibration:
            verdict = {"contract": contract["id"], "status": "pending_measurement",
                       "reason": "no sound field calibration recorded for this show"}
            db.execute(
                "INSERT INTO sound_license(show_id,calibration_id,contract_id,contract_version,"
                "gain_version,status,gain_ranges_json,verdict_json,reason,source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (show_id, None, contract["id"], contract["version"], gain_version,
                 "pending_measurement", contract["gain_ranges_json"],
                 json.dumps(verdict, sort_keys=True), verdict["reason"], source_id))
            decide(db, "pa-license", f"{contract['id']}:v{contract['version']}",
                   "pending_measurement", verdict["reason"], [contract["id"], source_id])
            out.append(verdict)
            continue
        out.append(evaluate(db, show_id, calibration, contract, gain_version,
                            common_error, source_id))
    return out


def run(db, data):
    """Dispatch entry point used by the CLI."""
    kind = data.get("kind")
    if kind == "calibration":
        return run_calibration(db, data)
    if kind == "contract":
        return run_contract(db, data)
    if kind == "gain_calibration":
        return run_gain_calibration(db, data)
    raise ValueError("pa payload kind must be calibration, contract or gain_calibration")
