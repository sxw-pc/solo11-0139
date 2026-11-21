"""Enhanced prop evidence: remote unpack, components, substitution, contract scope.

The acceptance scenario is deliberately fixed before any evidence is written:

* the signed program needs original prop ``bell-7`` for ``work-1`` in two
  roles, a ``C4_tone`` sound role and a ``visible_clock`` appearance role;
* ``bell-7`` is missing from the signed remote unpack;
* ``bell-9`` is inside the case and a double-signed certificate proves only
  ``C4_tone`` for it.

A ``sound_only`` commission that covers only the sound role may release this
performance with ``bell-9``; a ``recorded_closeup`` commission that also needs
the appearance role may not, and must await ``bell-7`` or valid appearance
evidence.  Both commissions are signed before packing, so the contract cannot
be widened after the loss is discovered.
"""

import json
from pathlib import Path

import pytest

from rightscue import props, report, show
from rightscue.store import connect, read


ROOT = Path(__file__).resolve().parents[1] / "fixtures"
VENUE = "hall-A"
START = 1000
END = 5000
PACKED_AT = 1100
UNPACKED_AT = 2000
SHOW = {
    "id": "show-1",
    "venue": VENUE,
    "starts": START,
    "ends": END,
    "cancelled": False,
    "door_time": 1030,
    "stage_time": 1050,
    "program": {"version": 1, "signed_at": 900, "work_ids": ["work-1"]},
}
REQUIREMENTS = [
    {"work_id": "work-1", "role": "sound", "serial": "bell-7", "capabilities": ["C4_tone"]},
    {"work_id": "work-1", "role": "appearance", "serial": "bell-7", "capabilities": ["visible_clock"]},
]


def show_payload(source_id="show-v1", **overrides):
    show_data = json.loads(json.dumps(SHOW))
    show_data.update(overrides)
    return {"source_id": source_id, "show": show_data}


def commission(source_id, commission_id, roles, signed_at=PACKED_AT - 100):
    return {
        "source_id": source_id,
        "kind": "commission",
        "show_id": SHOW["id"],
        "commission_id": commission_id,
        "scope": "sound_only" if roles == ["sound"] else "recorded_closeup",
        "roles": roles,
        "signed_at": signed_at,
        "signer_a": "producer-A",
        "signer_b": "producer-B",
    }


def packing(source_id, case_id="case-1", units=None):
    units = units if units is not None else [
        {
            "serial": "bell-9",
            "work_id": "work-1",
            "role": "sound",
            "case_id": case_id,
            "capabilities": ["C4_tone"],
            "components": [
                {"serial": "motor-M", "kind": "drive", "capabilities": ["strike"]},
            ],
        }
    ]
    return {
        "source_id": source_id,
        "kind": "packing",
        "show_id": SHOW["id"],
        "case_id": case_id,
        "seal": "seal-1",
        "packed_at": PACKED_AT,
        "signer_a": "warehouse-A",
        "signer_b": "warehouse-B",
        "requirements": REQUIREMENTS,
        "units": units,
        "scans": [
            {"id": f"scan-{case_id}", "serial": case_id, "case_id": case_id},
        ],
    }


def unpacking(source_id, items, case_id="case-1", seal_intact=True, venue=VENUE,
              unpack_id="unpack-1", received_at=UNPACKED_AT):
    return {
        "source_id": source_id,
        "kind": "unpack",
        "id": unpack_id,
        "show_id": SHOW["id"],
        "case_id": case_id,
        "venue": venue,
        "signer_a": "receiving-A",
        "signer_b": "receiving-B",
        "seal_intact": seal_intact,
        "seal": "seal-1",
        "received_at": received_at,
        "items": items,
    }


def sound_certificate(source_id="cert-sound", capabilities=("C4_tone",), role="sound"):
    return {
        "source_id": source_id,
        "kind": "substitution",
        "show_id": SHOW["id"],
        "cert_id": "cert-bell-9",
        "original_serial": "bell-7",
        "substitute_serial": "bell-9",
        "role": role,
        "capabilities": list(capabilities),
        "valid_shows": [SHOW["id"]],
        "signed_at": 950,
        "signer_a": "technical-A",
        "signer_b": "technical-B",
    }


def unpack_bell_9_with_motor(source_id="unpack-v1"):
    return unpacking(source_id, [
        {"serial": "bell-9", "kind": "unit"},
        {"serial": "motor-M", "kind": "component", "prop_serial": "bell-9"},
    ])


def latest_release(db):
    return report.build(db)["prop_release_summary"][0]


def roles(view):
    return {role["role"]: role for role in view["roles"]}


def seeded(path, *roles_covered, units=None):
    db = connect(path)
    with db:
        show.run(db, show_payload())
        props.run(db, commission("commission-v1", "commission-1", list(roles_covered)))
        props.run(db, packing("pack-v1", units=units))
        props.run(db, unpack_bell_9_with_motor())
        props.run(db, sound_certificate())
    return db


# ---------------------------------------------------------------------------
# Acceptance scenario


def test_outer_case_scan_does_not_prove_components_arrived(tmp_path):
    """The case barcode was scanned in transit; its parts were not counted."""
    db = connect(tmp_path / "props.sqlite")
    with db:
        show.run(db, show_payload())
        props.run(db, commission("commission-v1", "commission-1", ["sound", "appearance"]))
        props.run(db, packing("pack-v1"))
        # An external case scan alone leaves the show on hold.
        view = latest_release(db)
        assert view["release_status"] == "hold"
        props.run(db, unpacking("unpack-v1", [
            {"serial": "bell-9", "kind": "unit"},
        ]))
        props.run(db, sound_certificate())
    view = latest_release(db)
    assert view["release_status"] == "hold"
    sound = roles(view)["sound"]
    assert sound["status"] == "missing"
    assert any("motor-M" in conflict for conflict in sound["conflicts"])


def test_sound_only_commission_releases_with_partial_certificate(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound")
    view = latest_release(db)
    assert view["release_status"] == "ready"
    sound = roles(view)["sound"]
    assert sound["status"] == "substituted"
    assert sound["physical_serial"] == "bell-9"
    assert sound["certificate_id"] == "cert-bell-9"
    assert sound["covered_capabilities"] == ["C4_tone"]
    assert sound["unmet_capabilities"] == []
    assert any(path.startswith("certificate:cert-bell-9:bell-7->bell-9")
               for path in sound["path"])
    assert "unpack-1" in json.dumps(sound["path"])


def test_recorded_closeup_commission_blocks_partial_certificate(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound", "appearance")
    view = latest_release(db)
    assert view["release_status"] == "hold"
    by_role = roles(view)
    assert by_role["sound"]["status"] == "substituted"
    appearance = by_role["appearance"]
    assert appearance["status"] == "missing"
    # The certificate proves sound only; appearance stays explicitly unmet.
    assert appearance["covered_capabilities"] == []
    assert appearance["unmet_capabilities"] == ["visible_clock"]
    assert any("does not cover: visible_clock" in c for c in appearance["conflicts"])
    assert any("visible_clock" in unmet for unmet in view["unmet"])


def test_commission_cannot_widen_after_missing_part_discovered(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound")
    late = commission("commission-late", "commission-1", ["sound", "appearance"])
    with pytest.raises(ValueError, match="cannot be widened"):
        with db:
            props.run(db, late)
    assert latest_release(db)["release_status"] == "ready"


def test_parallel_certificate_covers_appearance_explicitly(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound", "appearance")
    with db:
        props.run(db, sound_certificate(
            source_id="cert-appearance", capabilities=["visible_clock"], role="appearance"))
    view = latest_release(db)
    assert view["release_status"] == "ready"
    appearance = roles(view)["appearance"]
    assert appearance["status"] == "substituted"
    assert appearance["covered_capabilities"] == ["visible_clock"]


# ---------------------------------------------------------------------------
# Component exclusivity


def test_same_motor_cannot_be_committed_to_two_props(tmp_path):
    """Both props list motor-M; one physical motor can assemble only one."""
    db = connect(tmp_path / "props.sqlite")
    program = show_payload()
    program["show"]["program"]["work_ids"] = ["work-1", "work-2"]
    units = [
        {
            "serial": "bell-9", "work_id": "work-1", "role": "sound",
            "capabilities": ["C4_tone"],
            "components": [{"serial": "motor-M", "kind": "drive",
                            "capabilities": ["strike"]}],
        },
        {
            "serial": "bell-2", "work_id": "work-2", "role": "strike",
            "capabilities": ["C4_tone"],
            "components": [{"serial": "motor-M", "kind": "drive",
                            "capabilities": ["strike"]}],
        },
    ]
    requirements = [
        {"work_id": "work-1", "role": "sound", "serial": "bell-7",
         "capabilities": ["C4_tone"]},
        {"work_id": "work-2", "role": "strike", "serial": "bell-2",
         "capabilities": ["C4_tone"]},
    ]
    with db:
        show.run(db, program)
        props.run(db, commission("commission-v1", "commission-1", ["sound", "strike"]))
        pack = packing("pack-v1", units=units)
        pack["requirements"] = requirements
        props.run(db, pack)
        props.run(db, unpacking("unpack-v1", [
            {"serial": "bell-9", "kind": "unit"},
            {"serial": "bell-2", "kind": "unit"},
            {"serial": "motor-M", "kind": "component", "prop_serial": "bell-9"},
        ]))
        props.run(db, sound_certificate())
    view = latest_release(db)
    by_role = roles(view)
    # The earlier requirement keeps the single motor; the second prop is the
    # one still missing a part even though its packing sheet also names motor-M.
    assert by_role["sound"]["status"] == "substituted"
    assert by_role["sound"]["components_used"] == ["motor-M"]
    strike = by_role["strike"]
    assert strike["status"] == "missing"
    assert "motor-M" in json.dumps(strike["conflicts"])
    assert "motor-M" in json.dumps(strike["unmet_capabilities"])
    assert any("motor-M" in unmet and "bell-2" in unmet for unmet in view["unmet"])
    assert view["release_status"] == "hold"


def test_motor_claimed_by_overlapping_show_blocks_even_sound_only(tmp_path):
    """One motor cannot be promised to two shows that must play simultaneously."""
    db = connect(tmp_path / "props.sqlite")
    second = show_payload(source_id="show-2-v1")
    second["show"].update({
        "id": "show-2", "venue": "hall-B", "starts": 900, "ends": 4000,
        "program": {"version": 1, "signed_at": 900, "work_ids": ["work-2"]},
    })
    with db:
        show.run(db, show_payload())
        props.run(db, commission("commission-v1", "commission-1", ["sound"]))
        props.run(db, packing("pack-v1"))
        props.run(db, unpack_bell_9_with_motor())
        props.run(db, sound_certificate())

        show.run(db, second)
        props.run(db, {
            "source_id": "commission-2", "kind": "commission", "show_id": "show-2",
            "commission_id": "commission-2", "scope": "sound_only", "roles": ["strike"],
            "signed_at": PACKED_AT - 100,
            "signer_a": "producer-A", "signer_b": "producer-B",
        })
        other_pack = {
            "source_id": "pack-v2", "kind": "packing", "show_id": "show-2",
            "case_id": "case-2", "seal": "seal-2", "packed_at": PACKED_AT,
            "signer_a": "warehouse-A", "signer_b": "warehouse-B",
            "requirements": [{"work_id": "work-2", "role": "strike", "serial": "bell-2",
                              "capabilities": ["C4_tone"]}],
            "units": [{"serial": "bell-2", "work_id": "work-2", "role": "strike",
                       "capabilities": ["C4_tone"],
                       "components": [{"serial": "motor-M", "kind": "drive",
                                       "capabilities": ["strike"]}]}],
            "scans": [{"id": "scan-case-2", "serial": "case-2", "case_id": "case-2"}],
        }
        props.run(db, other_pack)
        props.run(db, {
            "source_id": "unpack-v2", "kind": "unpack", "id": "unpack-2",
            "show_id": "show-2", "case_id": "case-2", "venue": "hall-B",
            "signer_a": "receiving-A", "signer_b": "receiving-B",
            "seal_intact": True, "seal": "seal-2", "received_at": UNPACKED_AT,
            "items": [{"serial": "bell-2", "kind": "unit"},
                      {"serial": "motor-M", "kind": "component", "prop_serial": "bell-2"}],
        })
    summary = {row["show"]: row for row in report.build(db)["prop_release_summary"]}
    # show-2 starts first and physically counts motor-M; show-1 may not also
    # commit that serial to bell-9, even under the sound-only commission.
    assert summary["show-2"]["release_status"] == "ready"
    assert summary["show-1"]["release_status"] == "hold"
    sound = roles(summary["show-1"])["sound"]
    assert sound["status"] == "missing"
    assert "motor-M" in json.dumps(summary["show-1"]["unmet"])
    assert any("show-2" in conflict for conflict in sound["conflicts"])


def test_replayed_case_barcode_cannot_produce_second_component_set(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound")
    before = report.build(db)["prop_unpack_item"]
    with db:
        result = props.run(db, unpack_bell_9_with_motor("unpack-replay"))
    assert result["status"] == "replay_conflict"
    after = report.build(db)["prop_unpack_item"]
    assert len(after) == len(before)
    assert latest_release(db)["release_status"] == "ready"


# ---------------------------------------------------------------------------
# Show and program coupling


def test_new_program_revision_holds_prior_ready_release(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound")
    assert latest_release(db)["release_status"] == "ready"
    revised = show_payload(source_id="show-v2")
    revised["show"]["program"] = {"version": 2, "signed_at": 1200, "work_ids": ["work-1", "work-2"]}
    with db:
        show.run(db, revised)
    view = latest_release(db)
    assert view["release_status"] == "hold"
    assert any("work-2" in conflict for conflict in view["unmet"])
    # Prior release rows and decisions remain traceable.
    releases = report.build(db)["prop_release"]
    assert any(row["status"] == "ready" for row in releases)


def test_program_removal_and_venue_change_retire_release(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound")
    moved = show_payload(source_id="show-moved", venue="hall-Z")
    with db:
        show.run(db, moved)
    view = latest_release(db)
    assert view["release_status"] == "hold"
    assert any("venue" in reason for reason in view["unmet"])
    removed = show_payload(source_id="show-cut")
    removed["show"]["venue"] = "hall-Z"
    removed["show"]["program"] = {"version": 2, "signed_at": 1300, "work_ids": []}
    with db:
        show.run(db, removed)
    assert latest_release(db)["release_status"] == "hold"


def test_cancelled_show_cannot_use_prior_release(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound")
    cancelled = show_payload(source_id="show-cancelled", cancelled=True)
    with db:
        show.run(db, cancelled)
    view = latest_release(db)
    assert view["release_status"] == "cancelled"


def test_prop_decisions_do_not_touch_audio_or_royalty(tmp_path):
    from rightscue import cue, rights

    db = connect(tmp_path / "props.sqlite")
    with db:
        show.run(db, show_payload())
        props.run(db, commission("commission-v1", "commission-1", ["sound"]))
        props.run(db, packing("pack-v1"))
        props.run(db, unpack_bell_9_with_motor())
        props.run(db, sound_certificate())
        baseline = {
            "work_match": report.build(db)["work_match"],
            "royalty_case": report.build(db)["royalty_case"],
        }
        cue.run(db, read(ROOT / "cue-ok.json"))
        rights.run(db, read(ROOT / "rights-ok.json"))
    settled = {row["work_id"]: row["amount"] for row in report.build(db)["royalty_case"]
               if row["amount"]}
    assert settled  # the audio/royalty path still works after prop evidence
    assert baseline["work_match"] == []


# ---------------------------------------------------------------------------
# Returns and traceability


def test_return_cannot_override_remote_loss(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound")
    with db:
        result = props.run(db, {
            "source_id": "return-v1", "kind": "return", "id": "return-1",
            "show_id": SHOW["id"], "returned_at": 3000,
            "signer_a": "tour-A", "signer_b": "tour-B",
            "items": [{"serial": "bell-7", "present": True},
                      {"serial": "bell-7", "present": False},
                      {"serial": "bell-9", "present": True}],
        })
    assert result["status"] == "conflict"
    assert any("bell-7" in conflict for conflict in result["conflicts"])
    losses = {row["serial"]: row["status"] for row in report.build(db)["prop_loss"]}
    assert losses["bell-7"] == "lost"
    assert losses["bell-9"] == "returned"


def test_return_of_part_lost_in_transit_is_recorded(tmp_path):
    """The old packing sheet says bell-7 left; the remote unpack never saw it."""
    db = seeded(tmp_path / "props.sqlite", "sound")
    with db:
        result = props.run(db, {
            "source_id": "return-v2", "kind": "return", "id": "return-2",
            "show_id": SHOW["id"], "returned_at": 3100,
            "signer_a": "tour-A", "signer_b": "tour-B",
            "items": [{"serial": "bell-7", "present": False}],
        })
    assert result["status"] == "conflict"
    assert any("packed but absent from remote unpack" in c for c in result["conflicts"])
    losses = {row["serial"]: row["status"] for row in report.build(db)["prop_loss"]}
    assert losses["bell-7"] == "lost"


def test_same_source_replay_is_idempotent(tmp_path):
    db = connect(tmp_path / "props.sqlite")
    with db:
        show.run(db, show_payload())
        props.run(db, commission("commission-v1", "commission-1", ["sound"]))
        props.run(db, packing("pack-v1"))
        props.run(db, unpack_bell_9_with_motor())
        props.run(db, sound_certificate())
        before = report.build(db)["prop_release"]
        for payload in (packing("pack-v1"), unpack_bell_9_with_motor(),
                        sound_certificate()):
            assert props.run(db, payload)["status"] == "duplicate"
    assert report.build(db)["prop_release"] == before
    assert latest_release(db)["release_status"] == "ready"


def test_same_case_barcode_scanned_twice_creates_one_unit(tmp_path):
    db = connect(tmp_path / "props.sqlite")
    pack = packing("pack-v1")
    pack["scans"].append({"id": "scan-case-1-again", "serial": "case-1",
                          "case_id": "case-1"})
    with db:
        show.run(db, show_payload())
        props.run(db, commission("commission-v1", "commission-1", ["sound"]))
        props.run(db, pack)
        props.run(db, unpack_bell_9_with_motor())
        props.run(db, sound_certificate())
    built = report.build(db)
    assert len(built["prop_scan"]) == 1
    assert any(row["reason"].startswith("duplicate scan")
               for row in built["prop_conflict"])
    assert latest_release(db)["release_status"] == "ready"


def test_report_lists_paths_capabilities_and_conflict_sources(tmp_path):
    db = seeded(tmp_path / "props.sqlite", "sound", "appearance")
    view = latest_release(db)
    assert view["release_status"] == "hold"
    sound = roles(view)["sound"]
    assert sound["physical_serial"] == "bell-9"
    assert sound["certificate_id"] == "cert-bell-9"
    assert sound["covered_capabilities"] == ["C4_tone"]
    assert sound["components_used"] == ["motor-M"]
    appearance = roles(view)["appearance"]
    assert appearance["unmet_capabilities"] == ["visible_clock"]
    assert appearance["conflicts"]
    # Raw evidence remains separately readable.
    built = report.build(db)
    assert built["prop_pack"] and built["prop_unpack"] and built["prop_substitution"]
    assert built["prop_unpack_conflict"] == []
    assert any(system == "prop-release" for system in
               {row["system"] for row in built["decision"]})


def test_cancelling_one_show_frees_shared_motor_for_the_other(tmp_path):
    """Cancellation is a show fact that limits existing prop release purposes."""
    db = connect(tmp_path / "props.sqlite")
    second = show_payload(source_id="show-2-v1")
    second["show"].update({
        "id": "show-2", "venue": "hall-B", "starts": 900, "ends": 4000,
        "program": {"version": 1, "signed_at": 900, "work_ids": ["work-2"]},
    })
    with db:
        show.run(db, show_payload())
        props.run(db, commission("commission-v1", "commission-1", ["sound"]))
        props.run(db, packing("pack-v1"))
        props.run(db, unpack_bell_9_with_motor())
        props.run(db, sound_certificate())
        show.run(db, second)
        props.run(db, {
            "source_id": "commission-2", "kind": "commission", "show_id": "show-2",
            "commission_id": "commission-2", "scope": "sound_only", "roles": ["strike"],
            "signed_at": PACKED_AT - 100,
            "signer_a": "producer-A", "signer_b": "producer-B",
        })
        props.run(db, {
            "source_id": "pack-v2", "kind": "packing", "show_id": "show-2",
            "case_id": "case-2", "seal": "seal-2", "packed_at": PACKED_AT,
            "signer_a": "warehouse-A", "signer_b": "warehouse-B",
            "requirements": [{"work_id": "work-2", "role": "strike", "serial": "bell-2",
                              "capabilities": ["C4_tone"]}],
            "units": [{"serial": "bell-2", "work_id": "work-2", "role": "strike",
                       "capabilities": ["C4_tone"],
                       "components": [{"serial": "motor-M", "kind": "drive",
                                       "capabilities": ["strike"]}]}],
            "scans": [{"id": "scan-case-2", "serial": "case-2", "case_id": "case-2"}],
        })
        props.run(db, {
            "source_id": "unpack-v2", "kind": "unpack", "id": "unpack-2",
            "show_id": "show-2", "case_id": "case-2", "venue": "hall-B",
            "signer_a": "receiving-A", "signer_b": "receiving-B",
            "seal_intact": True, "seal": "seal-2", "received_at": UNPACKED_AT,
            "items": [{"serial": "bell-2", "kind": "unit"},
                      {"serial": "motor-M", "kind": "component", "prop_serial": "bell-2"}],
        })
        summary = {row["show"]: row for row in report.build(db)["prop_release_summary"]}
        assert summary["show-1"]["release_status"] == "hold"
        cancelled = show_payload(source_id="show-2-cancel")
        cancelled["show"].update({
            "id": "show-2", "venue": "hall-B", "starts": 900, "ends": 4000,
            "cancelled": True,
            "program": {"version": 1, "signed_at": 900, "work_ids": ["work-2"]},
        })
        show.run(db, cancelled)
    summary = {row["show"]: row for row in report.build(db)["prop_release_summary"]}
    assert summary["show-2"]["release_status"] == "cancelled"
    # With show-2 no longer playing, motor-M is no longer contested.
    assert summary["show-1"]["release_status"] == "ready"


def test_legacy_database_is_migrated_without_losing_rows(tmp_path):
    """A props database created by the barcode-only version still opens."""
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE prop_requirement(id INTEGER PRIMARY KEY,show_id TEXT,work_id TEXT,"
        "role TEXT,serial TEXT,source_id TEXT);"
        "CREATE TABLE prop_case(show_id TEXT PRIMARY KEY,status TEXT,missing TEXT);"
        "INSERT INTO prop_case VALUES('show-1','ready','[]');"
    )
    raw.commit()
    raw.close()
    db = connect(path)
    assert db.execute("SELECT status FROM prop_case WHERE show_id='show-1'").fetchone()[0] == "ready"
    columns = {row[1] for row in db.execute("PRAGMA table_info(prop_requirement)")}
    assert {"capabilities", "commission_id"} <= columns
    with db:
        legacy_show = show_payload()
        legacy_show["show"]["program"]["work_ids"] = ["work-1", "work-2"]
        show.run(db, legacy_show)
        props.run(db, read(ROOT / "props-ok.json"))
    built = report.build(db)
    assert built["prop_case"]
    # Legacy intake remains a barcode-membership record only; it must not be
    # promoted into the enhanced per-role release view.
    assert built["prop_release_summary"] == []
