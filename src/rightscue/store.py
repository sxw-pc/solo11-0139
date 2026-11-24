"""Append-only source and decision ledger for offline performance rights."""
import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA="""
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT,UNIQUE(system,source_id,digest));
CREATE TABLE IF NOT EXISTS source_conflict(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,system TEXT,object_id TEXT,status TEXT,reason TEXT,evidence TEXT,previous INTEGER);
CREATE TABLE IF NOT EXISTS show_slot(id TEXT PRIMARY KEY,venue TEXT,starts INTEGER,ends INTEGER,cancelled INTEGER,program_version INTEGER,signed_at INTEGER,actual_start INTEGER,status TEXT,extra_of TEXT);
CREATE TABLE IF NOT EXISTS program_revision(id INTEGER PRIMARY KEY,show_id TEXT,version INTEGER,signed_at INTEGER,work_ids TEXT,source_id TEXT,UNIQUE(show_id,version));
CREATE TABLE IF NOT EXISTS cue_evidence(id TEXT PRIMARY KEY,show_id TEXT,starts INTEGER,ends INTEGER,digest TEXT,title TEXT,composer TEXT,work_no TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS work_catalog(id TEXT PRIMARY KEY,title TEXT,composer TEXT,work_no TEXT);
CREATE TABLE IF NOT EXISTS work_match(cue_id TEXT PRIMARY KEY,work_id TEXT,status TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS rights_claim(id INTEGER PRIMARY KEY,work_id TEXT,owner TEXT,share INTEGER,starts INTEGER,ends INTEGER,version INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS fee_rule(work_id TEXT PRIMARY KEY,cents_per_second INTEGER,version INTEGER);
CREATE TABLE IF NOT EXISTS royalty_case(show_id TEXT,work_id TEXT,seconds INTEGER,amount INTEGER,status TEXT,version INTEGER,PRIMARY KEY(show_id,work_id));
CREATE TABLE IF NOT EXISTS royalty_event(id INTEGER PRIMARY KEY,show_id TEXT,work_id TEXT,delta INTEGER,reason TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS audio_probe(id TEXT PRIMARY KEY,show_id TEXT,device_id TEXT,starts INTEGER,rate INTEGER,samples_json TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS audio_health(probe_id TEXT PRIMARY KEY,peak REAL,rms REAL,clipped INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS sound_calibration(id TEXT PRIMARY KEY,show_id TEXT,rate INTEGER,measured_at INTEGER,layout_json TEXT,ingested_at INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS sound_pulse(id TEXT PRIMARY KEY,calibration_id TEXT,speaker_id TEXT,label TEXT,digest TEXT,samples_json TEXT,rate INTEGER,independent INTEGER,conflict INTEGER DEFAULT 0,source_id TEXT,UNIQUE(calibration_id,label));
CREATE TABLE IF NOT EXISTS sound_response(id INTEGER PRIMARY KEY,pulse_id TEXT,mic_id TEXT,lag_start INTEGER,samples_json TEXT,clipped INTEGER,missing INTEGER,length INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS sound_response_clip(pulse_id TEXT,mic_id TEXT,lag INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS gain_calibration(show_id TEXT,version INTEGER,signed_at INTEGER,common_error REAL,source_id TEXT,PRIMARY KEY(show_id,version));
CREATE TABLE IF NOT EXISTS wiring_contract(id TEXT,show_id TEXT,version INTEGER,signed_at INTEGER,mapping_json TEXT,gain_ranges_json TEXT,peak_limits_json TEXT,source_id TEXT,status TEXT,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS sound_fir(id INTEGER PRIMARY KEY,show_id TEXT,calibration_id TEXT,speaker_id TEXT,mic_id TEXT,lag INTEGER,value REAL,lo REAL,hi REAL,quality TEXT,source_id TEXT,UNIQUE(calibration_id,speaker_id,mic_id,lag));
CREATE TABLE IF NOT EXISTS sound_license(id INTEGER PRIMARY KEY,show_id TEXT,calibration_id TEXT,contract_id TEXT,contract_version INTEGER,gain_version INTEGER,status TEXT,gain_ranges_json TEXT,verdict_json TEXT,superseded INTEGER DEFAULT 0,reason TEXT,source_id TEXT);
"""

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db

def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))

def ingest(db,system,data):
    raw=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    digest=hashlib.sha256(raw.encode()).hexdigest(); sid=data['source_id']
    old=db.execute('SELECT digest FROM source WHERE system=? AND source_id=?',(system,sid)).fetchall()
    if any(r['digest']!=digest for r in old):
        db.execute('INSERT INTO source_conflict(system,source_id,digest,payload) VALUES(?,?,?,?)',(system,sid,digest,raw))
        decide(db,system,sid,'conflict','same source ID changed bytes',[digest])
        return 'conflict'
    if old: return 'duplicate'
    db.execute('INSERT INTO source(system,source_id,digest,payload) VALUES(?,?,?,?)',(system,sid,digest,raw))
    return 'new'

def decide(db,system,key,status,reason,evidence):
    encoded=json.dumps(evidence,ensure_ascii=False,sort_keys=True)
    old=db.execute('SELECT * FROM decision WHERE system=? AND object_id=? ORDER BY id DESC LIMIT 1',(system,key)).fetchone()
    if old and (old['status'],old['reason'],old['evidence'])==(status,reason,encoded): return old['id']
    cur=db.execute('INSERT INTO decision(system,object_id,status,reason,evidence,previous) VALUES(?,?,?,?,?,?)',
                   (system,key,status,reason,encoded,old['id'] if old else None))
    return cur.lastrowid
