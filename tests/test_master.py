import json
import os
import subprocess
import sys
from pathlib import Path
from rightscue import show,cue,rights,report,master
from rightscue.store import connect,read

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'

def db(tmp_path,name='test'): return connect(tmp_path/f'{name}.sqlite')

def show_payload(duration=120,source_id='show-master-v1',**over):
    s={'id':'show-master','venue':'hall-B','starts':10000,'ends':10000+duration,'cancelled':False,
       'door_time':10030,'stage_time':10050,'program':{'version':1,'signed_at':9900,'work_ids':['work-1']}}
    s.update(over)
    return {'source_id':source_id,'show':s}

def seg(sid,file_no,start,end,digest,*,signer='engineer-1',signed_at=10130,valid=True,deadline=10200,
        bad=(),drift=0.0,unc=0.0,calib=None,invert_audio=(),swap_audio=False,unsigned=False,audio_shift=0):
    blocks=[]
    for t in range(start,end):
        real=t-audio_shift
        d=[f'wav-{real}-R',f'wav-{real}-L'] if swap_audio else [f'wav-{real}-L',f'wav-{real}-R']
        for ch in invert_audio: d[ch]='inv:'+d[ch]
        blocks.append({'index':t-start,'digest':d,'status':'bad' if t in bad else 'ok'})
    s={'id':sid,'file_no':file_no,'starts':start,'ends':end,'sample_rate':48000,'channels':2,
       'channel_order':'RL' if swap_audio else 'LR','drift_ppm':drift,'drift_uncertainty_ppm':unc,
       'digest':digest,'blocks':blocks,'handover_deadline':deadline}
    if calib: s['calibration']=calib
    s['signatures']=[] if unsigned else [{'signer':signer,'signed_at':signed_at,'valid':valid}]
    return s

def payload(contract,segments,source_id='master-v1',**extra):
    p={'source_id':source_id,'show_id':'show-master','contract':contract,'segments':segments}
    p.update(extra);return p

def abc():
    return [seg('seg-A','REC-A1',0,60,'original-A'),
            seg('seg-B','REC-B1',55,120,'original-B',signer='engineer-2'),
            seg('seg-B2','REC-B2',55,120,'original-B2',signer='engineer-3')]

def seeded(tmp_path,contract='listening',segments=None,duration=120,name='test'):
    cx=db(tmp_path,name)
    with cx:
        show.run(cx,show_payload(duration=duration))
        result=master.run(cx,payload(contract,segments if segments is not None else abc()))
    return cx,result['certificate']

def test_listening_contract_delivers_with_two_reproducible_paths(tmp_path):
    cx,cert=seeded(tmp_path,'listening')
    assert cert['status']=='deliverable' and cert['continuous'] and not cert['unique_source']
    assert cert['gaps']==[]
    plan=cert['plan']
    assert {s['id'] for s in plan['segments']}=={'seg-A','seg-B'}
    assert len(cert['alternatives'])==1
    assert {s['id'] for s in cert['alternatives'][0]['segments']}=={'seg-A','seg-B2'}
    takes={s['id']:s['takes'] for s in plan['segments']}
    assert takes['seg-A'][0]=={'output':[0,55],'blocks':[0,55],'samples':[0,55*48000]}
    assert takes['seg-B'][0]=={'output':[55,120],'blocks':[0,65],'samples':[0,65*48000]}
    assert plan['seams']==[{'between':['seg-A','seg-B'],'overlap':[55,60],'verified_seconds':5}]
    assert cert['ambiguity']==[{'interval':[55,120],'sources':['seg-B','seg-B2']}]

def test_forensic_contract_holds_when_source_not_unique(tmp_path):
    cx,cert=seeded(tmp_path,'forensic')
    assert cert['status']=='held' and cert['continuous'] and not cert['unique_source']
    assert cert['ambiguity'][0]['sources']==['seg-B','seg-B2']

def test_forensic_unique_after_signature_revoked(tmp_path):
    cx,cert=seeded(tmp_path,'forensic')
    assert cert['status']=='held'
    with cx:
        again=master.run(cx,{'source_id':'master-v2','show_id':'show-master',
                             'revocations':[{'segment':'seg-B2','reason':'signature_invalid'}]})
    cert2=again['certificate']
    assert cert2['status']=='deliverable' and cert2['unique_source'] and cert2['version']==2
    rows=cx.execute('SELECT version,status FROM master_certificate ORDER BY version').fetchall()
    assert [tuple(r) for r in rows]==[(1,'held'),(2,'deliverable')]
    assert cx.execute("SELECT status FROM master_segment WHERE id='seg-B2'").fetchone()[0]=='revoked'

def test_bad_block_repaired_by_overlapping_original(tmp_path):
    segments=[seg('seg-A','REC-A1',0,60,'original-A',bad=(59,)),
              seg('seg-B','REC-B1',55,120,'original-B',signer='engineer-2')]
    cx,cert=seeded(tmp_path,'forensic',segments)
    assert cert['status']=='deliverable' and cert['unique_source']
    takes={s['id']:s['takes'] for s in cert['plan']['segments']}
    assert takes['seg-A'][0]['output']==[0,55]
    assert takes['seg-B'][0]['output']==[55,120]
    assert cert['plan']['seams'][0]['verified_seconds']==4
    assert {'type':'bad_block','segment':'seg-A','second':59} in cert['conflicts']

def test_per_seam_rates_globally_incompatible(tmp_path):
    segments=[seg('seg-Y','REC-Y1',0,60,'orig-Y',drift=100.0),
              seg('seg-X','REC-X1',55,120,'orig-X',unc=150.0),
              seg('seg-Z','REC-Z1',115,180,'orig-Z',drift=-100.0)]
    cx,cert=seeded(tmp_path,'forensic',segments,duration=180)
    assert cert['status']=='gap' and not cert['continuous']
    kinds={c['type'] for c in cert['conflicts']}
    assert 'rate_globally_incompatible' in kinds
    assert 'rate_incompatible' not in kinds
    glob=[c for c in cert['conflicts'] if c['type']=='rate_globally_incompatible'][0]
    assert glob['segments']==['seg-X','seg-Y','seg-Z']

def test_channel_inversion_allowed_only_with_calibration(tmp_path):
    good=[seg('seg-A','REC-A1',0,60,'original-A'),
          seg('seg-B','REC-B1',55,120,'original-B',invert_audio=(0,),calib={'invert':[0]},signer='e2')]
    cx,cert=seeded(tmp_path,'listening',good)
    assert cert['status']=='deliverable'
    calib={s['id']:s['calibration'] for s in cert['plan']['segments']}
    assert calib['seg-B']=={'invert':[0]}
    raw=[seg('seg-A','REC-A1',0,60,'original-A'),
         seg('seg-B2','REC-B2',55,120,'original-B2',invert_audio=(0,),signer='e3')]
    cx2,cert2=seeded(tmp_path,'listening',raw,name='raw')
    assert cert2['status']=='gap'
    assert any(c['type']=='overlap_digest_mismatch' and c['between']==['seg-A','seg-B2']
               for c in cert2['conflicts'])

def test_swap_and_delay_allowed_only_with_calibration(tmp_path):
    good=[seg('seg-A','REC-A1',0,60,'original-A'),
          seg('seg-B','REC-B1',56,121,'original-B',swap_audio=True,audio_shift=1,
              calib={'swap_lr':True,'delay_samples':48000},signer='e2')]
    cx,cert=seeded(tmp_path,'listening',good)
    assert cert['status']=='deliverable'
    covers={s['id']:s['covers'] for s in cert['plan']['segments']}
    assert covers['seg-B']==[55,120]
    raw=[seg('seg-A','REC-A1',0,60,'original-A'),
         seg('seg-B2','REC-B2',56,121,'original-B2',swap_audio=True,audio_shift=1,signer='e3')]
    cx2,cert2=seeded(tmp_path,'listening',raw,name='raw')
    assert cert2['status']=='gap'
    assert any(c['type']=='overlap_digest_mismatch' for c in cert2['conflicts'])

def test_same_file_number_different_digest_keeps_conflict(tmp_path):
    segments=[seg('seg-C','REC-9',0,60,'orig-C1'),seg('seg-C2','REC-9',0,60,'orig-C2')]
    cx,cert=seeded(tmp_path,'listening',segments)
    assert cert['status']=='gap' and cert['gaps']==[[0,120]]
    assert any(c['type']=='file_number_digest_conflict' for c in cert['conflicts'])
    statuses={r['id']:r['status'] for r in cx.execute('SELECT id,status FROM master_segment')}
    assert statuses=={'seg-C':'conflict','seg-C2':'conflict'}

def test_missing_and_late_signatures_keep_conflict(tmp_path):
    missing=[seg('seg-A','REC-A1',0,60,'original-A'),
             seg('seg-B','REC-B1',55,120,'original-B',unsigned=True)]
    cx,cert=seeded(tmp_path,'listening',missing)
    assert cert['status']=='gap' and cert['gaps']==[[60,120]]
    assert any(c['type']=='signature_missing' and c['segment']=='seg-B' for c in cert['conflicts'])
    late=[seg('seg-A','REC-A1',0,60,'original-A'),
          seg('seg-B','REC-B1',55,120,'original-B',signer='e2'),
          seg('seg-B2','REC-B2',55,120,'original-B2',signer='e3',signed_at=10300)]
    cx2,cert2=seeded(tmp_path,'forensic',late,name='late')
    assert cert2['status']=='deliverable' and cert2['unique_source']
    assert any(c['type']=='signature_late' and c['segment']=='seg-B2' for c in cert2['conflicts'])

def test_late_arriving_valid_signature_issues_new_version(tmp_path):
    segments=[seg('seg-A','REC-A1',0,60,'original-A'),
              seg('seg-B','REC-B1',55,120,'original-B',unsigned=True)]
    cx,cert=seeded(tmp_path,'forensic',segments)
    assert cert['status']=='gap'
    with cx:
        again=master.run(cx,{'source_id':'master-v2','show_id':'show-master',
                             'signatures':[{'segment':'seg-B','signer':'engineer-2','signed_at':10140,'valid':True}]})
    assert again['certificate']['status']=='deliverable'
    rows=cx.execute('SELECT version,status FROM master_certificate ORDER BY version').fetchall()
    assert [tuple(r) for r in rows]==[(1,'gap'),(2,'deliverable')]

def test_abutting_time_labels_are_not_continuity(tmp_path):
    segments=[seg('seg-A','REC-A1',0,60,'original-A'),
              seg('seg-B','REC-B1',60,120,'original-B',signer='e2')]
    cx,cert=seeded(tmp_path,'listening',segments)
    assert cert['gaps']==[] and cert['status']=='gap' and not cert['continuous']
    assert {'type':'abutting_not_continuous','between':['seg-A','seg-B'],'at':60} in cert['conflicts']

def test_same_source_replay_rejected(tmp_path):
    segments=[seg('seg-R','REC-1',0,60,'orig-R'),seg('seg-R2','REC-1',60,120,'orig-R')]
    cx,cert=seeded(tmp_path,'listening',segments)
    assert cert['status']=='gap' and cert['gaps']==[[0,120]]
    assert any(c['type']=='same_source_replay' for c in cert['conflicts'])
    statuses={r['status'] for r in cx.execute('SELECT status FROM master_segment')}
    assert statuses=={'replay'}

def test_cancelled_show_changes_scope_old_certificate_traceable(tmp_path):
    cx,cert=seeded(tmp_path,'listening')
    assert cert['status']=='deliverable'
    with cx: show.run(cx,show_payload(source_id='cancel',cancelled=True))
    rows=cx.execute('SELECT version,status FROM master_certificate ORDER BY version').fetchall()
    assert [tuple(r) for r in rows]==[(1,'deliverable'),(2,'cancelled')]
    scope=json.loads(cx.execute('SELECT scope FROM master_certificate WHERE version=2').fetchone()[0])
    assert scope['cancelled'] is True

def test_actual_start_correction_issues_new_version(tmp_path):
    cx,cert=seeded(tmp_path,'listening')
    assert cert['scope']['actual_start']==10050
    with cx: show.run(cx,show_payload(source_id='opening-fix',door_time=10060,stage_time=10070))
    rows=cx.execute('SELECT version,scope FROM master_certificate ORDER BY version').fetchall()
    assert len(rows)==2 and json.loads(rows[0][1])['actual_start']==10050
    assert json.loads(rows[1][1])['actual_start']==10070

def test_cue_rematch_only_changes_certificate_scope(tmp_path):
    cx,cert=seeded(tmp_path,'listening')
    cues={'source_id':'cue-m1','catalog':[{'id':'work-1','title':'River Suite','composer':'Maker A','work_no':'R1'}],
          'cues':[{'id':'cue-m1','show_id':'show-master','starts':0,'ends':30,'digest':'sound-m1',
                   'title':'River Suite','composer':'Maker A','work_no':'R1'}]}
    with cx: cue.run(cx,cues)
    assert cx.execute('SELECT COUNT(*) FROM master_certificate').fetchone()[0]==2
    conflict={'source_id':'cue-m2','cues':[{'id':'cue-m1','show_id':'show-master','starts':0,'ends':30,
              'digest':'different-bytes','title':'River Suite','composer':'Maker A','work_no':'R1'}]}
    with cx: cue.run(cx,conflict)
    rows=cx.execute('SELECT version,scope FROM master_certificate ORDER BY version').fetchall()
    assert len(rows)==3
    assert json.loads(rows[2][1])['cue_matches']==[['cue-m1','conflict']]
    assert json.loads(rows[0][1])['cue_matches']==[]

def test_master_stitching_never_writes_royalty(tmp_path):
    cx=db(tmp_path)
    with cx:
        show.run(cx,read(FIX/'show-ok.json'))
        cue.run(cx,read(FIX/'cue-ok.json'))
        rights.run(cx,read(FIX/'rights-ok.json'))
    cases=[tuple(r) for r in cx.execute('SELECT * FROM royalty_case ORDER BY 1,2')]
    events=[tuple(r) for r in cx.execute('SELECT * FROM royalty_event ORDER BY 1')]
    with cx:
        show.run(cx,show_payload())
        master.run(cx,payload('listening',abc()))
        master.run(cx,{'source_id':'master-v2','show_id':'show-master',
                       'revocations':[{'segment':'seg-B2','reason':'signature_invalid'}]})
        show.run(cx,show_payload(source_id='cancel',cancelled=True))
    assert [tuple(r) for r in cx.execute('SELECT * FROM royalty_case ORDER BY 1,2')]==cases
    assert [tuple(r) for r in cx.execute('SELECT * FROM royalty_event ORDER BY 1')]==events

def test_contract_fixed_at_signing(tmp_path):
    cx,cert=seeded(tmp_path,'listening')
    with cx:
        master.run(cx,{'source_id':'master-v2','show_id':'show-master','contract':'forensic','segments':[]})
    row=cx.execute("SELECT status,reason FROM decision WHERE system='master-contract' ORDER BY id DESC LIMIT 1").fetchone()
    assert row['status']=='conflict' and 'fixed at signing' in row['reason']
    certs=cx.execute('SELECT contract,status FROM master_certificate').fetchall()
    assert len(certs)==1 and certs[0]['contract']=='listening'

def test_duplicate_delivery_is_idempotent(tmp_path):
    cx,cert=seeded(tmp_path,'listening')
    with cx: again=master.run(cx,payload('listening',abc()))
    assert again['status']=='duplicate'
    assert cx.execute('SELECT COUNT(*) FROM master_certificate').fetchone()[0]==1

def test_report_lists_master_evidence(tmp_path):
    cx,cert=seeded(tmp_path,'forensic')
    view=report.build(cx)
    assert len(view['master_certificate'])==1
    assert len(view['master_segment'])==3
    assert len(view['master_block'])==60+65+65
    assert len(view['master_signature'])==3
    assert view['master_contract'][0]['contract']=='forensic'

def test_cli_master_command(tmp_path):
    env=dict(os.environ,PYTHONPATH=str(ROOT/'src'))
    db_path=tmp_path/'cli.sqlite'
    for command,fixture in (('show','show-master.json'),('master','master-ok.json')):
        out=subprocess.run([sys.executable,'-m','rightscue',command,'--db',str(db_path),'--input',str(FIX/fixture)],
                           capture_output=True,text=True,cwd=ROOT,env=env)
        assert out.returncode==0,out.stderr
    result=json.loads(out.stdout)['result']
    assert result['certificate']['status']=='deliverable'
    assert result['certificate']['continuous'] and not result['certificate']['unique_source']
