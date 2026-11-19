"""Record a signed prop packing sheet and direct barcode scans."""

import json

from .store import decide, ingest


def run(db, data):
    state = ingest(db, "props", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    show_id = data["show_id"]
    show = db.execute("SELECT * FROM show_slot WHERE id=?", (show_id,)).fetchone()
    if not show:
        raise ValueError("show not found")
    if not data.get("signer_a") or not data.get("signer_b") or data["signer_a"] == data["signer_b"]:
        raise ValueError("two packing signers required")
    works = set(json.loads(db.execute(
        "SELECT work_ids FROM program_revision WHERE show_id=? ORDER BY signed_at DESC,id DESC LIMIT 1",
        (show_id,),
    ).fetchone()["work_ids"]))
    required = set()
    for item in data["requirements"]:
        if item["work_id"] not in works:
            raise ValueError("prop requirement not in program")
        required.add(item["serial"])
        db.execute(
            "INSERT INTO prop_requirement(show_id,work_id,role,serial,source_id) VALUES(?,?,?,?,?)",
            (show_id, item["work_id"], item["role"], item["serial"], data["source_id"]),
        )
    scanned = set()
    for item in data["scans"]:
        if item["serial"] in scanned:
            continue
        scanned.add(item["serial"])
        db.execute("INSERT INTO prop_scan(id,show_id,serial,case_id,source_id) VALUES(?,?,?,?,?)",
                   (item["id"], show_id, item["serial"], item["case_id"], data["source_id"]))
    missing = sorted(required - scanned)
    status = "ready" if not missing else "incomplete"
    db.execute("INSERT INTO prop_case VALUES(?,?,?)", (show_id, status, json.dumps(missing)))
    decide(db, "props", show_id, status, "direct barcode membership", [data["source_id"]])
    return {"show": show_id, "status": status, "missing": missing}
