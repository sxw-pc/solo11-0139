from pathlib import Path
from rightscue import show,cue,rights,report
from rightscue.store import connect,read

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def fixture(name):return read(ROOT/name)
def db(tmp_path):return connect(tmp_path/'test.sqlite')
def seeded(tmp_path):
    cx=db(tmp_path)
    with cx:
        show.run(cx,fixture('show-ok.json'))
        cue.run(cx,fixture('cue-ok.json'))
        rights.run(cx,fixture('rights-ok.json'))
    return cx

def test_show_overlap_freezes_other_slot(tmp_path):
    cx=db(tmp_path)
    with cx:
        show.run(cx,fixture('show-ok.json'))
        conflict=show.run(cx,fixture('show-conflict.json'))
    assert conflict['status']=='conflict'
    assert cx.execute('SELECT status FROM show_slot WHERE id="show-1"').fetchone()[0]=='scheduled'

def test_program_signed_version_and_opening(tmp_path):
    cx=db(tmp_path)
    with cx: show.run(cx,fixture('show-ok.json'))
    old=fixture('show-ok.json');old['source_id']='late-old'
    old['show']['program']['version']=0;old['show']['program']['signed_at']=800
    old['show']['door_time']=None
    with cx: result=show.run(cx,old)
    assert result['program_version']==1
    assert result['status']=='pending_opening'

def test_cue_match_and_overlap_union(tmp_path):
    cx=seeded(tmp_path)
    work=cx.execute('SELECT seconds,amount FROM royalty_case WHERE work_id="work-1"').fetchone()
    assert tuple(work)==(150,300)
    assert cx.execute('SELECT status FROM work_match WHERE cue_id="cue-1"').fetchone()[0]=='matched'

def test_rights_share_conflict_freezes_one_work(tmp_path):
    cx=seeded(tmp_path)
    with cx: rights.run(cx,fixture('rights-conflict.json'))
    first=cx.execute('SELECT status,amount FROM royalty_case WHERE work_id="work-1"').fetchone()
    other=cx.execute('SELECT status,amount FROM royalty_case WHERE work_id="work-2"').fetchone()
    assert tuple(first)==('rights_dispute',0)
    assert tuple(other)==('settled',150)

def test_cue_identity_conflict_freezes_only_related_work(tmp_path):
    cx=seeded(tmp_path)
    with cx: cue.run(cx,fixture('cue-conflict.json'))
    assert cx.execute('SELECT status FROM royalty_case WHERE work_id="work-1"').fetchone()[0]=='cue_dispute'
    assert cx.execute('SELECT status FROM royalty_case WHERE work_id="work-2"').fetchone()[0]=='settled'
    assert cx.execute('SELECT digest FROM cue_evidence WHERE id="cue-1"').fetchone()[0]=='sound-a'

def test_cancelled_show_reverses_royalty(tmp_path):
    cx=seeded(tmp_path)
    cancelled=fixture('show-ok.json');cancelled['source_id']='cancel';cancelled['show']['cancelled']=True
    with cx: show.run(cx,cancelled)
    assert cx.execute('SELECT SUM(amount) FROM royalty_case').fetchone()[0]==0
    assert cx.execute('SELECT SUM(delta) FROM royalty_event').fetchone()[0]==0
    assert cx.execute('SELECT COUNT(*) FROM royalty_event WHERE delta<0').fetchone()[0]==2

def test_program_revision_reverses_unlisted_work(tmp_path):
    cx=seeded(tmp_path)
    revised=fixture('show-ok.json');revised['source_id']='program-v2'
    revised['show']['program']={'version':2,'signed_at':950,'work_ids':['work-2']}
    with cx: show.run(cx,revised)
    assert cx.execute('SELECT status FROM work_match WHERE cue_id="cue-1"').fetchone()[0]=='unmatched'
    assert cx.execute('SELECT amount FROM royalty_case WHERE work_id="work-1"').fetchone()[0]==0
    assert cx.execute('SELECT amount FROM royalty_case WHERE work_id="work-2"').fetchone()[0]==150

def test_duplicate_source_idempotent(tmp_path):
    cx=seeded(tmp_path)
    before=cx.execute('SELECT COUNT(*) FROM royalty_event').fetchone()[0]
    with cx:result=rights.run(cx,fixture('rights-ok.json'))
    assert result['status']=='duplicate'
    assert cx.execute('SELECT COUNT(*) FROM royalty_event').fetchone()[0]==before

def test_evidence_report_preserves_reversals(tmp_path):
    cx=seeded(tmp_path)
    cancelled=fixture('show-ok.json');cancelled['source_id']='cancel';cancelled['show']['cancelled']=True
    with cx:show.run(cx,cancelled)
    view=report.build(cx)
    assert len([r for r in view['royalty_event'] if r['delta']<0])==2
    assert len(view['program_revision'])==1

def test_extra_show_requires_new_identity(tmp_path):
    cx=db(tmp_path)
    invalid=fixture('show-ok.json');invalid['source_id']='extra-invalid'
    invalid['show']['extra_of']='show-1'
    try:
        with cx:show.run(cx,invalid)
    except ValueError as exc:
        assert 'independent ID' in str(exc)
    else: raise AssertionError('same ID accepted for extra performance')

def test_ambiguous_work_authority_is_not_payable(tmp_path):
    cx=db(tmp_path)
    program=fixture('show-ok.json');program['show']['program']['work_ids'].append('work-1b')
    cues=fixture('cue-ok.json');cues['catalog'].append({'id':'work-1b','title':'River Suite',
        'composer':'Maker A','work_no':'R1'})
    with cx:
        show.run(cx,program)
        cue.run(cx,cues)
        rights.run(cx,fixture('rights-ok.json'))
    assert cx.execute('SELECT status FROM work_match WHERE cue_id="cue-1"').fetchone()[0]=='ambiguous'
    assert cx.execute('SELECT COUNT(*) FROM royalty_case WHERE work_id="work-1" AND amount>0').fetchone()[0]==0

def test_new_claim_version_supersedes_prior_owner_share(tmp_path):
    cx=seeded(tmp_path)
    revised={'source_id':'share-v2','claims':[{'work_id':'work-1','owner':'owner-A',
        'share':60,'starts':0,'ends':9000,'version':2}], 'fees':[]}
    with cx:rights.run(cx,revised)
    assert tuple(cx.execute('SELECT status,amount FROM royalty_case WHERE work_id="work-1"').fetchone())==('settled',300)
