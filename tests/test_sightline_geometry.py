"""Geometric sightline certification regression tests.

The reference case is the recomputable side section from the brief: every
coordinate other than the section plane is zero, eye height 1.2..1.8 m at
x=0, targets at x=4, panels at x=2. Scene 1 needs eye height above 1.5 m,
scene 2 below 1.4 m; each scene alone is visible, but no single height
satisfies both.
"""
import copy
from pathlib import Path

from rightscue import cue, report, rights, show, sightline
from rightscue.store import connect, read

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'
PANEL_LOW = {'id': 'panel-sc1', 'scene_id': 'scene-1', 'starts': 0, 'ends': 1000,
             'box_lo': [2, -0.01, -10], 'box_hi': [2, 0.01, 1.25], 'err': 0}
PANEL_HIGH = {'id': 'panel-sc2', 'scene_id': 'scene-2', 'starts': 1000, 'ends': 2000,
              'box_lo': [2, -0.01, 1.7], 'box_hi': [2, 0.01, 10], 'err': 0}
TARGET_1 = {'id': 'tgt-sc1', 'scene_id': 'scene-1', 'starts': 0, 'ends': 1000,
            'boundary': [[4, 0, 1], [4, 0, 1]]}
TARGET_2 = {'id': 'tgt-sc2', 'scene_id': 'scene-2', 'starts': 1000, 'ends': 2000,
            'boundary': [[4, 0, 2], [4, 0, 2]]}


def seat(seat_id, x=0.0):
    return {'id': f'seat-{seat_id}', 'seat_id': seat_id, 'position': [x, 0, 0],
            'eye_lo': [x, 0, 1.2], 'eye_hi': [x, 0, 1.8]}


def geo_payload(source_id='sight-geo-1', venue='hall-A', seats=None, targets=None,
                occluders=None, commitments=None):
    return {
        'source_id': source_id, 'show_id': 'show-1', 'venue': venue,
        'venue_seats': seats if seats is not None else [seat('A1'), seat('A2')],
        'targets': targets if targets is not None else [TARGET_1, TARGET_2],
        'occluders': occluders if occluders else [PANEL_LOW, PANEL_HIGH],
        'commitments': commitments if commitments is not None else [
            {'ticket_id': 'T-1', 'seat_id': 'A1', 'mode': 'fixed', 'full_visibility': True},
            {'ticket_id': 'T-2', 'seat_id': 'A2', 'mode': 'adjustable', 'full_visibility': True}],
    }


def seeded(tmp_path, payload=None, show_file='show-ok.json'):
    db = connect(tmp_path / 'sight.sqlite')
    with db:
        show.run(db, read(ROOT / show_file))
        sightline.run(db, payload or geo_payload())
    return db


def statuses(result):
    return {r['seat_id']: r['status'] for r in result['seats']}


def fresh_payload(tmp_path, payload):
    db = connect(tmp_path / 'fresh.sqlite')
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        result = sightline.run(db, copy.deepcopy(payload))
    return db, result


# ------------------------------------------------- recomputable side section

def test_cross_section_fixed_unsellable_adjustable_sellable(tmp_path):
    db, result = fresh_payload(tmp_path, geo_payload())
    assert statuses(result) == {'A1': 'reseat_required', 'A2': 'sellable'}
    a1 = next(r for r in result['seats'] if r['seat_id'] == 'A1')['verdict']
    # Each scene alone has a sustainable posture ...
    assert a1['adjustable']['sellable'] is True
    # ... but no single height satisfies both scenes.
    assert a1['fixed']['sellable'] is False
    assert a1['fixed']['common_solution'] is False
    assert a1['fixed']['posture'] is None
    sets = a1['scene_sets']
    assert sets['scene-1'] == [[1.5, 1.8]]
    assert sets['scene-2'] == [[1.2, 1.4]]
    assert a1['fixed']['failing_scenes'] == ['scene-1', 'scene-2']
    # Adjustable contract picks one sustainable posture per scene.
    p1 = a1['adjustable']['postures']['scene-1'][2]
    p2 = a1['adjustable']['postures']['scene-2'][2]
    assert p1 > 1.5 and p2 < 1.4
    # The adjustable seat carries a single posture valid for both scenes.
    a2 = next(r for r in result['seats'] if r['seat_id'] == 'A2')['verdict']
    assert a2['fixed']['sellable'] is False
    chain = db.execute(
        "SELECT status FROM decision WHERE system='sightline-cert' "
        "AND object_id='show-1:A1' ORDER BY id").fetchall()
    assert chain[0]['status'] == 'reseat_required'
    db.close()


def test_boundary_equality_is_grazing_not_a_collision(tmp_path):
    db = connect(tmp_path / 'g.sqlite')
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        sightline.run(db, geo_payload(seats=[seat('A1')], commitments=[]))
    cert = db.execute("SELECT detail FROM seat_cert WHERE seat_id='A1' ORDER BY id DESC").fetchone()
    import json
    detail = json.loads(cert['detail'])
    # h=1.5 exactly grazes panel 1; the open feasible interval starts at 1.5.
    assert detail['scene_sets']['scene-1'] == [[1.5, 1.8]]
    assert detail['scene_sets']['scene-2'] == [[1.2, 1.4]]
    db.close()


def test_manual_clear_flag_alone_is_not_geometric_evidence(tmp_path):
    db = connect(tmp_path / 'm.sqlite')
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        manual = sightline.run(db, read(ROOT / 'sightline-ok.json'))
    # Legacy intake still records the manual verdict ...
    assert {r['status'] for r in manual['seats']} == {'clear', 'obstructed'}
    # ... but produces no geometric certification.
    assert db.execute('SELECT COUNT(*) FROM seat_cert').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM venue_seat').fetchone()[0] == 0
    db.close()


# -------------------------------------------------- area target, not centre

def test_area_target_boundary_rechecked_not_carried_from_centre(tmp_path):
    point = copy.deepcopy(geo_payload())
    point['venue_seats'] = [seat('A1')]
    point['commitments'] = [{'ticket_id': 'T-1', 'seat_id': 'A1',
                             'mode': 'fixed', 'full_visibility': True}]
    point['targets'] = [TARGET_1]
    point['occluders'] = [PANEL_LOW]
    # Point target at height 1 is visible for some eye height above 1.5.
    db1 = connect(tmp_path / 'point-a.sqlite')
    with db1:
        show.run(db1, read(ROOT / 'show-ok.json'))
        r1 = sightline.run(db1, copy.deepcopy(point))
    assert statuses(r1)['A1'] == 'sellable'

    # Same centre, but the target is an area whose lower boundary forces the
    # ray below the panel top for every allowed eye height: the centre-point
    # conclusion must not be reused.
    area = copy.deepcopy(point)
    area['source_id'] = 'sight-area'
    # Lower boundary at 0.4: every permitted eye (h <= 1.8) crosses the
    # panel top 1.25 at x=2, while the centre point at height 1 stays clear.
    area['targets'] = [{'id': 'tgt-sc1', 'scene_id': 'scene-1', 'starts': 0,
                        'ends': 1000, 'boundary': [[4, 0, 1.0], [4, 0, 0.4]]}]
    db2 = connect(tmp_path / 'area-b.sqlite')
    with db2:
        show.run(db2, read(ROOT / 'show-ok.json'))
        r2 = sightline.run(db2, copy.deepcopy(area))
    assert statuses(r2)['A1'] == 'reseat_required'
    verdict = next(r for r in r2['seats'] if r['seat_id'] == 'A1')['verdict']
    blocking = verdict['detail']['scenes']['scene-1']['blocking']
    assert any(b['occluder_id'] == 'panel-sc1' for b in blocking)
    db1.close(); db2.close()


# ------------------------------------------------- occluder measurement error

def test_error_band_certifies_whole_band_not_one_ray(tmp_path):
    tight = geo_payload(source_id='sight-tight', seats=[seat('A1')],
                        targets=[TARGET_1], occluders=[PANEL_LOW],
                        commitments=[{'ticket_id': 'T-1', 'seat_id': 'A1',
                                      'mode': 'fixed', 'full_visibility': True}])
    db1 = connect(tmp_path / 'tight.sqlite')
    with db1:
        show.run(db1, read(ROOT / 'show-ok.json'))
        r1 = sightline.run(db1, copy.deepcopy(tight))
    assert statuses(r1)['A1'] == 'sellable'

    banded = copy.deepcopy(tight)
    banded['source_id'] = 'sight-band'
    banded['occluders'] = [dict(PANEL_LOW, err=0.1)]
    db2 = connect(tmp_path / 'band.sqlite')
    with db2:
        show.run(db2, read(ROOT / 'show-ok.json'))
        r2 = sightline.run(db2, copy.deepcopy(banded))
    # The whole error box must be cleared: expanding the panel top from
    # 1.25 to 1.35 raises the minimum eye height from 1.5 to 1.7, so a
    # different verdict is required rather than reusing the no-error ray.
    assert statuses(r2)['A1'] == 'sellable'
    v1 = next(r for r in r1['seats'] if r['seat_id'] == 'A1')['verdict']
    v2 = next(r for r in r2['seats'] if r['seat_id'] == 'A1')['verdict']
    assert v1['scene_sets']['scene-1'][0][0] < v2['scene_sets']['scene-1'][0][0]
    assert 1.7 < v2['scene_sets']['scene-1'][0][0] < 1.74

    # A wider band (top 1.45) needs eye above 1.9, outside the 1.8 range.
    blocked = copy.deepcopy(tight)
    blocked['source_id'] = 'sight-band-block'
    blocked['occluders'] = [dict(PANEL_LOW, err=0.2)]
    db3 = connect(tmp_path / 'band2.sqlite')
    with db3:
        show.run(db3, read(ROOT / 'show-ok.json'))
        r3 = sightline.run(db3, copy.deepcopy(blocked))
    assert statuses(r3)['A1'] == 'reseat_required'
    db1.close(); db2.close(); db3.close()


def test_incompatible_surveys_mark_resurvey_not_last_upload_wins(tmp_path):
    db = seeded(tmp_path)
    second = geo_payload(source_id='sight-geo-2')
    # Same time window, but the panel is measured far upstage (x=3) where its
    # error box cannot overlap the original x=2 measurement.
    second['occluders'] = [dict(PANEL_LOW, box_lo=[3, -0.01, -10],
                                box_hi=[3, 0.01, 1.25]), PANEL_HIGH]
    with db:
        result = sightline.run(db, second)
    # Both measurements are retained and neither may be used until resurvey ...
    rows = db.execute("SELECT source_id,state FROM moving_occluder WHERE id='panel-sc1'").fetchall()
    assert {r['state'] for r in rows} == {'conflict'}
    assert {r['source_id'] for r in rows} == {'sight-geo-1', 'sight-geo-2'}
    assert len(rows) == 2
    # ... and every seat touching scene 1 is held for resurvey.
    assert all(r['status'] == 'resurvey' for r in result['seats'])
    decision = db.execute(
        "SELECT status FROM decision WHERE system='sightline-survey' "
        "AND object_id='show-1:panel-sc1' ORDER BY id DESC LIMIT 1").fetchone()
    assert decision['status'] == 'conflict'
    view = report.build(db)['sightline_view']
    cert = view['certifications'][-1]
    assert cert['state'] == 'resurvey'
    db.close()


def test_compatible_overlapping_surveys_do_not_conflict(tmp_path):
    db = seeded(tmp_path)
    second = geo_payload(source_id='sight-geo-2b')
    second['occluders'] = [dict(PANEL_LOW, err=0.05), PANEL_HIGH]
    with db:
        result = sightline.run(db, second)
    assert db.execute("SELECT COUNT(*) FROM moving_occluder WHERE state='conflict'").fetchone()[0] == 0
    assert statuses(result)['A2'] == 'sellable'
    db.close()


# ------------------------------------------------------ alternative allocation

def test_alternative_seat_satisfies_same_contract_and_is_reassigned(tmp_path):
    payload = geo_payload(seats=[seat('A1'), seat('A2'), seat('B1', x=0.5)])
    db, result = fresh_payload(tmp_path, payload)
    order = result['reseat_orders'][0]
    assert order['ticket_id'] == 'T-1'
    assert order['from_seat'] == 'A1'
    assert order['to_seat'] == 'B1'
    assert order['status'] == 'reassigned'
    stored = db.execute('SELECT * FROM reseat_order').fetchone()
    assert stored['to_seat'] == 'B1'
    db.close()


def test_one_alternative_cannot_be_occupied_by_two_tickets(tmp_path):
    # Two fixed-contract seats fail; only one contract-equivalent empty
    # alternative exists, so the second order must fail and name the ticket
    # already holding that alternative.
    payload = geo_payload(
        seats=[seat('A1'), seat('A2'), seat('A3'), seat('B1', x=0.5)],
        commitments=[
            {'ticket_id': 'T-1', 'seat_id': 'A1', 'mode': 'fixed'},
            {'ticket_id': 'T-3', 'seat_id': 'A3', 'mode': 'fixed'},
            {'ticket_id': 'T-2', 'seat_id': 'A2', 'mode': 'adjustable'}])
    db, result = fresh_payload(tmp_path, payload)
    orders = {o['ticket_id']: o for o in result['reseat_orders']}
    assert orders['T-1']['to_seat'] == 'B1'
    failed = orders['T-3']
    assert failed['status'] == 'failed'
    assert failed['to_seat'] is None
    assert 'T-1' in failed['conflict_tickets']
    db.close()


# --------------------------------------------------- program version lifecycle

def test_program_reversion_invalidates_and_recertifies_against_new_scenes(tmp_path):
    db = seeded(tmp_path)
    before = db.execute("SELECT state FROM seat_cert WHERE seat_id='A1'").fetchone()['state']
    assert before == 'certified'
    revised = read(ROOT / 'show-ok.json')
    revised['source_id'] = 'program-v2-sight'
    revised['show']['program'] = {'version': 2, 'signed_at': 950,
                                  'work_ids': ['work-1', 'work-2'],
                                  'scenes': ['scene-2']}
    with db:
        show.run(db, revised)
    rows = db.execute("SELECT state,program_version FROM seat_cert WHERE seat_id='A1' ORDER BY id").fetchall()
    assert rows[0]['state'] == 'superseded'
    assert rows[-1]['state'] == 'certified' and rows[-1]['program_version'] == 2
    # Only scene 2 (eye below 1.4) remains: the fixed seat now passes.
    latest = db.execute("SELECT status FROM seat_cert WHERE seat_id='A1' ORDER BY id DESC").fetchone()
    assert latest['status'] == 'sellable'
    # The original surveys and the sold-at version remain on the ledger.
    assert db.execute('SELECT COUNT(*) FROM venue_seat').fetchone()[0] == 2
    assert db.execute('SELECT COUNT(*) FROM moving_occluder').fetchone()[0] == 2
    db.close()


def test_venue_change_invalidates_without_overwriting_surveys(tmp_path):
    db = seeded(tmp_path)
    moved = read(ROOT / 'show-ok.json')
    moved['source_id'] = 'venue-move'
    moved['show']['venue'] = 'hall-B'
    with db:
        show.run(db, moved)
    certs = db.execute("SELECT DISTINCT state FROM seat_cert").fetchall()
    assert {r['state'] for r in certs} == {'invalidated'}
    # Raw geometry is preserved for audit even though it no longer applies.
    assert db.execute('SELECT COUNT(*) FROM venue_seat').fetchone()[0] == 2
    decision = db.execute(
        "SELECT status FROM decision WHERE system='sightline-cert' "
        "AND object_id='show-1:A1' ORDER BY id DESC LIMIT 1").fetchone()
    assert decision['status'] == 'invalidated'
    db.close()


def test_cancellation_invalidates_and_keeps_reseat_orders(tmp_path):
    payload = geo_payload(seats=[seat('A1'), seat('A2'), seat('B1', x=0.5)])
    db = connect(tmp_path / 'c.sqlite')
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        sightline.run(db, payload)
        assert db.execute('SELECT COUNT(*) FROM reseat_order').fetchone()[0] == 1
        cancelled = read(ROOT / 'show-ok.json')
        cancelled['source_id'] = 'cancel-sight'
        cancelled['show']['cancelled'] = True
        show.run(db, cancelled)
    assert {r['state'] for r in db.execute('SELECT DISTINCT state FROM seat_cert')} == {'invalidated'}
    # Issued re-seat orders are retained, never rewritten.
    order = db.execute('SELECT * FROM reseat_order').fetchone()
    assert order['status'] == 'reassigned' and order['to_seat'] == 'B1'
    db.close()


def test_time_window_only_occluder_attaches_to_overlapping_scene(tmp_path):
    # A temporary camera rig is measured without a scene label; its time
    # window determines which certified scene it obstructs.
    camera = {'id': 'rig-cam', 'scene_id': None, 'starts': 200, 'ends': 800,
              'box_lo': [2, -0.01, -10], 'box_hi': [2, 0.01, 1.25], 'err': 0}
    payload = geo_payload(source_id='sight-cam', seats=[seat('A1')],
                          occluders=[PANEL_LOW, PANEL_HIGH, camera],
                          commitments=[{'ticket_id': 'T-1', 'seat_id': 'A1',
                                        'mode': 'adjustable'}])
    db, result = fresh_payload(tmp_path, payload)
    verdict = result['seats'][0]['verdict']
    assert 'rig-cam' in [o['id'] for o in
                         verdict['detail']['scenes']['scene-1']['occluders']]
    assert 'rig-cam' not in [o['id'] for o in
                             verdict['detail']['scenes']['scene-2']['occluders']]
    db.close()


def test_three_dimensional_eye_range_uses_grid_with_error_box(tmp_path):
    # Eye may move in x and z; the grid fallback must clear the whole error
    # box rather than select one favourable ray.
    seat3d = {'id': 'seat-A1', 'seat_id': 'A1', 'position': [0, 0, 0],
              'eye_lo': [-0.2, 0, 1.2], 'eye_hi': [0.2, 0, 1.8]}
    payload = geo_payload(source_id='sight-3d', seats=[seat3d],
                          targets=[TARGET_1],
                          occluders=[dict(PANEL_LOW, err=0.02)],
                          commitments=[{'ticket_id': 'T-1', 'seat_id': 'A1',
                                        'mode': 'fixed'}])
    db, result = fresh_payload(tmp_path, payload)
    verdict = result['seats'][0]['verdict']
    assert verdict['detail']['method'] == 'grid_3d'
    # Leaning back to x=-0.2 steepens the ray enough to pass over 1.27.
    assert result['seats'][0]['status'] == 'sellable'
    posture = verdict['fixed']['posture']
    assert posture[0] <= -0.19 and posture[2] > 1.5
    db.close()


def test_sold_commitment_is_never_downgraded_after_obstruction_found(tmp_path):
    # A seat sold as full-visibility adjustable stays certified against that
    # exact contract; discovering geometry cannot rewrite the commitment.
    db = seeded(tmp_path)
    ticket = db.execute("SELECT mode,full_visibility FROM sight_commitment WHERE ticket_id='T-2'").fetchone()
    assert tuple(ticket) == ('adjustable', 1)
    second = geo_payload(source_id='sight-geo-3')
    second['commitments'] = [{'ticket_id': 'T-2', 'seat_id': 'A2',
                              'mode': 'fixed', 'full_visibility': False}]
    with db:
        sightline.run(db, second)
    unchanged = db.execute("SELECT mode,full_visibility FROM sight_commitment WHERE ticket_id='T-2'").fetchone()
    assert tuple(unchanged) == ('adjustable', 1)
    db.close()


# ------------------------------------------------------------- replay / report

def test_same_source_replay_is_idempotent(tmp_path):
    db = connect(tmp_path / 'r.sqlite')
    payload = geo_payload()
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        first = sightline.run(db, copy.deepcopy(payload))
        before = db.execute('SELECT COUNT(*) FROM seat_cert').fetchone()[0]
        again = sightline.run(db, copy.deepcopy(payload))
        after = db.execute('SELECT COUNT(*) FROM seat_cert').fetchone()[0]
    assert again['status'] == 'duplicate'
    assert before == after
    assert len(first['seats']) == 2
    db.close()


def test_different_bytes_same_source_id_conflicts(tmp_path):
    db = connect(tmp_path / 'x.sqlite')
    payload = geo_payload()
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        sightline.run(db, copy.deepcopy(payload))
        payload['occluders'][0]['err'] = 0.2
        result = sightline.run(db, copy.deepcopy(payload))
    assert result['status'] == 'conflict'
    assert db.execute('SELECT COUNT(*) FROM source_conflict').fetchone()[0] == 1
    db.close()


def test_report_carries_boundaries_sources_and_common_solution(tmp_path):
    db = seeded(tmp_path)
    view = report.build(db)
    cert = next(c for c in view['sightline_view']['certifications'] if c['seat_id'] == 'A1')
    import json
    parsed = cert['detail_parsed']
    scene = parsed['detail']['scenes']['scene-1']
    assert scene['target_boundaries'] == [[[4, 0, 1], [4, 0, 1]]]
    assert scene['occluders'][0]['source_id'] == 'sight-geo-1'
    assert parsed['fixed']['common_solution'] is False
    for table in ('venue_seat', 'stage_target', 'moving_occluder',
                  'sight_commitment', 'seat_cert', 'reseat_order'):
        assert table in view
    db.close()


def test_sightline_does_not_touch_royalty_settlement(tmp_path):
    db = connect(tmp_path / 'roy.sqlite')
    with db:
        show.run(db, read(ROOT / 'show-ok.json'))
        cue.run(db, read(ROOT / 'cue-ok.json'))
        rights.run(db, read(ROOT / 'rights-ok.json'))
        before = db.execute('SELECT SUM(amount) FROM royalty_case').fetchone()[0]
        events_before = db.execute('SELECT COUNT(*) FROM royalty_event').fetchone()[0]
        sightline.run(db, geo_payload())
        after = db.execute('SELECT SUM(amount) FROM royalty_case').fetchone()[0]
        events_after = db.execute('SELECT COUNT(*) FROM royalty_event').fetchone()[0]
    assert before == after == 450
    # Sightline certification appends no royalty events of its own.
    assert events_before == events_after
    db.close()
