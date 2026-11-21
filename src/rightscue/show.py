"""Venue slot, signed program revision and actual opening evidence."""
import json
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'show',data)
    if state!='new': return {'source_id':data['source_id'],'status':state}
    s=data['show'];sid=s['id'];starts=int(s['starts']);ends=int(s['ends'])
    if ends<=starts: raise ValueError('invalid show interval')
    if s.get('extra_of') and s['extra_of']==sid: raise ValueError('extra show needs independent ID')
    existing=db.execute('SELECT * FROM show_slot WHERE id=?',(sid,)).fetchone()
    overlapping=db.execute('SELECT id FROM show_slot WHERE venue=? AND id<>? AND cancelled=0 AND starts<? AND ends>?',
                           (s['venue'],sid,ends,starts)).fetchall()
    overlap=bool(overlapping)
    cancelled=bool(s.get('cancelled',False))
    if overlap: status='conflict'
    elif cancelled: status='cancelled'
    else: status='scheduled'
    door=s.get('door_time');stage=s.get('stage_time')
    actual=int(stage) if door is not None and stage is not None and abs(int(door)-int(stage))<=300 else None
    if not cancelled and actual is None: status='pending_opening'
    decide(db,'slot',sid,'conflict' if overlap else 'accepted','venue interval overlap' if overlap else 'independent slot',
           [data['source_id']]+[r['id'] for r in overlapping])
    decide(db,'opening',sid,'confirmed' if actual is not None else 'pending','door and stage evidence within five minutes' if actual is not None else 'opening requires two sources',[data['source_id']])
    prior_version=existing['program_version'] if existing else -1
    prior_venue=existing['venue'] if existing else None
    program=s['program'];version=int(program['version']);signed=int(program['signed_at'])
    scenes=json.dumps(program['scenes']) if program.get('scenes') is not None else None
    if existing and signed<existing['signed_at']:
        decide(db,'program',sid,'stale','late older signed program rejected',[data['source_id']])
        version=prior_version;signed=existing['signed_at']
    else:
        db.execute('INSERT OR IGNORE INTO program_revision(show_id,version,signed_at,work_ids,source_id,scenes) VALUES(?,?,?,?,?,?)',
                   (sid,version,signed,json.dumps(program['work_ids']),data['source_id'],scenes))
        decide(db,'program',sid,'effective',f'program version {version} selected',[data['source_id']])
    db.execute('INSERT INTO show_slot(id,venue,starts,ends,cancelled,program_version,signed_at,actual_start,status,extra_of) VALUES(?,?,?,?,?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET venue=excluded.venue,starts=excluded.starts,ends=excluded.ends,cancelled=excluded.cancelled,program_version=excluded.program_version,signed_at=excluded.signed_at,actual_start=excluded.actual_start,status=excluded.status,extra_of=excluded.extra_of',
               (sid,s['venue'],starts,ends,int(cancelled),version,signed,actual,status,s.get('extra_of')))
    decide(db,'show',sid,status,'show status affects cue and payable royalty',[data['source_id']])
    _invalidate_sightline(db,sid,data['source_id'],
        program_changed=version!=prior_version,
        venue_changed=prior_venue is not None and prior_venue!=s['venue'],
        cancelled=cancelled)
    from .cue import rematch_show
    from .rights import reconcile_show
    rematch_show(db,sid)
    reconcile_show(db,sid)
    return {'show':sid,'status':status,'program_version':version,'actual_start':actual}

def _invalidate_sightline(db,sid,source_id,program_changed,venue_changed,cancelled):
    """Program revision, venue move and cancellation void prior sellable
    seat verdicts; raw surveys, the sold-at version and re-seat orders stay."""
    has_geometry=db.execute('SELECT 1 FROM venue_seat WHERE show_id=? LIMIT 1',(sid,)).fetchone()
    if not has_geometry: return
    reasons=[]
    if program_changed: reasons.append('program revision changed certified scenes/targets')
    if venue_changed: reasons.append('venue changed; prior geometry no longer applies')
    if cancelled: reasons.append('show cancelled')
    if not reasons: return
    if venue_changed or cancelled:
        # The old geometry cannot recertify anything; retain evidence only.
        db.execute('UPDATE seat_cert SET state="invalidated" WHERE show_id=? AND state="certified"',(sid,))
    for row in db.execute('SELECT DISTINCT seat_id FROM seat_cert WHERE show_id=?',(sid,)).fetchall():
        decide(db,'sightline-cert',f'{sid}:{row["seat_id"]}','invalidated','; '.join(reasons),
               [sid,source_id])
    if venue_changed or cancelled:
        return
    from .sightline import recertify_show
    recertify_show(db,sid,source_id,allocate=False)
