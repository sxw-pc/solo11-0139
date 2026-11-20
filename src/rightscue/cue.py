"""Audio evidence identity, work authority matching and overlap accounting."""
import json
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'cue',data)
    if state!='new': return {'source_id':data['source_id'],'status':state}
    for work in data.get('catalog',[]):
        db.execute('INSERT OR IGNORE INTO work_catalog(id,title,composer,work_no) VALUES(?,?,?,?)',
                   (work['id'],work['title'],work['composer'],work['work_no']))
    out=[]
    for cue in data['cues']:
        cid=cue['id']; old=db.execute('SELECT digest,status FROM cue_evidence WHERE id=?',(cid,)).fetchone()
        if old:
            status='duplicate' if old['digest']==cue['digest'] else 'conflict'
            decide(db,'cue-identity',cid,status,'same cue ID with same or different bytes',[data['source_id']])
            if status=='conflict':
                db.execute('UPDATE work_match SET status="conflict" WHERE cue_id=?',(cid,))
                from .rights import reconcile_show
                reconcile_show(db,cue['show_id'])
            out.append({'id':cid,'status':status});continue
        show=db.execute('SELECT status FROM show_slot WHERE id=?',(cue['show_id'],)).fetchone()
        if int(cue['ends'])<=int(cue['starts']): raise ValueError('cue interval is empty')
        status='received' if show else 'pending_show'
        db.execute('INSERT INTO cue_evidence(id,show_id,starts,ends,digest,title,composer,work_no,status) VALUES(?,?,?,?,?,?,?,?,?)',
                   (cid,cue['show_id'],int(cue['starts']),int(cue['ends']),cue['digest'],cue['title'],cue['composer'],cue['work_no'],status))
        decide(db,'cue-identity',cid,status,'original audio evidence retained',[data['source_id']])
        out.append({'id':cid,'status':status})
    for show_id in {cue['show_id'] for cue in data['cues']}:
        rematch_show(db,show_id)
        from .rights import reconcile_show
        reconcile_show(db,show_id)
    return {'source_id':data['source_id'],'cues':out}

def rematch_show(db,show_id):
    show=db.execute('SELECT * FROM show_slot WHERE id=?',(show_id,)).fetchone()
    if not show:
        from .master import recompute_show
        recompute_show(db,show_id)
        return
    row=db.execute('SELECT work_ids FROM program_revision WHERE show_id=? AND version=?',
                   (show_id,show['program_version'])).fetchone()
    allowed=set(json.loads(row['work_ids'])) if row else set()
    for cue in db.execute('SELECT * FROM cue_evidence WHERE show_id=?',(show_id,)).fetchall():
        identity=db.execute('SELECT status FROM decision WHERE system="cue-identity" AND object_id=? ORDER BY id DESC LIMIT 1',(cue['id'],)).fetchone()
        if identity and identity['status']=='conflict':
            db.execute('UPDATE work_match SET status="conflict" WHERE cue_id=?',(cue['id'],))
            continue
        candidates=db.execute('SELECT id FROM work_catalog WHERE title=? AND composer=? AND work_no=?',
                              (cue['title'],cue['composer'],cue['work_no'])).fetchall()
        eligible=[x['id'] for x in candidates if x['id'] in allowed]
        if show['cancelled']: status='cancelled';work_id=None
        elif len(eligible)==1: status='matched';work_id=eligible[0]
        elif len(eligible)>1: status='ambiguous';work_id=None
        else: status='unmatched';work_id=None
        db.execute('INSERT INTO work_match(cue_id,work_id,status,reason) VALUES(?,?,?,?) '
                   'ON CONFLICT(cue_id) DO UPDATE SET work_id=excluded.work_id,status=excluded.status,reason=excluded.reason',
                   (cue['id'],work_id,status,'catalog and signed program version'))
        decide(db,'work-match',cue['id'],status,'title, composer and work number resolve within signed program',[show_id,show['program_version']])
    from .master import recompute_show
    recompute_show(db,show_id)

def union_seconds(intervals):
    total=0;last=None
    for start,end in sorted(intervals):
        if last is None: last=[start,end]
        elif start<=last[1]: last[1]=max(last[1],end)
        else: total+=last[1]-last[0];last=[start,end]
    if last: total+=last[1]-last[0]
    return total
