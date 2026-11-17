"""Effective owner shares, disputed claims and reversible royalty events."""
from .store import ingest,decide
from .cue import union_seconds

def run(db,data):
    state=ingest(db,'rights',data)
    if state!='new': return {'source_id':data['source_id'],'status':state}
    for claim in data.get('claims',[]):
        if not 0<=int(claim['share'])<=100: raise ValueError('share outside 0..100')
        db.execute('INSERT INTO rights_claim(work_id,owner,share,starts,ends,version,source_id) VALUES(?,?,?,?,?,?,?)',
                   (claim['work_id'],claim['owner'],int(claim['share']),int(claim['starts']),int(claim['ends']),int(claim['version']),data['source_id']))
    for fee in data.get('fees',[]):
        old=db.execute('SELECT version FROM fee_rule WHERE work_id=?',(fee['work_id'],)).fetchone()
        if not old or int(fee['version'])>=old['version']:
            db.execute('INSERT INTO fee_rule(work_id,cents_per_second,version) VALUES(?,?,?) '
                       'ON CONFLICT(work_id) DO UPDATE SET cents_per_second=excluded.cents_per_second,version=excluded.version',
                       (fee['work_id'],int(fee['cents_per_second']),int(fee['version'])))
    out=[]
    for row in db.execute('SELECT id FROM show_slot').fetchall():
        out.extend(reconcile_show(db,row['id']))
    return {'source_id':data['source_id'],'cases':out}

def effective_shares(db,work_id,at):
    rows=db.execute('SELECT * FROM rights_claim WHERE work_id=? AND starts<=? AND ends>? ORDER BY owner,version DESC',
                    (work_id,at,at)).fetchall()
    by_owner={}; conflicts=[]
    for row in rows:
        owner=row['owner']
        if owner not in by_owner: by_owner[owner]=row
        elif row['version']==by_owner[owner]['version'] and row['share']!=by_owner[owner]['share']:
            conflicts.append(owner)
    total=sum(r['share'] for r in by_owner.values())
    return total,conflicts

def reconcile_show(db,show_id):
    show=db.execute('SELECT * FROM show_slot WHERE id=?',(show_id,)).fetchone()
    if not show: return []
    works={r['work_id'] for r in db.execute('SELECT DISTINCT work_id FROM work_match m JOIN cue_evidence c ON c.id=m.cue_id WHERE c.show_id=? AND work_id IS NOT NULL',(show_id,))}
    works.update(r['work_id'] for r in db.execute('SELECT work_id FROM royalty_case WHERE show_id=?',(show_id,)))
    output=[]
    for work in sorted(works):
        rows=db.execute('SELECT c.starts,c.ends,m.status FROM cue_evidence c JOIN work_match m ON c.id=m.cue_id WHERE c.show_id=? AND m.work_id=?',
                        (show_id,work)).fetchall()
        intervals=[(r['starts'],r['ends']) for r in rows if r['status']=='matched']
        seconds=union_seconds(intervals)
        share,claim_conflicts=effective_shares(db,work,show['starts'])
        fee=db.execute('SELECT cents_per_second FROM fee_rule WHERE work_id=?',(work,)).fetchone()
        cue_conflict=any(r['status']=='conflict' for r in rows)
        if show['cancelled']: status='cancelled'
        elif show['status']!='scheduled': status='pending_show'
        elif cue_conflict: status='cue_dispute'
        elif not seconds: status='unmatched'
        elif claim_conflicts or share!=100: status='rights_dispute'
        elif fee is None: status='pending_rate'
        else: status='settled'
        amount=seconds*fee['cents_per_second'] if status=='settled' else 0
        old=db.execute('SELECT amount,seconds,status,version FROM royalty_case WHERE show_id=? AND work_id=?',(show_id,work)).fetchone()
        if old and (old['amount'],old['seconds'],old['status'])==(amount,seconds,status):
            output.append({'show':show_id,'work':work,'status':status,'amount':amount});continue
        version=old['version']+1 if old else 1
        db.execute('INSERT INTO royalty_case(show_id,work_id,seconds,amount,status,version) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(show_id,work_id) DO UPDATE SET seconds=excluded.seconds,amount=excluded.amount,status=excluded.status,version=excluded.version',
                   (show_id,work,seconds,amount,status,version))
        delta=amount-(old['amount'] if old else 0)
        db.execute('INSERT INTO royalty_event(show_id,work_id,delta,reason,version) VALUES(?,?,?,?,?)',
                   (show_id,work,delta,status,version))
        decide(db,'royalty',f'{show_id}:{work}',status,f'{seconds} seconds, {share}% shares, {amount} cents',[show_id,work,version])
        output.append({'show':show_id,'work':work,'status':status,'amount':amount,'delta':delta})
    return output
