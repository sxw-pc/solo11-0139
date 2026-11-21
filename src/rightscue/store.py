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

-- Legacy packing intake.  Kept intact so earlier props payloads remain
-- replayable; the enhanced tables below are additive and never rewrite the
-- audio/royalty ledger.
CREATE TABLE IF NOT EXISTS prop_requirement(id INTEGER PRIMARY KEY,show_id TEXT,work_id TEXT,role TEXT,serial TEXT,source_id TEXT,capabilities TEXT DEFAULT '[]',commission_id TEXT);
CREATE TABLE IF NOT EXISTS prop_scan(id TEXT PRIMARY KEY,show_id TEXT,serial TEXT,case_id TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_case(show_id TEXT PRIMARY KEY,status TEXT,missing TEXT,program_version INTEGER,details TEXT,venue TEXT,updated_at INTEGER);

-- One physical prop unit (outer shell plus its declared serialized parts).
CREATE TABLE IF NOT EXISTS prop_unit(show_id TEXT,serial TEXT,work_id TEXT,role TEXT,capabilities TEXT,case_id TEXT,source_id TEXT,PRIMARY KEY(show_id,serial));
CREATE TABLE IF NOT EXISTS prop_component(show_id TEXT,prop_serial TEXT,serial TEXT,kind TEXT,capabilities TEXT,source_id TEXT,PRIMARY KEY(show_id,prop_serial,serial));
CREATE TABLE IF NOT EXISTS prop_pack(id INTEGER PRIMARY KEY,show_id TEXT,case_id TEXT,seal TEXT,packed_at INTEGER,signer_a TEXT,signer_b TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_pack_item(show_id TEXT,case_id TEXT,serial TEXT,prop_serial TEXT,work_id TEXT,role TEXT,capabilities TEXT,components TEXT,source_id TEXT,PRIMARY KEY(show_id,case_id,serial));
CREATE TABLE IF NOT EXISTS prop_unpack(id TEXT PRIMARY KEY,show_id TEXT,case_id TEXT,venue TEXT,signer_a TEXT,signer_b TEXT,seal_intact INTEGER,received_at INTEGER,status TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_unpack_item(unpack_id TEXT,show_id TEXT,case_id TEXT,serial TEXT,prop_serial TEXT,kind TEXT,present INTEGER,source_id TEXT,PRIMARY KEY(unpack_id,serial));
CREATE TABLE IF NOT EXISTS prop_conflict(id INTEGER PRIMARY KEY,show_id TEXT,serial TEXT,kind TEXT,reason TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_unpack_conflict(id INTEGER PRIMARY KEY,show_id TEXT,case_id TEXT,reason TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_substitution(id INTEGER PRIMARY KEY,cert_id TEXT,show_id TEXT,original_serial TEXT,substitute_serial TEXT,role TEXT,capabilities TEXT,valid_shows TEXT,valid_from INTEGER,valid_to INTEGER,signer_a TEXT,signer_b TEXT,signed_at INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_commission(commission_id TEXT PRIMARY KEY,show_id TEXT,scope TEXT,roles TEXT,signed_at INTEGER,signer_a TEXT,signer_b TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_return(id TEXT PRIMARY KEY,show_id TEXT,returned_at INTEGER,signer_a TEXT,signer_b TEXT,source_id TEXT);
CREATE TABLE IF NOT EXISTS prop_return_item(return_id TEXT,show_id TEXT,serial TEXT,present INTEGER,source_id TEXT,PRIMARY KEY(return_id,serial));
CREATE TABLE IF NOT EXISTS prop_loss(show_id TEXT,serial TEXT,status TEXT,first_seen TEXT,last_seen TEXT,PRIMARY KEY(show_id,serial));

-- Current and historical release views.  prop_release is append-only; the
-- latest row per show is the operative decision while earlier rows and the
-- decision chain remain readable.
CREATE TABLE IF NOT EXISTS prop_release(id INTEGER PRIMARY KEY,show_id TEXT,program_version INTEGER,status TEXT,reason TEXT,details TEXT,source_id TEXT,created_at INTEGER);
CREATE TABLE IF NOT EXISTS prop_release_role(release_id INTEGER,show_id TEXT,work_id TEXT,role TEXT,status TEXT,original_serial TEXT,physical_serial TEXT,path TEXT,certificate_id TEXT,required_capabilities TEXT,covered_capabilities TEXT,unmet_capabilities TEXT,components_used TEXT,conflicts TEXT,evidence TEXT);
"""

# Older databases were created before the enhanced columns existed.  SQLite
# CREATE IF NOT EXISTS does not widen a table, so add the columns explicitly.
_ADDITIONS = (
    ("prop_requirement", "capabilities", "capabilities TEXT DEFAULT '[]'"),
    ("prop_requirement", "commission_id", "commission_id TEXT"),
    ("prop_case", "program_version", "program_version INTEGER"),
    ("prop_case", "details", "details TEXT"),
    ("prop_case", "venue", "venue TEXT"),
    ("prop_case", "updated_at", "updated_at INTEGER"),
)


def _migrate(db):
    for table, column, declaration in _ADDITIONS:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if column not in columns:
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN {declaration}')


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ingest(db, system, data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    sid = data["source_id"]
    old = db.execute(
        "SELECT digest FROM source WHERE system=? AND source_id=?", (system, sid)
    ).fetchall()
    if any(r["digest"] != digest for r in old):
        db.execute(
            "INSERT INTO source_conflict(system,source_id,digest,payload) VALUES(?,?,?,?)",
            (system, sid, digest, raw),
        )
        decide(db, system, sid, "conflict", "same source ID changed bytes", [digest])
        return "conflict"
    if old:
        return "duplicate"
    db.execute(
        "INSERT INTO source(system,source_id,digest,payload) VALUES(?,?,?,?)",
        (system, sid, digest, raw),
    )
    return "new"


def decide(db, system, key, status, reason, evidence):
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    old = db.execute(
        "SELECT * FROM decision WHERE system=? AND object_id=? ORDER BY id DESC LIMIT 1",
        (system, key),
    ).fetchone()
    if old and (old["status"], old["reason"], old["evidence"]) == (status, reason, encoded):
        return old["id"]
    cur = db.execute(
        "INSERT INTO decision(system,object_id,status,reason,evidence,previous) VALUES(?,?,?,?,?,?)",
        (system, key, status, reason, encoded, old["id"] if old else None),
    )
    return cur.lastrowid
