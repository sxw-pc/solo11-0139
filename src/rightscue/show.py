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
    program=s['program'];version=int(program['version']);signed=int(program['signed_at'])
    if existing and signed<existing['signed_at']:
        decide(db,'program',sid,'stale','late older signed program rejected',[data['source_id']])
        version=prior_version;signed=existing['signed_at']
    else:
        db.execute('INSERT OR IGNORE INTO program_revision(show_id,version,signed_at,work_ids,source_id) VALUES(?,?,?,?,?)',
                   (sid,version,signed,json.dumps(program['work_ids']),data['source_id']))
        decide(db,'program',sid,'effective',f'program version {version} selected',[data['source_id']])
    db.execute('INSERT INTO show_slot(id,venue,starts,ends,cancelled,program_version,signed_at,actual_start,status,extra_of) VALUES(?,?,?,?,?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET venue=excluded.venue,starts=excluded.starts,ends=excluded.ends,cancelled=excluded.cancelled,program_version=excluded.program_version,signed_at=excluded.signed_at,actual_start=excluded.actual_start,status=excluded.status,extra_of=excluded.extra_of',
               (sid,s['venue'],starts,ends,int(cancelled),version,signed,actual,status,s.get('extra_of')))
    decide(db,'show',sid,status,'show status affects cue and payable royalty',[data['source_id']])
    from .cue import rematch_show
    from .rights import reconcile_show
    rematch_show(db,sid)
    reconcile_show(db,sid)
    for probe in db.execute('SELECT id FROM audio_probe WHERE show_id=?',(sid,)).fetchall():
        from .audio_probe import summarize
        summarize(db,probe['id'])
    return {'show':sid,'status':status,'program_version':version,'actual_start':actual}
