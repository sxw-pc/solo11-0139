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
CREATE TABLE IF NOT EXISTS seat_survey(id TEXT PRIMARY KEY,show_id TEXT,seat_id TEXT,visual_clear INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS seat_case(show_id TEXT,seat_id TEXT,status TEXT,PRIMARY KEY(show_id,seat_id));
CREATE TABLE IF NOT EXISTS venue_seat(source_id TEXT,id TEXT,show_id TEXT,venue TEXT,seat_id TEXT,position TEXT,eye_lo TEXT,eye_hi TEXT,PRIMARY KEY(source_id,id));
CREATE TABLE IF NOT EXISTS stage_target(source_id TEXT,id TEXT,show_id TEXT,scene_id TEXT,starts INTEGER,ends INTEGER,boundary TEXT,PRIMARY KEY(source_id,id));
CREATE TABLE IF NOT EXISTS moving_occluder(source_id TEXT,id TEXT,show_id TEXT,venue TEXT,scene_id TEXT,starts INTEGER,ends INTEGER,box_lo TEXT,box_hi TEXT,err TEXT,state TEXT,PRIMARY KEY(source_id,id));
CREATE TABLE IF NOT EXISTS sight_commitment(ticket_id TEXT PRIMARY KEY,show_id TEXT,seat_id TEXT,mode TEXT,full_visibility INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS seat_cert(id INTEGER PRIMARY KEY AUTOINCREMENT,show_id TEXT,seat_id TEXT,program_version INTEGER,venue TEXT,state TEXT,status TEXT,reason TEXT,detail TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS reseat_order(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id TEXT,show_id TEXT,from_seat TEXT,to_seat TEXT,status TEXT,reason TEXT,conflict_tickets TEXT,program_version INTEGER,source_id TEXT);
"""

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    cols={r['name'] for r in db.execute('PRAGMA table_info(program_revision)')}
    if 'scenes' not in cols:
        db.execute('ALTER TABLE program_revision ADD COLUMN scenes TEXT')
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
