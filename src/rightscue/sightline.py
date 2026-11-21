"""Offline sightline certification from sourced 3D geometry.

Seats, eye-posture ranges, area targets and moving occluders arrive with
sourced three-dimensional coordinates and time intervals; a manual
``visual_clear`` flag is never trusted on its own.  Targets are planar
areas whose boundary is verified, occluder measurements carry error
bands (the whole error box must be cleared, not one favourable ray) and
one continuous scene requires one sustainable eye posture: the feasible
posture sets are intersected across scenes instead of re-picking an eye
per target edge.  Whether posture may change between scenes is governed
by the pre-sold contract (``fixed`` vs ``adjustable``).
"""
import json
from .store import ingest, decide

EPS = 1e-9
GRID_STEPS = 9


# ---------------------------------------------------------------- geometry

def corners(lo, hi):
    return [[lo[0] if bit & 1 else hi[0],
             lo[1] if bit & 2 else hi[1],
             lo[2] if bit & 4 else hi[2]] for bit in range(8)]


def expand_box(lo, hi, err):
    if isinstance(err, (int, float)):
        err = [err, err, err]
    return [lo[k] - err[k] for k in range(3)], [hi[k] + err[k] for k in range(3)]


def boxes_overlap(a_lo, a_hi, b_lo, b_hi):
    return all(a_lo[k] <= b_hi[k] + EPS and b_lo[k] <= a_hi[k] + EPS for k in range(3))


def interval_overlap(a, b):
    return max(a[0], b[0]) < min(a[1], b[1]) - EPS


def segment_hits_box(origin, point, lo, hi):
    """Ray/AABB slab test over the segment parameter range (0, 1).

    Slabs are intersected over the full ray; a parallel ray lying inside
    a slab leaves the other slabs decisive. Boundary grazing is ignored:
    contact only at t=0 or t=1, or intersection of zero thickness lying
    exactly on another slab's boundary, does not count. A ray crossing a
    zero-thickness panel face inside the segment does count, since the
    panel has solid extent in its other axes."""
    t_enter, t_exit = float('-inf'), float('inf')
    for k in range(3):
        d = point[k] - origin[k]
        if abs(d) < EPS:
            if origin[k] < lo[k] - EPS or origin[k] > hi[k] + EPS:
                return False
            continue
        t1 = (lo[k] - origin[k]) / d
        t2 = (hi[k] - origin[k]) / d
        near, far = (t1, t2) if t1 < t2 else (t2, t1)
        t_enter = max(t_enter, near)
        t_exit = min(t_exit, far)
        if t_enter > t_exit + EPS:
            return False
    if t_enter >= 1.0 - EPS or t_exit <= EPS:
        return False
    return True


def boundary_samples(boundary):
    """Target boundary vertices plus each edge midpoint, so an area target
    is checked along its boundary rather than only at its centre point."""
    pts = [list(p) for p in boundary]
    if len(pts) >= 2:
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            pts.append([(a[k] + b[k]) / 2 for k in range(3)])
    return pts


# ---------------------------------------------------------- 1D exact solver

def critical_eye_values(eye_lo, eye_hi, axis, boundary, occ_lo, occ_hi):
    """Eye values at which the ray to a boundary vertex grazes an occluder
    corner.  Between such values the clear/blocked status is constant, so
    midpoint tests over the partition are exact rather than sampled."""
    base = list(eye_lo)
    other = [k for k in range(3) if k != axis]
    out = [eye_lo[axis], eye_hi[axis]]
    for target in boundary:
        for corner in corners(occ_lo, occ_hi):
            t = None
            for k in other:  # ray parameter from a fixed non-axis component
                dv = target[k] - base[k]
                if abs(dv) >= EPS:
                    t = (corner[k] - base[k]) / dv
                    break
            if t is None or not EPS < t < 1 - EPS:
                continue
            u = (corner[axis] - t * target[axis]) / (1 - t)
            if eye_lo[axis] - EPS <= u <= eye_hi[axis] + EPS:
                out.append(min(eye_hi[axis], max(eye_lo[axis], u)))
    return sorted({round(v, 12) for v in out})


def eye_posture(eye_lo, eye_hi, axis, u):
    p = list(eye_lo)
    p[axis] = u
    return p


def pair_clear_set_1d(eye_lo, eye_hi, axis, target, occ_lo, occ_hi):
    """Exact feasible eye interval for clearing one occluder on the way
    to one target point."""
    crit = critical_eye_values(eye_lo, eye_hi, axis, [target], occ_lo, occ_hi)
    clear = []
    for i in range(len(crit) - 1):
        u = (crit[i] + crit[i + 1]) / 2
        if segment_hits_box(eye_posture(eye_lo, eye_hi, axis, u), target, occ_lo, occ_hi):
            continue
        clear.append((crit[i], crit[i + 1]))
    return clear


def intersect_sets(a, b):
    if not a or not b:
        return []
    if isinstance(a[0][0], tuple):  # degenerate point-posture sets
        points = {iv[0] for iv in a}
        return [iv for iv in b if iv[0] in points]
    out = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi - EPS:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def nonempty(intervals):
    if not intervals:
        return False
    lo, hi = intervals[0]
    if isinstance(lo, tuple):
        return True
    return any(b - a > EPS for a, b in intervals)


def pick_posture(intervals, eye_lo, eye_hi, axis=None):
    if not nonempty(intervals):
        return None
    lo, hi = intervals[0]
    if axis is None:
        return list(lo)
    return eye_posture(eye_lo, eye_hi, axis, (lo + hi) / 2)


# ------------------------------------------------------------- certification

def certify_seat(seat, scenes_data):
    """Return per-scene feasible posture sets and per-contract verdicts.

    ``scenes_data`` maps scene_id to {'targets':[boundary...],
    'occluders':[row...], 'conflict': bool}.
    """
    eye_lo, eye_hi = seat['eye_lo'], seat['eye_hi']
    free_axes = [k for k in range(3) if eye_hi[k] - eye_lo[k] > EPS]
    scene_sets = {}
    detail = {'scenes': {}, 'method': None}
    conflict_scenes = []

    for scene_id, data in sorted(scenes_data.items()):
        if data['conflict']:
            conflict_scenes.append(scene_id)
            detail['scenes'][scene_id] = {'state': 'resurvey',
                                           'occluder_sources': data['sources']}
            continue
        if not free_axes:
            posture = list(eye_lo)
            blocked = _blocking(posture, data)
            # Degenerate point interval; intersect_sets handles (p, p) sets.
            scene_sets[scene_id] = [] if blocked else [((tuple(posture)), (tuple(posture)))]
            detail['method'] = 'point'
            detail['scenes'][scene_id] = _scene_detail(posture, data, blocked)
            continue
        if len(free_axes) == 1:
            axis = free_axes[0]
            clear = None
            for boundary in data['targets']:
                for target in boundary_samples(boundary):
                    for occ in data['occluders']:
                        pair = pair_clear_set_1d(eye_lo, eye_hi, axis, target,
                                                 occ['lo'], occ['hi'])
                        clear = pair if clear is None else intersect_sets(clear, pair)
                    if clear is not None and not clear:
                        break
                if clear is not None and not clear:
                    break
            if clear is None:
                clear = [(eye_lo[axis], eye_hi[axis])]
            scene_sets[scene_id] = clear
            detail['method'] = 'analytic_1d'
            posture = pick_posture(clear, eye_lo, eye_hi, axis)
            detail['scenes'][scene_id] = _scene_detail(posture, data,
                                                       None if posture else _blocking(
                                                           eye_posture(eye_lo, eye_hi, axis,
                                                                       (eye_lo[axis] + eye_hi[axis]) / 2),
                                                           data))
        else:
            posture, blocked = _grid_search(eye_lo, eye_hi, free_axes, data)
            if posture is None:
                scene_sets[scene_id] = []
            else:
                scene_sets[scene_id] = [(tuple(posture), tuple(posture))]
            detail['method'] = 'grid_3d'
            detail['scenes'][scene_id] = _scene_detail(posture, data, blocked)

    if conflict_scenes:
        return {'state': 'resurvey',
                'reason': 'incompatible occluder surveys in scenes: ' + ','.join(conflict_scenes),
                'detail': detail, 'fixed': None, 'adjustable': None,
                'scene_sets': scene_sets}

    fixed_set = None
    for clear in scene_sets.values():
        fixed_set = clear if fixed_set is None else intersect_sets(fixed_set, clear)
    fixed_ok = fixed_set is not None and nonempty(fixed_set)
    adjustable_ok = all(nonempty(s) for s in scene_sets.values()) and bool(scene_sets)

    chosen = {}
    if adjustable_ok:
        for scene_id, clear in scene_sets.items():
            if len(free_axes) == 1:
                chosen[scene_id] = pick_posture(
                    clear, eye_lo, eye_hi, free_axes[0])
            else:
                # Grid path stores one point posture (tuple, tuple).
                chosen[scene_id] = list(clear[0][0]) if nonempty(clear) else None
    fixed_posture = None
    if fixed_ok:
        if len(free_axes) == 1:
            fixed_posture = pick_posture(fixed_set, eye_lo, eye_hi, free_axes[0])
        else:
            fixed_posture = list(fixed_set[0][0])

    failing = [sid for sid, s in sorted(scene_sets.items()) if not nonempty(s)]
    fixed_failing = failing if fixed_ok else sorted(scene_sets)
    return {
        'state': 'certified',
        'fixed': {'sellable': fixed_ok, 'common_solution': fixed_ok,
                  'posture': fixed_posture, 'failing_scenes': fixed_failing},
        'adjustable': {'sellable': adjustable_ok,
                       'postures': chosen,
                       'failing_scenes': failing if not adjustable_ok else []},
        'scene_sets': {sid: [list(iv) for iv in s] for sid, s in scene_sets.items()},
        'detail': detail,
    }


def _blocking(posture, data):
    found = []
    for boundary in data['targets']:
        for target in boundary_samples(boundary):
            for occ in data['occluders']:
                if segment_hits_box(posture, target, occ['lo'], occ['hi']):
                    found.append({'target': target, 'occluder_id': occ['id'],
                                  'source_id': occ['source_id']})
    return found


def _scene_detail(posture, data, blocked):
    return {
        'state': 'resurvey' if data.get('conflict') else (
            'clear' if posture is not None and not blocked else 'obstructed'),
        'posture': posture,
        'target_boundaries': data['targets'],
        'occluders': [{'id': o['id'], 'source_id': o['source_id'],
                       'box': [o['lo'], o['hi']]} for o in data['occluders']],
        'blocking': blocked or [],
    }


def _grid_search(eye_lo, eye_hi, free_axes, data):
    """Fallback for genuinely 3D eye ranges: a deterministic grid including
    every box corner; the 1D analytic path is used whenever only one axis
    is free."""
    import itertools
    grid = []
    for k in range(3):
        if k in free_axes:
            grid.append([eye_lo[k] + (eye_hi[k] - eye_lo[k]) * i / GRID_STEPS
                         for i in range(GRID_STEPS + 1)])
        else:
            grid.append([eye_lo[k]])
    for combo in itertools.product(*grid):
        posture = list(combo)
        blocked = _blocking(posture, data)
        if not blocked:
            return posture, None
    mid = [(eye_lo[k] + eye_hi[k]) / 2 for k in range(3)]
    return None, _blocking(mid, data)


# ------------------------------------------------------------------- ledger

def run(db, data):
    state = ingest(db, 'sightline', data)
    if state != 'new':
        return {'source_id': data['source_id'], 'status': state}
    show_id = data['show_id']
    slot = db.execute('SELECT venue FROM show_slot WHERE id=?', (show_id,)).fetchone()
    if not slot:
        raise ValueError('show not found')

    # Legacy manual-only intake remains supported for prior surveys but is
    # never used as geometric evidence.
    if 'seats' in data and 'venue_seats' not in data:
        return _legacy_manual(db, data)

    if data.get('venue') != slot['venue']:
        raise ValueError('sightline survey venue does not match show slot venue')
    _store_geometry(db, data, show_id)
    return recertify_show(db, show_id, data['source_id'])


def _legacy_manual(db, data):
    show_id = data['show_id']
    result = []
    for row in data['seats']:
        clear = bool(row['visual_clear'])
        db.execute('INSERT OR IGNORE INTO seat_survey VALUES(?,?,?,?,?)',
                   (row['id'], show_id, row['seat_id'], int(clear), data['source_id']))
        status = 'clear' if clear else 'obstructed'
        db.execute('INSERT INTO seat_case VALUES(?,?,?) ON CONFLICT(show_id,seat_id) '
                   'DO UPDATE SET status=excluded.status', (show_id, row['seat_id'], status))
        decide(db, 'sightline', f'{show_id}:{row["seat_id"]}', status,
               'manual visual-clear survey (no geometry)', [data['source_id'], row['id']])
        result.append({'seat_id': row['seat_id'], 'status': status})
    return {'show_id': show_id, 'seats': result, 'mode': 'manual'}


def _store_geometry(db, data, show_id):
    venue = data['venue']
    for seat in data.get('venue_seats', []):
        db.execute('INSERT OR IGNORE INTO venue_seat VALUES(?,?,?,?,?,?,?,?)',
                   (data['source_id'], seat['id'], show_id, venue, seat['seat_id'],
                    json.dumps(seat['position']), json.dumps(seat['eye_lo']),
                    json.dumps(seat['eye_hi'])))
    for target in data.get('targets', []):
        db.execute('INSERT OR IGNORE INTO stage_target VALUES(?,?,?,?,?,?,?)',
                   (data['source_id'], target['id'], show_id, target['scene_id'],
                    int(target['starts']), int(target['ends']),
                    json.dumps(target['boundary'])))
    for occ in data.get('occluders', []):
        err = occ.get('err', 0.0)
        lo, hi = list(occ['box_lo']), list(occ['box_hi'])
        ex_lo, ex_hi = expand_box(lo, hi, err)
        # Every survey of the same physical occluder over an overlapping
        # time window must be mutually compatible (error boxes overlap).
        # Incompatible pairs are marked for resurvey rather than letting
        # the latest upload silently overwrite the earlier position.
        prior = db.execute(
            'SELECT * FROM moving_occluder WHERE id=? AND show_id=?',
            (occ['id'], show_id)).fetchall()
        incompatible = []
        for old in prior:
            if not interval_overlap((int(occ['starts']), int(occ['ends'])),
                                    (old['starts'], old['ends'])):
                continue
            old_lo, old_hi = expand_box(json.loads(old['box_lo']),
                                        json.loads(old['box_hi']), json.loads(old['err']))
            if not boxes_overlap(ex_lo, ex_hi, old_lo, old_hi):
                incompatible.append(old)
        conflict = bool(incompatible)
        if conflict:
            for old in incompatible:
                db.execute('UPDATE moving_occluder SET state="conflict" WHERE id=? '
                           'AND show_id=? AND source_id=?',
                           (occ['id'], show_id, old['source_id']))
            decide(db, 'sightline-survey', f'{show_id}:{occ["id"]}', 'conflict',
                   'incompatible occluder positions for overlapping time; resurvey required',
                   [data['source_id']] + [o['source_id'] for o in incompatible])
        db.execute('INSERT OR IGNORE INTO moving_occluder VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (data['source_id'], occ['id'], show_id, venue, occ.get('scene_id'),
                    int(occ['starts']), int(occ['ends']), json.dumps(lo), json.dumps(hi),
                    json.dumps(err), 'conflict' if conflict else 'active'))
    for ticket in data.get('commitments', []):
        db.execute('INSERT OR IGNORE INTO sight_commitment VALUES(?,?,?,?,?,?)',
                   (ticket['ticket_id'], show_id, ticket['seat_id'],
                    ticket['mode'], int(bool(ticket.get('full_visibility', True))),
                    data['source_id']))


def _load_scene_inputs(db, show_id, active_scenes):
    seats = {}
    for row in db.execute(
            'SELECT * FROM venue_seat WHERE show_id=? ORDER BY rowid DESC', (show_id,)):
        seats.setdefault(row['seat_id'], {
            'seat_id': row['seat_id'], 'position': json.loads(row['position']),
            'eye_lo': json.loads(row['eye_lo']), 'eye_hi': json.loads(row['eye_hi']),
            'source_id': row['source_id']})

    # Scene applicability comes from the signed program's scene list and
    # each measurement's sourced time interval: an occluder counts for a
    # scene only when its active window overlaps that scene's window.
    targets = {}
    scene_windows = {}
    for row in db.execute('SELECT * FROM stage_target WHERE show_id=?', (show_id,)):
        sid = row['scene_id']
        if active_scenes is not None and sid not in active_scenes:
            continue
        targets.setdefault(sid, []).append(json.loads(row['boundary']))
        win = (row['starts'], row['ends'])
        if sid in scene_windows:
            old = scene_windows[sid]
            scene_windows[sid] = (min(old[0], win[0]), max(old[1], win[1]))
        else:
            scene_windows[sid] = win

    occluders = {}
    conflict_sources = {}
    for row in db.execute('SELECT * FROM moving_occluder WHERE show_id=?', (show_id,)):
        sid = row['scene_id']
        if sid is None:
            # Measurements carrying only a time window attach to every scene
            # whose window they overlap (e.g. a temporary camera rig).
            matching = [s for s, w in scene_windows.items()
                        if interval_overlap((row['starts'], row['ends']), w)]
        else:
            if active_scenes is not None and sid not in active_scenes:
                continue
            matching = [sid]
            if sid in scene_windows and not interval_overlap(
                    (row['starts'], row['ends']), scene_windows[sid]):
                continue  # measurement window never overlaps the scene: not evidence here
        entry = {'id': row['id'], 'source_id': row['source_id'],
                 'lo': json.loads(row['box_lo']), 'hi': json.loads(row['box_hi']),
                 'err': json.loads(row['err']),
                 'window': [row['starts'], row['ends']]}
        entry['lo'], entry['hi'] = expand_box(entry['lo'], entry['hi'], entry['err'])
        for sid in matching:
            occluders.setdefault(sid, []).append(entry)
            if row['state'] == 'conflict':
                conflict_sources.setdefault(sid, set()).add(row['source_id'])

    scenes = {}
    for sid in set(targets) | set(occluders):
        scenes[sid] = {'targets': targets.get(sid, []),
                       'occluders': occluders.get(sid, []),
                       'conflict': sid in conflict_sources,
                       'sources': sorted(conflict_sources.get(sid, []))}
    return seats, scenes


def recertify_show(db, show_id, source_id=None, allocate=True):
    """Recompute every seat verdict for the signed program revision.

    Called when geometry arrives and from ``show.run`` after a program
    revision, venue change or cancellation invalidates prior verdicts.
    Original surveys, the version each verdict was sold under and issued
    re-seat orders are always retained.
    """
    slot = db.execute('SELECT * FROM show_slot WHERE id=?', (show_id,)).fetchone()
    if not slot:
        return {'show_id': show_id, 'seats': []}
    revision = db.execute('SELECT scenes FROM program_revision WHERE show_id=? AND version=?',
                          (show_id, slot['program_version'])).fetchone()
    active_scenes = None
    if revision and revision['scenes']:
        active_scenes = set(json.loads(revision['scenes']))

    seats, scenes = _load_scene_inputs(db, show_id, active_scenes)
    tickets = {r['seat_id']: dict(r) for r in
               db.execute('SELECT * FROM sight_commitment WHERE show_id=?', (show_id,))}

    prior_version = db.execute('SELECT MAX(program_version) FROM seat_cert WHERE show_id=?',
                               (show_id,)).fetchone()[0]
    if prior_version is not None and prior_version != slot['program_version']:
        db.execute('UPDATE seat_cert SET state="superseded" WHERE show_id=? AND state="certified"',
                   (show_id,))

    results = []
    verdicts = {}
    for seat_id in sorted(seats):
        seat = seats[seat_id]
        if slot['cancelled'] or slot['venue'] != _seat_venue(db, show_id, seat_id):
            state, status, reason, verdict = 'invalidated', 'invalidated', \
                'show cancelled or venue changed; prior sale verdict void', None
        else:
            verdict = certify_seat(seat, scenes)
            state = verdict['state']
            if state == 'resurvey':
                status, reason = 'resurvey', verdict['reason']
            else:
                ticket = tickets.get(seat_id)
                mode = ticket['mode'] if ticket else 'fixed'
                mode_result = verdict[mode]
                status = 'sellable' if mode_result['sellable'] else 'reseat_required'
                reason = (f'{mode} contract: common eye solution exists'
                          if mode_result['sellable']
                          else f'{mode} contract: no sustainable eye posture across scenes')
        detail = json.dumps(verdict, ensure_ascii=False, sort_keys=True) if verdict else '{}'
        db.execute('INSERT INTO seat_cert(show_id,seat_id,program_version,venue,state,status,'
                   'reason,detail,source_id) VALUES(?,?,?,?,?,?,?,?,?)',
                   (show_id, seat_id, slot['program_version'], slot['venue'], state, status,
                    reason, detail, source_id))
        decide(db, 'sightline-cert', f'{show_id}:{seat_id}', status, reason,
               [show_id, slot['program_version'], seat['source_id']])
        verdicts[seat_id] = status
        results.append({'seat_id': seat_id, 'status': status, 'reason': reason,
                        'verdict': verdict})

    orders = []
    if allocate and not slot['cancelled']:
        orders = _allocate_reseats(db, show_id, slot, seats, tickets, verdicts, source_id)
    return {'show_id': show_id, 'program_version': slot['program_version'],
            'venue': slot['venue'], 'seats': results, 'reseat_orders': orders}


def _seat_venue(db, show_id, seat_id):
    row = db.execute('SELECT venue FROM venue_seat WHERE show_id=? AND seat_id=? '
                     'ORDER BY id DESC LIMIT 1', (show_id, seat_id)).fetchone()
    return row['venue'] if row else None


def _allocate_reseats(db, show_id, slot, seats, tickets, verdicts, source_id):
    """One alternative seat cannot be occupied by two tickets: failed
    allocations record the conflicting ticket numbers."""
    occupied = {t['seat_id']: t['ticket_id'] for t in tickets.values()}
    orders = []
    for seat_id in sorted(tickets):
        ticket = tickets[seat_id]
        if verdicts.get(seat_id) != 'reseat_required':
            continue
        exists = db.execute('SELECT 1 FROM reseat_order WHERE ticket_id=? AND program_version=?',
                            (ticket['ticket_id'], slot['program_version'])).fetchone()
        if exists:
            continue
        alternatives = []
        chosen = None
        conflict_tickets = []
        for cand in sorted(seats):
            if cand == seat_id:
                continue
            cand_verdict = db.execute(
                'SELECT detail FROM seat_cert WHERE show_id=? AND seat_id=? AND state="certified" '
                'ORDER BY id DESC LIMIT 1', (show_id, cand)).fetchone()
            data = json.loads(cand_verdict['detail']) if cand_verdict else {}
            # The alternative must satisfy this ticket's own pre-sold
            # contract mode; the seat's top-level status may use another.
            mode_result = data.get(ticket['mode'])
            if not mode_result or not mode_result.get('sellable'):
                continue
            alternatives.append(cand)
            if cand in occupied:
                conflict_tickets.append(occupied[cand])
            elif chosen is None:
                chosen = cand
        if chosen is not None:
            occupied[chosen] = ticket['ticket_id']
            order_status, reason, to_seat = 'reassigned', \
                'alternative seat satisfies the same pre-sold sightline contract', chosen
            conflicts = []
        else:
            order_status, reason, to_seat, conflicts = 'failed', \
                'no contract-equivalent empty seat available', None, conflict_tickets
        db.execute('INSERT INTO reseat_order(ticket_id,show_id,from_seat,to_seat,status,reason,'
                   'conflict_tickets,program_version,source_id) VALUES(?,?,?,?,?,?,?,?,?)',
                   (ticket['ticket_id'], show_id, seat_id, to_seat, order_status, reason,
                    json.dumps(conflicts), slot['program_version'], source_id))
        decide(db, 'sightline-reseat', ticket['ticket_id'], order_status, reason,
               [show_id, seat_id, to_seat] + conflicts)
        orders.append({'ticket_id': ticket['ticket_id'], 'from_seat': seat_id,
                       'to_seat': to_seat, 'status': order_status,
                       'alternatives': alternatives, 'conflict_tickets': conflicts})
    return orders
