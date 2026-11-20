"""Recording master stitching: offline continuity and provenance certificates.

For one show a master certificate answers two questions using only per-original
evidence (per-second block digests, sample rate, channel order, device clock
calibration and handover signatures):

1. is the whole performance continuous -- every seam is proven by shared
   overlap audio, never by time labels that merely abut;
2. can every output sample be traced to exactly one original -- a complete
   stitch that could come from several different originals is deliverable
   under a listening contract but only held for review under a forensic
   per-sample-unique-source contract.

Device drift is one value per original and must satisfy every seam at once;
channel inversion, left/right swap and fixed delay are corrected only when
calibration evidence is present.  Bad blocks, missing or late signatures,
same file number with different digests and same-source replay stay recorded
as conflicts.  Master stitching never writes royalty rows; show or cue events
only change the scope of the current certificate while older certificates
remain traceable.
"""
import itertools
import json
from .store import ingest,decide

SEAM_RATE_TOLERANCE_PPM=25   # effective sample-rate mismatch allowed at one seam
MIN_OVERLAP_SECONDS=1        # shared verified seconds required to prove a seam
CONTRACTS=('listening','forensic')

def run(db,data):
    state=ingest(db,'master',data)
    if state!='new': return {'source_id':data['source_id'],'status':state}
    show_id=data['show_id'];contract=data.get('contract')
    signed=db.execute('SELECT contract FROM master_contract WHERE show_id=?',(show_id,)).fetchone()
    if signed is None:
        if contract not in CONTRACTS: raise ValueError('contract must be listening or forensic')
        db.execute('INSERT INTO master_contract(show_id,contract,duration,source_id) VALUES(?,?,?,?)',
                   (show_id,contract,data.get('duration'),data['source_id']))
        decide(db,'master-contract',show_id,contract,'master contract fixed at signing',[data['source_id']])
    elif contract is not None and contract!=signed['contract']:
        decide(db,'master-contract',show_id,'conflict','contract fixed at signing cannot change before delivery',
               [data['source_id'],signed['contract'],contract])
    for seg in data.get('segments',[]): _add_segment(db,show_id,seg,data['source_id'])
    for sig in data.get('signatures',[]): _add_signature(db,sig,data['source_id'])
    for rev in data.get('revocations',[]): _add_revocation(db,rev,data['source_id'])
    cert=recompute_show(db,show_id)
    return {'source_id':data['source_id'],'show_id':show_id,'certificate':cert}

def _add_segment(db,show_id,seg,source_id):
    sid=seg['id'];starts=int(seg['starts']);ends=int(seg['ends'])
    if ends<=starts: raise ValueError('segment interval is empty')
    rate=int(seg['sample_rate']);channels=int(seg['channels'])
    blocks=seg['blocks']
    if len(blocks)!=ends-starts: raise ValueError('blocks must tile the declared interval one row per second')
    for i,block in enumerate(blocks):
        if 'index' in block and int(block['index'])!=i: raise ValueError('block index out of order')
        if len(block['digest'])!=channels: raise ValueError('block digest must carry one entry per channel')
    calib=seg.get('calibration') or {}
    delay=int(calib.get('delay_samples',0))
    if delay%rate: raise ValueError('delay calibration must be a whole number of seconds')
    old=db.execute('SELECT * FROM master_segment WHERE id=?',(sid,)).fetchone()
    if old:
        same=(old['file_no'],old['starts'],old['ends'],old['digest'])==(seg['file_no'],starts,ends,seg['digest'])
        decide(db,'master-segment',sid,'duplicate' if same else 'conflict',
               'same segment ID delivered again' if same else 'same segment ID changed bytes',[source_id])
        return
    db.execute('INSERT INTO master_segment(id,show_id,file_no,starts,ends,sample_rate,channels,channel_order,'
               'drift_ppm,drift_uncertainty_ppm,digest,calibration,handover_deadline,status,source_id) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (sid,show_id,seg['file_no'],starts,ends,rate,channels,seg.get('channel_order','LR'),
                float(seg.get('drift_ppm',0)),float(seg.get('drift_uncertainty_ppm',0)),seg['digest'],
                json.dumps(calib,sort_keys=True),seg.get('handover_deadline'),'received',source_id))
    for i,block in enumerate(blocks):
        db.execute('INSERT INTO master_block(segment_id,idx,digest,status) VALUES(?,?,?,?)',
                   (sid,i,json.dumps(block['digest']),block.get('status','ok')))
    for sig in seg.get('signatures',[]):
        db.execute('INSERT INTO master_signature(segment_id,signer,signed_at,valid,source_id) VALUES(?,?,?,?,?)',
                   (sid,sig['signer'],int(sig['signed_at']),int(bool(sig['valid'])),source_id))
    decide(db,'master-segment',sid,'received','original segment evidence retained',[source_id])

def _add_signature(db,sig,source_id):
    seg=db.execute('SELECT id FROM master_segment WHERE id=?',(sig['segment'],)).fetchone()
    if not seg:
        decide(db,'master-signature',sig['segment'],'unknown_segment','signature references unknown segment',[source_id]);return
    db.execute('INSERT INTO master_signature(segment_id,signer,signed_at,valid,source_id) VALUES(?,?,?,?,?)',
               (sig['segment'],sig['signer'],int(sig['signed_at']),int(bool(sig['valid'])),source_id))
    decide(db,'master-signature',sig['segment'],'appended','handover signature appended after delivery',[source_id])

def _add_revocation(db,rev,source_id):
    seg=db.execute('SELECT id FROM master_segment WHERE id=?',(rev['segment'],)).fetchone()
    if not seg:
        decide(db,'master-revocation',rev['segment'],'unknown_segment','revocation references unknown segment',[source_id]);return
    db.execute('INSERT INTO master_revocation(segment_id,reason,source_id) VALUES(?,?,?)',
               (rev['segment'],rev['reason'],source_id))
    decide(db,'master-revocation',rev['segment'],'revoked',rev['reason'],[source_id])

def _set_status(db,seg_id,status,reason,source_id):
    old=db.execute('SELECT status FROM master_segment WHERE id=?',(seg_id,)).fetchone()
    if old['status']==status: return
    db.execute('UPDATE master_segment SET status=? WHERE id=?',(status,seg_id))
    decide(db,'master-segment',seg_id,status,reason,[source_id])

def _usable_segments(db,show_id):
    rows=db.execute('SELECT * FROM master_segment WHERE show_id=? ORDER BY id',(show_id,)).fetchall()
    conflicts=[]
    by_file={}
    for r in rows: by_file.setdefault(r['file_no'],[]).append(r)
    for file_no,group in sorted(by_file.items()):
        digests={g['digest'] for g in group};ranges={(g['starts'],g['ends']) for g in group}
        if len(digests)>1:
            for g in group: _set_status(db,g['id'],'conflict','same file number with different original digests',g['source_id'])
            conflicts.append({'type':'file_number_digest_conflict','file_no':file_no,'segments':sorted(g['id'] for g in group)})
        elif len(ranges)>1:
            for g in group: _set_status(db,g['id'],'replay','same original samples offered for different show times',g['source_id'])
            conflicts.append({'type':'same_source_replay','file_no':file_no,'segments':sorted(g['id'] for g in group)})
        else:
            for g in sorted(group,key=lambda x:x['id'])[1:]:
                _set_status(db,g['id'],'duplicate','identical original delivered under a new segment ID',g['source_id'])
    revoked={r['segment_id'] for r in db.execute('SELECT segment_id FROM master_revocation')}
    for r in rows:
        cur=db.execute('SELECT status FROM master_segment WHERE id=?',(r['id'],)).fetchone()['status']
        if cur in ('conflict','replay','duplicate'): continue
        if r['id'] in revoked:
            _set_status(db,r['id'],'revoked','handover evidence revoked after delivery',r['source_id'])
            conflicts.append({'type':'revoked','segment':r['id']});continue
        sigs=db.execute('SELECT * FROM master_signature WHERE segment_id=?',(r['id'],)).fetchall()
        deadline=r['handover_deadline']
        ok=[s for s in sigs if s['valid'] and (deadline is None or s['signed_at']<=deadline)]
        if ok: _set_status(db,r['id'],'received','original segment evidence retained',r['source_id'])
        elif not sigs:
            _set_status(db,r['id'],'signature_missing','handover signature missing',r['source_id'])
            conflicts.append({'type':'signature_missing','segment':r['id']})
        elif any(s['valid'] for s in sigs):
            _set_status(db,r['id'],'signature_late','handover signature signed after the handover deadline',r['source_id'])
            conflicts.append({'type':'signature_late','segment':r['id']})
        else:
            _set_status(db,r['id'],'signature_invalid','handover signature invalid',r['source_id'])
            conflicts.append({'type':'signature_invalid','segment':r['id']})
    usable=[]
    for r in rows:
        status=db.execute('SELECT status FROM master_segment WHERE id=?',(r['id'],)).fetchone()['status']
        if status!='received': continue
        calib=json.loads(r['calibration'] or '{}')
        delay=int(calib.get('delay_samples',0))//r['sample_rate']
        blocks=[(json.loads(b['digest']),b['status']) for b in
                db.execute('SELECT digest,status FROM master_block WHERE segment_id=? ORDER BY idx',(r['id'],))]
        for i,(dig,st) in enumerate(blocks):
            if st!='ok': conflicts.append({'type':'bad_block','segment':r['id'],'second':r['starts']-delay+i})
        usable.append({'id':r['id'],'file_no':r['file_no'],'digest':r['digest'],
                       'start':r['starts']-delay,'end':r['ends']-delay,'rate':r['sample_rate'],
                       'drift':r['drift_ppm'],'unc':r['drift_uncertainty_ppm'],
                       'calibration':calib,'blocks':blocks})
    return usable,conflicts

def _transform(digest,calib):
    d=list(digest)
    if calib.get('swap_lr'): d=list(reversed(d))
    for ch in calib.get('invert',[]):
        if 0<=ch<len(d): d[ch]=d[ch][4:] if d[ch].startswith('inv:') else 'inv:'+d[ch]
    return d

def _rate_interval(seg):
    return (seg['rate']*(1+(seg['drift']-seg['unc'])/1e6),seg['rate']*(1+(seg['drift']+seg['unc'])/1e6))

def _tol(a,b): return SEAM_RATE_TOLERANCE_PPM/1e6*max(a['rate'],b['rate'])

def _seam(x,y):
    o1,o2=max(x['start'],y['start']),min(x['end'],y['end'])
    if o2<=o1: return None
    xlo,xhi=_rate_interval(x);ylo,yhi=_rate_interval(y);tol=_tol(x,y)
    if xlo-tol>yhi or ylo-tol>xhi: return {'ok':False,'reason':'rate_incompatible','overlap':[o1,o2]}
    verified=0;mismatch=[]
    for t in range(o1,o2):
        bx=x['blocks'][t-x['start']];by=y['blocks'][t-y['start']]
        if bx[1]!='ok' or by[1]!='ok': continue
        if _transform(bx[0],x['calibration'])==_transform(by[0],y['calibration']): verified+=1
        else: mismatch.append(t)
    if mismatch: return {'ok':False,'reason':'overlap_digest_mismatch','overlap':[o1,o2],'seconds':mismatch[:5]}
    if verified<MIN_OVERLAP_SECONDS: return {'ok':False,'reason':'no_verified_overlap','overlap':[o1,o2]}
    return {'ok':True,'overlap':[o1,o2],'verified':verified}

def _solve(segs,duration):
    conflicts=[]
    covered=set()
    for s in segs:
        for i,(dig,st) in enumerate(s['blocks']):
            t=s['start']+i
            if st=='ok' and 0<=t<duration: covered.add(t)
    gaps=[];t=0
    while t<duration:
        if t in covered: t+=1;continue
        a=t
        while t<duration and t not in covered: t+=1
        gaps.append([a,t])
    cache={}
    def seam(x,y):
        key=tuple(sorted((x['id'],y['id'])))
        if key not in cache: cache[key]=_seam(x,y)
        return cache[key]
    for i in range(len(segs)):
        for j in range(i+1,len(segs)):
            x,y=segs[i],segs[j];r=seam(x,y)
            if r is None:
                at=x['end'] if x['end']==y['start'] else (y['end'] if y['end']==x['start'] else None)
                if at is not None: conflicts.append({'type':'abutting_not_continuous','between':sorted([x['id'],y['id']]),'at':at})
            elif not r['ok']:
                conflicts.append({'type':r['reason'],'between':sorted([x['id'],y['id']]),'overlap':r['overlap']})
    def covers(sub):
        cov=set()
        for s in sub:
            for i,(dig,st) in enumerate(s['blocks']):
                t=s['start']+i
                if st=='ok' and 0<=t<duration: cov.add(t)
        return len(cov)==duration
    def connected(sub):
        parent={s['id']:s['id'] for s in sub}
        def find(a):
            while parent[a]!=a: parent[a]=parent[parent[a]];a=parent[a]
            return a
        for i in range(len(sub)):
            for j in range(i+1,len(sub)):
                r=seam(sub[i],sub[j])
                if r and r['ok']:
                    ra,rb=find(sub[i]['id']),find(sub[j]['id'])
                    if ra!=rb: parent[ra]=rb
        return len({find(s['id']) for s in sub})==1
    def rate_feasible(sub):
        dom={s['id']:list(_rate_interval(s)) for s in sub}
        edges=[(a,b) for i,a in enumerate(sub) for b in sub[i+1:] if (seam(a,b) or {}).get('ok')]
        for _ in range(len(sub)*len(sub)+1):
            changed=False
            for a,b in edges:
                tol=_tol(a,b)
                for u,v in ((a,b),(b,a)):
                    lo=max(dom[u['id']][0],dom[v['id']][0]-tol);hi=min(dom[u['id']][1],dom[v['id']][1]+tol)
                    if [lo,hi]!=dom[u['id']]: dom[u['id']]=[lo,hi];changed=True
            if not changed: break
        if any(lo>hi+1e-9 for lo,hi in dom.values()): return None
        return dom
    plans=[]
    for size in range(1,len(segs)+1):
        for combo in itertools.combinations(segs,size):
            ids=tuple(sorted(s['id'] for s in combo))
            if any(set(p)<set(ids) for p in plans): continue
            if not covers(combo) or not connected(combo): continue
            dom=rate_feasible(combo)
            if dom is None:
                conflicts.append({'type':'rate_globally_incompatible','segments':list(ids)});continue
            plans.append(ids)
    plan_dicts=[]
    for ids in plans:
        sub=[s for s in segs if s['id'] in ids]
        plan_dicts.append(_build_plan(sub,rate_feasible(sub),duration,seam))
    plan_dicts.sort(key=lambda p:[s['id'] for s in p['segments']])
    ambiguity=[]
    if len(plan_dicts)>1:
        t=0
        while t<duration:
            if len({p['source_at'][t] for p in plan_dicts})>1:
                a=t;sources=set()
                while t<duration and len({p['source_at'][t] for p in plan_dicts})>1:
                    sources|={p['source_at'][t] for p in plan_dicts};t+=1
                ambiguity.append({'interval':[a,t],'sources':sorted(s for s in sources if s)})
            else: t+=1
    for p in plan_dicts: del p['source_at']
    return {'plans':plan_dicts,'gaps':gaps,'conflicts':conflicts,'ambiguity':ambiguity}

def _build_plan(sub,dom,duration,seam):
    source_at=[]
    for t in range(duration):
        best=None
        for s in sub:
            i=t-s['start']
            if 0<=i<len(s['blocks']) and s['blocks'][i][1]=='ok':
                if best is None or (s['start'],s['id'])>(best['start'],best['id']): best=s
        source_at.append(best['id'] if best else None)
    segments=[]
    for s in sorted(sub,key=lambda x:(x['start'],x['id'])):
        takes=[];t=0
        while t<duration:
            if source_at[t]!=s['id']: t+=1;continue
            a=t
            while t<duration and source_at[t]==s['id']: t+=1
            i1,i2=a-s['start'],t-s['start']
            spb=round(sum(dom[s['id']])/2)
            takes.append({'output':[a,t],'blocks':[i1,i2],'samples':[i1*spb,i2*spb]})
        segments.append({'id':s['id'],'file_no':s['file_no'],'digest':s['digest'],'covers':[s['start'],s['end']],
                         'takes':takes,'calibration':s['calibration'],
                         'solved_drift_ppm':round((sum(dom[s['id']])/2/s['rate']-1)*1e6,3)})
    seams=[]
    for i in range(len(sub)):
        for j in range(i+1,len(sub)):
            r=seam(sub[i],sub[j])
            if r and r['ok']:
                seams.append({'between':sorted([sub[i]['id'],sub[j]['id']]),'overlap':r['overlap'],
                              'verified_seconds':r['verified']})
    return {'segments':segments,'seams':seams,'source_at':source_at}

def recompute_show(db,show_id):
    contract_row=db.execute('SELECT * FROM master_contract WHERE show_id=?',(show_id,)).fetchone()
    if not contract_row: return None
    contract=contract_row['contract']
    show=db.execute('SELECT * FROM show_slot WHERE id=?',(show_id,)).fetchone()
    duration=(show['ends']-show['starts']) if show else contract_row['duration']
    segs,conflicts=_usable_segments(db,show_id)
    plan=None;alternatives=[];gaps=[];ambiguity=[]
    if duration:
        solved=_solve(segs,int(duration))
        gaps=solved['gaps'];conflicts+=solved['conflicts'];ambiguity=solved['ambiguity']
        if solved['plans']: plan=solved['plans'][0];alternatives=solved['plans'][1:]
    continuous=plan is not None;unique=continuous and not alternatives
    if show is None or (not show['cancelled'] and show['status']!='scheduled'): status='pending_show'
    elif show['cancelled']: status='cancelled'
    elif not continuous: status='gap'
    elif contract=='listening' or unique: status='deliverable'
    else: status='held'
    scope={'show_status':show['status'] if show else None,'cancelled':bool(show and show['cancelled']),
           'actual_start':show['actual_start'] if show else None,
           'program_version':show['program_version'] if show else None,
           'cue_matches':[[r['cue_id'],r['status']] for r in db.execute(
               'SELECT m.cue_id,m.status FROM work_match m JOIN cue_evidence c ON c.id=m.cue_id WHERE c.show_id=? ORDER BY m.cue_id',
               (show_id,))]}
    body={'show_id':show_id,'contract':contract,'status':status,'continuous':continuous,'unique_source':unique,
          'plan':plan,'alternatives':alternatives,'gaps':gaps,'ambiguity':ambiguity,'conflicts':conflicts,'scope':scope}
    plan_json=json.dumps({'plan':plan,'alternatives':alternatives,'gaps':gaps,'ambiguity':ambiguity,'conflicts':conflicts},
                         ensure_ascii=False,sort_keys=True)
    scope_json=json.dumps(scope,ensure_ascii=False,sort_keys=True)
    last=db.execute('SELECT * FROM master_certificate WHERE show_id=? ORDER BY version DESC LIMIT 1',(show_id,)).fetchone()
    if last and (last['status'],last['continuous'],last['unique_source'],last['plan'],last['scope'])== \
                (status,int(continuous),int(unique),plan_json,scope_json):
        body['version']=last['version'];return body
    version=(last['version']+1) if last else 1
    db.execute('INSERT INTO master_certificate(show_id,version,contract,status,continuous,unique_source,plan,scope,source_id) '
               'VALUES(?,?,?,?,?,?,?,?,?)',
               (show_id,version,contract,status,int(continuous),int(unique),plan_json,scope_json,contract_row['source_id']))
    decide(db,'master-certificate',show_id,status,
           f'certificate version {version}: continuous={continuous}, unique_source={unique}',
           [show_id,version]+[s['id'] for s in segs])
    body['version']=version
    return body
