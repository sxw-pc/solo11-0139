"""Independent offline gate-scan intake and simple venue headcount."""
from .store import ingest, decide


def run(db, data):
    state = ingest(db, "attendance", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    affected = set()
    for scan in data["scans"]:
        if scan["action"] not in ("entry", "exit"):
            raise ValueError("unknown gate action")
        if not scan["id"] or not scan["ticket_id"]:
            raise ValueError("gate scan needs identity")
        old = db.execute(
            "SELECT show_id,ticket_id,gate,at,action FROM attendance_scan WHERE id=?",
            (scan["id"],),
        ).fetchone()
        identity = (scan["show_id"], scan["ticket_id"], scan["gate"], int(scan["at"]), scan["action"])
        if old and tuple(old) != identity:
            raise ValueError("same scan ID changed content")
        if not old:
            db.execute(
                "INSERT INTO attendance_scan VALUES (?,?,?,?,?,?,?)",
                (scan["id"], *identity, data["source_id"]),
            )
        affected.add(scan["show_id"])
    return {"source_id": data["source_id"], "cases": [summarize(db, show_id) for show_id in sorted(affected)]}


def summarize(db, show_id):
    scans = db.execute(
        "SELECT action FROM attendance_scan WHERE show_id=? ORDER BY at,id",
        (show_id,),
    ).fetchall()
    entries = sum(s["action"] == "entry" for s in scans)
    exits = sum(s["action"] == "exit" for s in scans)
    show = db.execute("SELECT id FROM show_slot WHERE id=?", (show_id,)).fetchone()
    status = "pending_show" if not show else "disputed" if exits > entries else "recorded"
    present = max(entries - exits, 0)
    db.execute(
        "INSERT INTO attendance_case VALUES (?,?,?,?,?) ON CONFLICT(show_id) "
        "DO UPDATE SET entries=excluded.entries,exits=excluded.exits,present=excluded.present,status=excluded.status",
        (show_id, entries, exits, present, status),
    )
    decide(db, "attendance", show_id, status, f"{entries} entries, {exits} exits", [show_id])
    return {"show_id": show_id, "entries": entries, "exits": exits, "present": present, "status": status}

