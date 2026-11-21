"""Recomputable settlement evidence view."""

import json

TABLES = [
    "show_slot",
    "program_revision",
    "cue_evidence",
    "work_match",
    "rights_claim",
    "royalty_case",
    "royalty_event",
    "prop_requirement",
    "prop_scan",
    "prop_case",
    "prop_unit",
    "prop_component",
    "prop_pack",
    "prop_pack_item",
    "prop_unpack",
    "prop_unpack_item",
    "prop_unpack_conflict",
    "prop_conflict",
    "prop_substitution",
    "prop_commission",
    "prop_return",
    "prop_return_item",
    "prop_loss",
    "prop_release",
    "prop_release_role",
    "decision",
]


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _prop_summary(db):
    """Per-show operative release with every role's physical evidence path.

    A bare ready/incomplete flag cannot be audited: the summary names the
    physical serial actually used, the certificate that authorised a
    substitution, exactly which capabilities were verified, which parts of a
    requirement remain unmet and where each conflict came from.
    """
    summary = []
    for show in db.execute("SELECT * FROM show_slot ORDER BY id").fetchall():
        row = db.execute(
            "SELECT * FROM prop_release WHERE show_id=? ORDER BY id DESC LIMIT 1", (show["id"],)
        ).fetchone()
        if not row:
            continue
        roles = []
        for role in db.execute(
            "SELECT * FROM prop_release_role WHERE release_id=? ORDER BY rowid", (row["id"],)
        ).fetchall():
            roles.append({
                "work_id": role["work_id"],
                "role": role["role"],
                "status": role["status"],
                "original_serial": role["original_serial"],
                "physical_serial": role["physical_serial"],
                "path": _json(role["path"], []),
                "certificate_id": role["certificate_id"],
                "required_capabilities": _json(role["required_capabilities"], []),
                "covered_capabilities": _json(role["covered_capabilities"], []),
                "unmet_capabilities": _json(role["unmet_capabilities"], []),
                "components_used": _json(role["components_used"], []),
                "conflicts": _json(role["conflicts"], []),
                "evidence_sources": _json(role["evidence"], []),
            })
        details = _json(row["details"], {})
        summary.append({
            "show": show["id"],
            "venue": show["venue"],
            "show_status": show["status"],
            "program_version": row["program_version"],
            "release_status": row["status"],
            "release_reason": row["reason"],
            "source_id": row["source_id"],
            "release_id": row["id"],
            "unmet": details.get("conflicts", []),
            "roles": roles,
            # Raw traceability: packing sheet, remote unpack and prior
            # decisions remain separately readable even after revalidation.
            "packs": [dict(r) for r in db.execute(
                "SELECT * FROM prop_pack WHERE show_id=? ORDER BY id", (show["id"],))],
            "unpacks": [dict(r) for r in db.execute(
                "SELECT * FROM prop_unpack WHERE show_id=? ORDER BY rowid", (show["id"],))],
            "certificates": [dict(r) for r in db.execute(
                "SELECT * FROM prop_substitution WHERE show_id=? ORDER BY id", (show["id"],))],
            "decisions": [dict(r) for r in db.execute(
                "SELECT * FROM decision WHERE system LIKE 'prop-%' AND object_id=? ORDER BY id",
                (show["id"],))],
        })
    return summary


def build(db):
    view = {}
    for table in TABLES:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        view[table] = [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
    view["prop_release_summary"] = _prop_summary(db)
    return view
