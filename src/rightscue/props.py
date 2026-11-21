"""Prop packing, remote unpacking, substitution evidence and release decisions.

The legacy intake in this module only compared a required serial with a
barcode scan.  That is kept replayable, but it is no longer sufficient for an
enhanced release: a case barcode proves a case was packed, not that the signed
unpack found every serialized part inside it.  Enhanced payloads therefore
carry an explicit ``kind`` and are evaluated against the physical count, the
double-signed remote unpack, the seal and the current program revision.

Release is evaluated by role.  A role succeeds only when the physical unit was
counted in a valid signed unpack, every declared serialized component was
counted with it, the role's performance capabilities are actually provided
(either by the unit itself or by a signed substitution certificate that names
those exact capabilities), and no overlapping show physically claims the same
component.  Appearance and sound are separate capabilities: a certificate that
verifies only sound never satisfies a camera appearance requirement.
"""

import json

from .store import decide, ingest


# ---------------------------------------------------------------------------
# Helpers


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _show(db, show_id):
    show = db.execute("SELECT * FROM show_slot WHERE id=?", (show_id,)).fetchone()
    if not show:
        raise ValueError("show not found")
    return show


def _program(db, show_id):
    show = _show(db, show_id)
    row = None
    if show["program_version"] is not None and show["program_version"] >= 0:
        row = db.execute(
            "SELECT * FROM program_revision WHERE show_id=? AND version=?",
            (show_id, show["program_version"]),
        ).fetchone()
    if not row:
        row = db.execute(
            "SELECT * FROM program_revision WHERE show_id=? ORDER BY signed_at DESC,id DESC LIMIT 1",
            (show_id,),
        ).fetchone()
    if not row:
        return None, None, set()
    return row["version"], row["signed_at"], set(_json(row["work_ids"], []))


def _caps(data):
    return sorted(set(data or []))


def _signers_ok(data, prefix=""):
    a = data.get(prefix + "signer_a")
    b = data.get(prefix + "signer_b")
    return bool(a) and bool(b) and a != b


def _record_conflict(db, show_id, serial, kind, reason, source_id):
    old = db.execute(
        "SELECT 1 FROM prop_conflict WHERE show_id=? AND serial=? AND kind=? AND reason=?",
        (show_id, serial, kind, reason),
    ).fetchone()
    if old:
        return
    db.execute(
        "INSERT INTO prop_conflict(show_id,serial,kind,reason,source_id) VALUES(?,?,?,?,?)",
        (show_id, serial, kind, reason, source_id),
    )


def _evidence_kind(data):
    kind = data.get("kind") or data.get("type")
    aliases = {
        "pack": "packing", "intake": "packing", "packing_sheet": "packing",
        "open": "unpack", "receiving": "unpack", "receipt": "unpack",
        "certificate": "substitution", "alternate": "substitution",
        "delegation": "commission", "contract": "commission",
        "giveback": "return", "returned": "return",
    }
    if kind:
        return aliases.get(kind, kind)
    # No explicit kind: only infer when the payload cannot be the legacy
    # barcode intake (which has requirements + scans and no lifecycle fields).
    if data.get("packed_at") or data.get("seal"):
        return "packing"
    if data.get("seal_intact") is not None or data.get("items") is not None:
        return "unpack"
    if data.get("cert_id"):
        return "substitution"
    if data.get("commission_id"):
        return "commission"
    if data.get("returned_at") is not None or data.get("return_id"):
        return "return"
    return None


# ---------------------------------------------------------------------------
# Evidence intake


def run(db, data):
    state = ingest(db, "props", data)
    if state != "new":
        return {"source_id": data["source_id"], "status": state}
    kind = _evidence_kind(data)
    if not kind:
        return _legacy_pack(db, data)
    handler = {
        "packing": _pack,
        "unpack": _unpack,
        "substitution": _substitution,
        "commission": _commission,
        "return": _return,
    }.get(kind)
    if handler is None:
        raise ValueError(f"unknown prop evidence kind: {kind}")
    return handler(db, data)


def _legacy_pack(db, data):
    show_id = data["show_id"]
    show = _show(db, show_id)
    if not _signers_ok(data):
        raise ValueError("two packing signers required")
    version, _signed, works = _program(db, show_id)
    required = set()
    for item in data["requirements"]:
        if item["work_id"] not in works:
            raise ValueError("prop requirement not in program")
        required.add(item["serial"])
        db.execute(
            "INSERT INTO prop_requirement(show_id,work_id,role,serial,source_id,capabilities) VALUES(?,?,?,?,?,?)",
            (show_id, item["work_id"], item["role"], item["serial"], data["source_id"], "[]"),
        )
    scanned = set()
    for item in data["scans"]:
        if item["serial"] in scanned:
            continue
        scanned.add(item["serial"])
        db.execute(
            "INSERT INTO prop_scan(id,show_id,serial,case_id,source_id) VALUES(?,?,?,?,?)",
            (item["id"], show_id, item["serial"], item["case_id"], data["source_id"]),
        )
    missing = sorted(required - scanned)
    status = "ready" if not missing else "incomplete"
    db.execute(
        "INSERT INTO prop_case(show_id,status,missing,program_version,details,venue,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(show_id) DO UPDATE SET status=excluded.status,"
        "missing=excluded.missing,program_version=excluded.program_version,details=excluded.details,"
        "venue=excluded.venue,updated_at=excluded.updated_at",
        (show_id, status, json.dumps(missing), version, "legacy direct barcode membership",
         show["venue"], 0),
    )
    decide(db, "props", show_id, status, "legacy direct barcode membership", [data["source_id"]])
    return {"show": show_id, "status": status, "missing": missing}


def _commission(db, data):
    show_id = data["show_id"]
    _show(db, show_id)
    if not _signers_ok(data):
        raise ValueError("two commission signers required")
    roles = data.get("roles")
    if not isinstance(roles, list) or not roles:
        raise ValueError("commission must name the roles it covers")
    signed_at = int(data["signed_at"])
    existing = db.execute(
        "SELECT * FROM prop_commission WHERE show_id=? ORDER BY signed_at,rowid DESC LIMIT 1",
        (show_id,),
    ).fetchone()
    # Once any intake exists for the show, a later commission may only be a
    # faithful re-signature of the same scope.  A missing part must never be
    # papered over by widening the contract after the fact.
    if existing:
        if set(_json(existing["roles"], [])) != set(roles):
            raise ValueError("commission cannot be widened or changed after signing")
        if signed_at > int(existing["signed_at"]):
            raise ValueError("commission cannot be widened or re-dated after packing")
    pack = db.execute(
        "SELECT MIN(packed_at) AS packed_at FROM prop_pack WHERE show_id=?", (show_id,)
    ).fetchone()
    if pack and pack["packed_at"] is not None and signed_at > pack["packed_at"]:
        raise ValueError("commission must be signed before packing")
    db.execute(
        "INSERT INTO prop_commission(commission_id,show_id,scope,roles,signed_at,signer_a,signer_b,source_id) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(commission_id) DO UPDATE SET scope=excluded.scope,"
        "roles=excluded.roles,signed_at=excluded.signed_at,signer_a=excluded.signer_a,"
        "signer_b=excluded.signer_b,source_id=excluded.source_id",
        (data["commission_id"], show_id, data.get("scope", "custom"), _dumps(sorted(set(roles))),
         signed_at, data["signer_a"], data["signer_b"], data["source_id"]),
    )
    decide(db, "prop-commission", show_id, "signed", "commission scope fixed before packing",
           [data["source_id"], data["commission_id"]])
    return {"show": show_id, "commission": data["commission_id"], "roles": sorted(set(roles))}


def _pack(db, data):
    show_id = data["show_id"]
    show = _show(db, show_id)
    if not _signers_ok(data):
        raise ValueError("two packing signers required")
    packed_at = int(data["packed_at"])
    commission = db.execute(
        "SELECT * FROM prop_commission WHERE show_id=? ORDER BY signed_at DESC,rowid DESC LIMIT 1",
        (show_id,),
    ).fetchone()
    if not commission:
        raise ValueError("commission must be signed before packing")
    if int(commission["signed_at"]) > packed_at:
        raise ValueError("commission must be signed before packing")
    version, _signed, works = _program(db, show_id)
    case_id = data["case_id"]
    db.execute(
        "INSERT INTO prop_pack(show_id,case_id,seal,packed_at,signer_a,signer_b,source_id) VALUES(?,?,?,?,?,?,?)",
        (show_id, case_id, data.get("seal"), packed_at,
         data["signer_a"], data["signer_b"], data["source_id"]),
    )
    for item in data.get("requirements", []):
        if item["work_id"] not in works:
            raise ValueError("prop requirement not in program")
        db.execute(
            "INSERT INTO prop_requirement(show_id,work_id,role,serial,source_id,capabilities,commission_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (show_id, item["work_id"], item["role"], item["serial"], data["source_id"],
             _dumps(_caps(item.get("capabilities"))), commission["commission_id"]),
        )
    for unit in data.get("units", []):
        db.execute(
            "INSERT INTO prop_unit(show_id,serial,work_id,role,capabilities,case_id,source_id) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(show_id,serial) DO UPDATE SET work_id=excluded.work_id,"
            "role=excluded.role,capabilities=excluded.capabilities,case_id=excluded.case_id,"
            "source_id=excluded.source_id",
            (show_id, unit["serial"], unit["work_id"], unit.get("role"),
             _dumps(_caps(unit.get("capabilities"))), unit.get("case_id", case_id), data["source_id"]),
        )
        components = []
        for component in unit.get("components", []):
            components.append(component["serial"])
            db.execute(
                "INSERT INTO prop_component(show_id,prop_serial,serial,kind,capabilities,source_id) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(show_id,prop_serial,serial) DO UPDATE SET "
                "kind=excluded.kind,capabilities=excluded.capabilities,source_id=excluded.source_id",
                (show_id, unit["serial"], component["serial"], component.get("kind", "component"),
                 _dumps(_caps(component.get("capabilities"))), data["source_id"]),
            )
        db.execute(
            "INSERT INTO prop_pack_item(show_id,case_id,serial,prop_serial,work_id,role,capabilities,components,source_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(show_id,case_id,serial) DO UPDATE SET "
            "prop_serial=excluded.prop_serial,work_id=excluded.work_id,role=excluded.role,"
            "capabilities=excluded.capabilities,components=excluded.components,source_id=excluded.source_id",
            (show_id, case_id, unit["serial"], unit["serial"], unit["work_id"], unit.get("role"),
             _dumps(_caps(unit.get("capabilities"))), _dumps(components), data["source_id"]),
        )
    seen = set()
    for scan in data.get("scans", []):
        if scan["serial"] in seen:
            # One case barcode scanned twice never yields two physical sets.
            _record_conflict(db, show_id, scan["serial"], "scan",
                             "duplicate scan of one case barcode does not create a second unit",
                             data["source_id"])
            continue
        seen.add(scan["serial"])
        old = db.execute("SELECT * FROM prop_scan WHERE id=?", (scan["id"],)).fetchone()
        if old:
            if (old["serial"], old["case_id"]) != (scan["serial"], scan["case_id"]):
                _record_conflict(db, show_id, scan["serial"], "scan",
                                 "scan ID reused with different bytes", data["source_id"])
            continue
        db.execute(
            "INSERT INTO prop_scan(id,show_id,serial,case_id,source_id) VALUES(?,?,?,?,?)",
            (scan["id"], show_id, scan["serial"], scan["case_id"], data["source_id"]),
        )
    decide(db, "prop-pack", f"{show_id}:{case_id}", "packed",
           "signed packing records what was loaded; external scans are not receipt", [data["source_id"]])
    _recompute_related(db, show_id, data["source_id"])
    return {"show": show_id, "case": case_id, "status": "packed", "program_version": version}


def _unpack(db, data):
    show_id = data["show_id"]
    show = _show(db, show_id)
    if not _signers_ok(data):
        raise ValueError("two unpack signers required")
    case_id = data["case_id"]
    existing = db.execute("SELECT * FROM prop_unpack WHERE case_id=?", (case_id,)).fetchone()
    if existing:
        # Replaying a case barcode must not generate a second component set.
        db.execute(
            "INSERT INTO prop_unpack_conflict(show_id,case_id,reason,source_id) VALUES(?,?,?,?)",
            (show_id, case_id, "case barcode replayed; one case cannot yield two component sets",
             data["source_id"]),
        )
        decide(db, "prop-unpack", case_id, "replay_conflict",
               "duplicate case barcode does not create a second set of components", [data["source_id"]])
        return {"show": show_id, "case": case_id, "status": "replay_conflict"}
    pack = db.execute(
        "SELECT * FROM prop_pack WHERE show_id=? AND case_id=? ORDER BY id DESC LIMIT 1",
        (show_id, case_id),
    ).fetchone()
    seal_intact = bool(data.get("seal_intact"))
    if data.get("seal") is not None and pack and pack["seal"] is not None:
        if data["seal"] != pack["seal"]:
            seal_intact = False
    if data.get("venue") != show["venue"]:
        status = "venue_mismatch"
    elif not seal_intact:
        status = "seal_compromised"
    else:
        status = "sealed"
    db.execute(
        "INSERT INTO prop_unpack(id,show_id,case_id,venue,signer_a,signer_b,seal_intact,received_at,status,source_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (data["id"], show_id, case_id, data.get("venue"), data["signer_a"], data["signer_b"],
         int(seal_intact), int(data["received_at"]), status, data["source_id"]),
    )
    counted = set()
    for item in data.get("items", []):
        serial = item["serial"]
        if serial in counted:
            _record_conflict(db, show_id, serial, "unpack",
                             "serial counted twice in one unpack", data["source_id"])
            continue
        counted.add(serial)
        db.execute(
            "INSERT INTO prop_unpack_item(unpack_id,show_id,case_id,serial,prop_serial,kind,present,source_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (data["id"], show_id, case_id, serial, item.get("prop_serial", serial),
             item.get("kind", "unit"), int(bool(item.get("present", True))), data["source_id"]),
        )
    decide(db, "prop-unpack", data["id"], status,
           "double-signed remote receipt with seal and physical component count", [data["source_id"]])
    _recompute_related(db, show_id, data["source_id"])
    return {"show": show_id, "case": case_id, "status": status}


def _substitution(db, data):
    show_id = data["show_id"]
    _show(db, show_id)
    if not _signers_ok(data):
        raise ValueError("two substitution signers required")
    required = {"cert_id", "original_serial", "substitute_serial", "role", "capabilities",
                "valid_shows", "signed_at"}
    if not required.issubset(data):
        raise ValueError("substitution certificate is incomplete")
    if data["original_serial"] == data["substitute_serial"]:
        raise ValueError("substitution needs a different physical serial")
    if not data["capabilities"]:
        raise ValueError("substitution certificate must verify named capabilities")
    db.execute(
        "INSERT INTO prop_substitution(cert_id,show_id,original_serial,substitute_serial,role,capabilities,"
        "valid_shows,valid_from,valid_to,signer_a,signer_b,signed_at,source_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (data["cert_id"], show_id, data["original_serial"], data["substitute_serial"], data["role"],
         _dumps(_caps(data["capabilities"])), _dumps(data["valid_shows"]),
         data.get("valid_from"), data.get("valid_to"), data["signer_a"], data["signer_b"],
         int(data["signed_at"]), data["source_id"]),
    )
    decide(db, "prop-substitution", data["cert_id"], "signed",
           "certificate verifies only the listed performance capabilities", [data["source_id"]])
    _recompute_related(db, show_id, data["source_id"])
    return {"show": show_id, "certificate": data["cert_id"], "capabilities": _caps(data["capabilities"])}


def _return(db, data):
    show_id = data["show_id"]
    _show(db, show_id)
    if not _signers_ok(data):
        raise ValueError("two return signers required")
    db.execute(
        "INSERT INTO prop_return(id,show_id,returned_at,signer_a,signer_b,source_id) VALUES(?,?,?,?,?,?)",
        (data["id"], show_id, int(data["returned_at"]), data["signer_a"], data["signer_b"],
         data["source_id"]),
    )
    conflicts = []
    seen = set()
    for item in data.get("items", []):
        serial = item["serial"]
        if serial in seen:
            _record_conflict(db, show_id, serial, "return",
                             "serial counted twice in one return", data["source_id"])
            continue
        seen.add(serial)
        present = int(bool(item.get("present", True)))
        db.execute(
            "INSERT INTO prop_return_item(return_id,show_id,serial,present,source_id) VALUES(?,?,?,?,?)",
            (data["id"], show_id, serial, present, data["source_id"]),
        )
        unpacked = db.execute(
            "SELECT present FROM prop_unpack_item WHERE show_id=? AND serial=? AND present=1 LIMIT 1",
            (show_id, serial),
        ).fetchone()
        packed = db.execute(
            "SELECT 1 FROM prop_pack_item WHERE show_id=? AND "
            "(serial=? OR components LIKE ?) LIMIT 1",
            (show_id, serial, f'%"{serial}"%'),
        ).fetchone()
        if not packed:
            packed = db.execute(
                "SELECT 1 FROM prop_requirement WHERE show_id=? AND serial=? LIMIT 1",
                (show_id, serial),
            ).fetchone()
        if present and not unpacked:
            # A reverse receipt may not resurrect a unit the remote unpack
            # recorded as lost; the physical count wins over the packing sheet.
            conflict = f"{serial} claimed returned although remote unpack never received it"
            conflicts.append(conflict)
            _record_conflict(db, show_id, serial, "return", conflict, data["source_id"])
            db.execute(
                "INSERT INTO prop_loss(show_id,serial,status,first_seen,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(show_id,serial) DO UPDATE SET status=excluded.status,last_seen=excluded.last_seen",
                (show_id, serial, "lost", data["source_id"], data["source_id"]),
            )
        elif not present and packed and not unpacked:
            # The packing sheet says the part left, and the remote unpack never
            # confirmed it.  Record the loss explicitly; the old sheet cannot
            # stand in for a physical count.
            conflict = f"{serial} packed but absent from remote unpack"
            conflicts.append(conflict)
            _record_conflict(db, show_id, serial, "return", conflict, data["source_id"])
            db.execute(
                "INSERT INTO prop_loss(show_id,serial,status,first_seen,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(show_id,serial) DO UPDATE SET status=excluded.status,last_seen=excluded.last_seen",
                (show_id, serial, "lost", data["source_id"], data["source_id"]),
            )
        elif not present:
            db.execute(
                "INSERT INTO prop_loss(show_id,serial,status,first_seen,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(show_id,serial) DO UPDATE SET status=excluded.status,last_seen=excluded.last_seen",
                (show_id, serial, "lost", data["source_id"], data["source_id"]),
            )
        else:
            db.execute(
                "INSERT INTO prop_loss(show_id,serial,status,first_seen,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(show_id,serial) DO UPDATE SET status=excluded.status,last_seen=excluded.last_seen",
                (show_id, serial, "returned", data["source_id"], data["source_id"]),
            )
    decide(db, "prop-return", data["id"], "conflict" if conflicts else "counted",
           "reverse receipt is counted physically; an old packing sheet cannot override a remote loss",
           [data["source_id"]])
    return {"show": show_id, "return": data["id"],
            "status": "conflict" if conflicts else "counted", "conflicts": conflicts}


# ---------------------------------------------------------------------------
# Release evaluation


def _stock(db, show):
    """Physical units and components confirmed by a valid double-signed unpack."""
    units = {}
    components = {}
    unpacks = db.execute(
        "SELECT * FROM prop_unpack WHERE show_id=? AND status='sealed' AND venue=? "
        "ORDER BY received_at,id", (show["id"], show["venue"]),
    ).fetchall()
    for unpack in unpacks:
        for item in db.execute(
            "SELECT * FROM prop_unpack_item WHERE unpack_id=? AND present=1 ORDER BY rowid",
            (unpack["id"],),
        ).fetchall():
            bucket = units if item["kind"] == "unit" else components
            if item["serial"] in bucket:
                _record_conflict(db, show["id"], item["serial"], "stock",
                                 "same serial received in more than one case", item["source_id"])
                continue
            bucket[item["serial"]] = {"item": item, "unpack": unpack}
    return units, components


def _unit_components(db, show_id, serial):
    rows = db.execute(
        "SELECT * FROM prop_component WHERE show_id=? AND prop_serial=? ORDER BY serial",
        (show_id, serial),
    ).fetchall()
    return [dict(row) for row in rows]


def _priority(show):
    """Deterministic order for competing component demand.

    Earlier start wins; equal starts fall back to the lexicographic show id so
    the outcome never depends on insertion order.
    """
    return (int(show["starts"]), str(show["id"]))


def _foreign_claims(db, show):
    """Serialized components physically committed to a higher-priority show.

    Two overlapping props may both list the same motor in their packing
    sheets.  Per-item checks would each "find M", but only one set can be
    assembled.  The earlier show (tie-break: lower id) keeps the component and
    the later show must report the occupied serial and the missing part.
    """
    claims = {}
    for other in db.execute(
        "SELECT * FROM show_slot WHERE id<>? AND cancelled=0 AND status<>'cancelled'", (show["id"],)
    ).fetchall():
        if not (int(show["starts"]) < int(other["ends"]) and
                int(other["starts"]) < int(show["ends"])):
            continue
        if _priority(other) >= _priority(show):
            continue
        _units, components = _stock(db, other)
        for serial, info in components.items():
            claims.setdefault(serial, []).append(
                {"show": other["id"], "prop": info["item"]["prop_serial"],
                 "case": info["unpack"]["case_id"]}
            )
    return claims


def _commission_roles(db, show_id):
    row = db.execute(
        "SELECT * FROM prop_commission WHERE show_id=? ORDER BY signed_at DESC,rowid DESC LIMIT 1",
        (show_id,),
    ).fetchone()
    if not row:
        return None, None
    return row, set(_json(row["roles"], []))


def _certificate(db, show_id, requirement, show):
    """Return a signed certificate offered for this original prop.

    Matching is by the original serial and validity window, not by role name.
    Equivalent substitution is about verified performance capabilities; a
    certificate that names only ``sound`` does not thereby cover an
    ``appearance`` role, and the evaluator must report the uncovered capability
    rather than silently skipping the substitute.
    """
    rows = db.execute(
        "SELECT * FROM prop_substitution WHERE show_id=? AND original_serial=? "
        "ORDER BY signed_at,id",
        (show_id, requirement["serial"]),
    ).fetchall()
    best = None
    best_score = None
    required = set(_json(requirement["capabilities"], []))
    for row in rows:
        valid_shows = set(_json(row["valid_shows"], []))
        if "*" not in valid_shows and show_id not in valid_shows:
            continue
        if int(row["signed_at"]) > int(show["starts"]):
            continue
        if row["valid_from"] is not None and int(show["starts"]) < int(row["valid_from"]):
            continue
        if row["valid_to"] is not None and int(show["starts"]) > int(row["valid_to"]):
            continue
        caps = set(_json(row["capabilities"], []))
        score = (len(caps & required), len(caps))
        if best is None or score > best_score:
            best, best_score = row, score
    return best


def _evaluate_role(db, show, requirement, units, components, foreign, allocated):
    """Evaluate one role.

    ``allocated`` maps a component serial to the prop serial that already owns
    it in this release, so two props cannot both be assembled from one motor.
    """
    required_caps = _caps(_json(requirement["capabilities"], []))
    role = {
        "work_id": requirement["work_id"],
        "role": requirement["role"],
        "original_serial": requirement["serial"],
        "physical_serial": None,
        "certificate_id": None,
        "required_capabilities": required_caps,
        "covered_capabilities": [],
        "unmet_capabilities": [],
        "components_used": [],
        "conflicts": [],
        "path": [],
        "evidence": [requirement["source_id"]],
        "status": "missing",
    }

    if requirement["serial"] in units:
        physical = requirement["serial"]
        role["physical_serial"] = physical
        unit = units[physical]
        role["path"].append(f"unpack:{unit['unpack']['id']}:{physical}")
        unit_row = db.execute(
            "SELECT capabilities FROM prop_unit WHERE show_id=? AND serial=?",
            (show["id"], physical),
        ).fetchone()
        carried = set(_json(unit_row["capabilities"], []) if unit_row else [])
        missing_caps = [cap for cap in required_caps if cap not in carried]
        role["covered_capabilities"] = [cap for cap in required_caps if cap in carried]
        if missing_caps:
            role["unmet_capabilities"].extend(missing_caps)
            role["conflicts"].append(
                "physical unit does not carry: " + ", ".join(missing_caps))
        role["status"] = "ready"
    else:
        certificate = _certificate(db, show["id"], requirement, show)
        if not certificate:
            role["unmet_capabilities"] = list(required_caps) or ["physical_unit"]
            role["conflicts"].append(
                f"original {requirement['serial']} absent from signed remote unpack "
                "and no valid certificate")
            return role
        physical = certificate["substitute_serial"]
        role["certificate_id"] = certificate["cert_id"]
        covered = [cap for cap in required_caps
                   if cap in set(_json(certificate["capabilities"], []))]
        uncovered = [cap for cap in required_caps if cap not in covered]
        role["covered_capabilities"] = covered
        role["unmet_capabilities"] = list(uncovered)
        role["physical_serial"] = physical
        role["evidence"].append(certificate["source_id"])
        role["path"].append(
            f"certificate:{certificate['cert_id']}:{requirement['serial']}->{physical}")
        if physical not in units:
            role["conflicts"].append(
                f"certificate names {physical} but signed remote unpack did not receive it")
            return role
        role["path"].append(f"unpack:{units[physical]['unpack']['id']}:{physical}")
        role["evidence"].append(units[physical]["item"]["source_id"])
        role["status"] = "substituted" if not uncovered else "missing"
        if uncovered:
            role["conflicts"].append(
                "certificate does not cover: " + ", ".join(uncovered))

    physical = role["physical_serial"]
    # Components declared for the physical prop must each be counted in the
    # signed unpack and cannot be committed to two overlapping props.
    for component in _unit_components(db, show["id"], physical):
        serial = component["serial"]
        if serial in foreign:
            owners = ", ".join(f"{c['show']}/{c['prop']}" for c in foreign[serial])
            role["conflicts"].append(
                f"component {serial} is physically claimed by overlapping show(s) {owners}")
            role["unmet_capabilities"].append(f"component:{serial}")
            continue
        if serial in allocated and allocated[serial] != physical:
            role["conflicts"].append(
                f"component {serial} already allocated to prop {allocated[serial]}; "
                f"{physical} is still missing it")
            role["unmet_capabilities"].append(f"component:{serial}")
            continue
        component_item = components.get(serial)
        if not component_item:
            role["conflicts"].append(f"component {serial} not counted in signed unpack")
            role["unmet_capabilities"].append(f"component:{serial}")
            continue
        recorded_under = component_item["item"]["prop_serial"]
        if recorded_under not in (None, physical):
            role["conflicts"].append(
                f"component {serial} was recorded under prop {recorded_under}")
            role["unmet_capabilities"].append(f"component:{serial}")
            continue
        allocated[serial] = physical
        role["components_used"].append(serial)
        role["path"].append(f"component:{component_item['unpack']['case_id']}:{serial}")
        role["evidence"].append(component_item["item"]["source_id"])

    if role["conflicts"] or role["unmet_capabilities"]:
        role["status"] = "missing"
    return role


def _recompute(db, show_id, source_id):
    """Rebuild the append-only release view for a show from current evidence."""
    show = db.execute("SELECT * FROM show_slot WHERE id=?", (show_id,)).fetchone()
    if not show:
        return None
    has_evidence = db.execute(
        "SELECT 1 FROM prop_pack WHERE show_id=? UNION SELECT 1 FROM prop_unpack WHERE show_id=? LIMIT 1",
        (show_id, show_id),
    ).fetchone()
    if not has_evidence:
        return None
    version, _signed, works = _program(db, show_id)
    commission, contract_roles = _commission_roles(db, show_id)
    units, components = _stock(db, show)
    foreign = _foreign_claims(db, show)
    role_rows = []
    conflicts = []
    if show["cancelled"]:
        status = "cancelled"
        reason = "show cancelled; prior prop release cannot be used"
    elif not works:
        status, reason = "hold", "no effective signed program"
    elif not commission:
        status, reason = "hold", "no signed commission"
    else:
        status = "ready"
        reason = "signed unpack, seal and physical component count cover commissioned roles"
        allocated = {}
        covered_works = set()
        packs_exist = db.execute(
            "SELECT 1 FROM prop_pack WHERE show_id=? LIMIT 1", (show_id,)
        ).fetchone()
        valid_unpack = db.execute(
            "SELECT 1 FROM prop_unpack WHERE show_id=? AND status='sealed' AND venue=? LIMIT 1",
            (show_id, show["venue"]),
        ).fetchone()
        if packs_exist and not valid_unpack:
            status = "hold"
            conflicts.append(
                f"no valid signed unpack at venue {show['venue']}; transit scans are not receipt")
        raw_requirements = db.execute(
            "SELECT * FROM prop_requirement WHERE show_id=? ORDER BY id", (show_id,)
        ).fetchall()
        # A packing sheet or an earlier legacy intake may have recorded the
        # same requirement twice; evaluate the latest record once per role.
        latest = {}
        for row in raw_requirements:
            latest[(row["work_id"], row["role"], row["serial"])] = row
        requirement_rows = list(latest.values())
        for requirement in requirement_rows:
            if requirement["work_id"] not in works:
                continue
            # The signed commission fixes the contract.  A role outside it is
            # not evaluated and cannot influence release; a role inside it is
            # always evaluated, even if it looks optional.
            if requirement["role"] not in contract_roles:
                continue
            role = _evaluate_role(db, show, requirement, units, components, foreign, allocated)
            role["in_contract"] = True
            covered_works.add(requirement["work_id"])
            role["status_reason"] = "; ".join(role["conflicts"]) or (
                "physical count matches" if role["status"] in ("ready", "substituted")
                else "unmet requirement")
            role_rows.append(role)
            if role["status"] not in ("ready", "substituted"):
                status = "hold"
                conflicts.append(
                    f"{requirement['role']}/{requirement['serial']}: {role['status_reason']}")
        # Report roles the program needs but the commission does not cover, so
        # the contract width itself is auditable rather than silently ignored.
        uncovered = [f"{row['role']}/{row['serial']}" for row in requirement_rows
                     if row["work_id"] in works and row["role"] not in contract_roles]
        if uncovered:
            reason = reason + "; roles outside signed commission: " + ", ".join(sorted(set(uncovered)))
        for work in sorted(works - covered_works):
            if any(row["work_id"] == work for row in requirement_rows):
                continue
            status = "hold"
            conflicts.append(f"program work {work} has no prop requirement intake")
        if not role_rows and not conflicts:
            reason = "commissioned roles need no props"
        if show["status"] != "scheduled":
            status = "hold"
            conflicts.append(f"show slot is {show['status']}")
    details = {"reason": reason, "conflicts": conflicts, "roles": role_rows}
    encoded = _dumps(details)
    old = db.execute(
        "SELECT * FROM prop_release WHERE show_id=? ORDER BY id DESC LIMIT 1", (show_id,)
    ).fetchone()
    decide(db, "prop-release", show_id, status, reason, [source_id, show_id, version])
    if old and (old["status"], old["program_version"], old["reason"], old["details"]) == (
            status, version, reason, encoded):
        return {"show": show_id, "status": status, "reason": reason, "roles": role_rows,
                "program_version": version}
    cur = db.execute(
        "INSERT INTO prop_release(show_id,program_version,status,reason,details,source_id,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (show_id, version, status, reason, encoded, source_id, int(show["actual_start"] or 0)),
    )
    release_id = cur.lastrowid
    for role in role_rows:
        db.execute(
            "INSERT INTO prop_release_role(release_id,show_id,work_id,role,status,original_serial,"
            "physical_serial,path,certificate_id,required_capabilities,covered_capabilities,"
            "unmet_capabilities,components_used,conflicts,evidence) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (release_id, show_id, role["work_id"], role["role"], role["status"],
             role["original_serial"], role["physical_serial"], _dumps(role["path"]),
             role["certificate_id"], _dumps(role["required_capabilities"]),
             _dumps(role["covered_capabilities"]), _dumps(role["unmet_capabilities"]),
             _dumps(role["components_used"]), _dumps(role["conflicts"]),
             _dumps(role["evidence"])),
        )
    return {"show": show_id, "status": status, "reason": reason, "roles": role_rows,
            "program_version": version}


def release(db, show_id):
    """Latest operative prop release, or None."""
    row = db.execute(
        "SELECT * FROM prop_release WHERE show_id=? ORDER BY id DESC LIMIT 1", (show_id,)
    ).fetchone()
    return dict(row) if row else None


def _recompute_related(db, show_id, source_id):
    """Recompute this show and any overlapping show whose stock could change.

    A component counted for one show can displace a competing commitment at
    another show performing at the same time, so new evidence at either end
    must refresh both release views.  Ordering is deterministic (start time,
    then show id), which makes the later show the one that reports the loss.
    """
    target = db.execute("SELECT * FROM show_slot WHERE id=?", (show_id,)).fetchone()
    if not target:
        return None
    # Later starts are evaluated last so they see the higher-priority claim on
    # a shared component within this same refresh, regardless of input order.
    related = [target] + [row for row in db.execute(
        "SELECT * FROM show_slot WHERE id<>? AND cancelled=0", (show_id,)
    ).fetchall() if int(target["starts"]) < int(row["ends"]) and
        int(row["starts"]) < int(target["ends"])]
    related.sort(key=lambda row: (int(row["starts"]), str(row["id"])))
    result = None
    for row in related:
        outcome = _recompute(db, row["id"], source_id)
        if row["id"] == show_id:
            result = outcome
    return result


def revalidate_show(db, show_id, cause):
    """Invalidate a prior release after a show or program change.

    Called by ``show.py`` when a show is cancelled, moves venue or adopts a new
    signed program.  Earlier release rows and the decision chain are preserved;
    a new decision is appended so an old ready status cannot be used against
    the new facts.  Props never alter cue or royalty decisions.
    """
    show = db.execute("SELECT * FROM show_slot WHERE id=?", (show_id,)).fetchone()
    if not show:
        return None
    has_evidence = db.execute(
        "SELECT 1 FROM prop_pack WHERE show_id=? UNION SELECT 1 FROM prop_unpack WHERE show_id=? LIMIT 1",
        (show_id, show_id),
    ).fetchone()
    if not has_evidence:
        return None
    decide(db, "prop-release-change", show_id, "invalidated", cause, [show_id])
    if cause == "venue_changed":
        db.execute(
            "UPDATE prop_unpack SET status='venue_mismatch' WHERE show_id=? AND venue<>?",
            (show_id, show["venue"]),
        )
    # Recompute this show and every overlapping show: a cancellation frees the
    # parts it no longer performs with, and a newly signed program changes which
    # roles need covering.  The earlier release rows and decisions stay.
    result = _recompute_related(db, show_id, cause)
    # If a commission happens to need no props, the recompute could still say
    # ready.  A cancellation or venue move must nevertheless retire the earlier
    # outbound release so that status is never reused.
    if cause in ("show_cancelled", "venue_changed") and result and result["status"] == "ready":
        reason = "prior release invalidated by " + cause
        details = _dumps({"reason": reason, "conflicts": [reason], "roles": result["roles"]})
        db.execute(
            "INSERT INTO prop_release(show_id,program_version,status,reason,details,source_id,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (show_id, result.get("program_version"), "hold", reason, details, cause, 0),
        )
        decide(db, "prop-release", show_id, "hold", reason, [show_id, cause])
        return {"show": show_id, "status": "hold", "reason": reason,
                "roles": result["roles"], "program_version": result.get("program_version")}
    return result
