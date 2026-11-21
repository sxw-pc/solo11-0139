"""Recomputable settlement and sightline evidence view."""
import json

def build(db):
    tables=['show_slot','program_revision','cue_evidence','work_match','rights_claim',
            'royalty_case','royalty_event','seat_survey','seat_case','venue_seat',
            'stage_target','moving_occluder','sight_commitment','seat_cert','reseat_order',
            'decision']
    view={table:[dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
          for table in tables}
    view['sightline_view']=_sightline_view(db)
    return view

def _sightline_view(db):
    """Per-seat certification with parsed geometry: failing scenes, target
    boundaries, occluder sources, common-eye-solution availability and
    contract-equivalent alternative seats with double-booking conflicts."""
    out=[]
    for row in db.execute('SELECT * FROM seat_cert ORDER BY show_id,seat_id,id'):
        item=dict(row)
        try: item['detail_parsed']=json.loads(row['detail'])
        except (TypeError,ValueError): item['detail_parsed']={}
        out.append(item)
    orders=[dict(r) for r in db.execute('SELECT * FROM reseat_order ORDER BY id')]
    for order in orders:
        try: order['conflict_tickets']=json.loads(order['conflict_tickets'])
        except (TypeError,ValueError): pass
    return {'certifications':out,'reseat_orders':orders}
