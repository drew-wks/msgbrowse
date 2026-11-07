#!/usr/bin/env python3
"""Retain unresolved Nexus observations in a visibly separate review source.

Cross-platform metadata matches are candidate overlaps, not proven identities,
since Nexus records carry no Apple GUID. Hold these candidates from visible
message import while keeping their verified local source and review mapping.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3

from scripts.nexus_sms_import import (
    SMS, SHORT_CODE, archive_times, blocked_addresses, classify as classify_sms,
    load_sms, normalize_address, wall_time as sms_wall_time,
)
from scripts.nexus_mms_import import (
    MMS, MEDIA_EXT, classify as classify_mms, load_mms, staged_media,
    wall_time as mms_wall_time,
)


LOG = logging.getLogger("nexus-recovery-import")
SOURCE = "nexus_recovery"
OWNER_EXCLUSIONS = frozenset((
    "short_code", "owner_blocked", "owner_optout_thread", "appointment_reminder",
))


def apple_exact_index(db: sqlite3.Connection) -> set[tuple[int, bool, str, str]]:
    """Index possible Apple overlaps by handle, wall second, direction, and body."""
    return {
        (timestamp, sender == "Me", body, normalize_address(handle))
        for timestamp, sender, body, handle in db.execute(
            "SELECT m.ts_unix,m.sender,m.body,c.handle FROM messages m "
            "JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.source='imessage' AND m.is_system=0 AND c.is_group=0"
        )
    }


def excluded_sms(record: SMS, blocked: set[str], optout: set[str]) -> bool:
    """Apply owner exclusions independently of conservative hold priority."""
    address = normalize_address(record.address)
    return (address in blocked or bool(SHORT_CODE.fullmatch(record.address))
            or address in optout or (record.kind == 1 and
                                      "appointment reminder" in record.body.casefold()))


def excluded_mms(record: MMS, owners: set[str], blocked: set[str],
                 optout: set[str]) -> bool:
    """Apply the same owner exclusions to a recovered MMS thread."""
    others = tuple(address for address in record.participants if address not in owners)
    return (any(address.isdigit() and 4 <= len(address) <= 6 for address in others)
            or any(address in blocked or address in optout for address in others)
            or (record.box == 1 and "appointment reminder" in record.body.casefold()))


def select_recovery(source_root: Path, config_path: Path, ledger_path: Path,
                    db: sqlite3.Connection) -> tuple[list[tuple[SMS, str]], list[tuple[MMS, str]], Counter[str]]:
    """Account for every source identity without multiplying overlap candidates.

    Args:
        source_root: Copied, immutable Nexus XML directory.
        config_path: Owner-directed import exclusions.
        ledger_path: Private list of Nexus matches to excluded Apple GUIDs.
        db: Current archive or an isolated copy of it.

    Returns:
        Selected SMS, selected MMS, and aggregate disposition counts.
    """
    sms, _ = load_sms(source_root)
    blocked = blocked_addresses(config_path)
    times = archive_times(db)
    _, _, held_sms = classify_sms(sms, blocked, times)
    mms, _, owners = load_mms(source_root)
    optout = {
        normalize_address(record.address) for record in sms
        if record.kind == 2 and record.body.strip().casefold() in ("stop", "quit")
    }
    _, _, held_mms = classify_mms(mms, owners, blocked, optout, times)
    ledger = json.loads(ledger_path.read_text())
    if ledger.get("schema_version") != 1 or len(ledger.get("identities", [])) != 4:
        raise ValueError("Nexus exclusion-ledger match list is incomplete")
    excluded_identities = set(ledger["identities"])
    exact = apple_exact_index(db)
    selected_sms: list[tuple[SMS, str]] = []
    selected_mms: list[tuple[MMS, str]] = []
    reasons: Counter[str] = Counter()
    nexus_present = {identity for (identity,) in db.execute(
        "SELECT hash FROM messages WHERE source='nexus'"
    )}
    for record in sms:
        reason = held_sms.get(record.identity)
        if reason is None:
            if record.identity not in nexus_present:
                raise ValueError("previously accepted Nexus SMS is absent from archive")
            reasons["already_in_nexus"] += 1
        elif excluded_sms(record, blocked, optout) or reason in OWNER_EXCLUSIONS:
            reasons["owner_excluded"] += 1
        elif record.identity in excluded_identities:
            reasons["apple_exclusion_ledger"] += 1
        elif (sms_wall_time(record)[1], record.kind == 2, record.body,
              normalize_address(record.address)) in exact:
            reasons["possible_apple_overlap_held"] += 1
        else:
            selected_sms.append((record, reason))
            reasons["recovery_sms"] += 1
    for record in mms:
        reason = held_mms.get(record.identity)
        if reason is None:
            if record.identity not in nexus_present:
                raise ValueError("previously accepted Nexus MMS is absent from archive")
            reasons["already_in_nexus"] += 1
        elif excluded_mms(record, owners, blocked, optout) or reason in OWNER_EXCLUSIONS:
            reasons["owner_excluded"] += 1
        else:
            selected_mms.append((record, reason))
            reasons["recovery_mms"] += 1
    if sum(reasons.values()) != len(sms) + len(mms):
        raise ValueError("Nexus source identities were not fully accounted for")
    return selected_sms, selected_mms, reasons


def conversation_name(addresses: tuple[str, ...]) -> tuple[str, str, bool]:
    """Create a review label without asserting an uncertain contact match."""
    distinct = tuple(sorted(x for x in set(addresses) if x))
    if not distinct:
        return "Review: unknown sender", "", False
    if len(distinct) == 1:
        return "Review: " + distinct[0], distinct[0], False
    return "Review group: " + ", ".join(distinct), "", True


def append_isolated(db: sqlite3.Connection, selected_sms: list[tuple[SMS, str]],
                    selected_mms: list[tuple[MMS, str]], owners: set[str],
                    staging: Path, data_dir: Path) -> tuple[int, int, int]:
    """Append review messages and verified media to a disposable archive."""
    media = staged_media(staging)
    media_root = data_dir / "media" / "nexus"
    media_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    messages_added = attachments_added = files_copied = 0
    with db:
        for record, reason in selected_sms:
            address = normalize_address(record.address)
            name, handle, group = conversation_name((address,))
            db.execute(
                "INSERT INTO conversations(source,name,handle,is_group) VALUES(?,?,?,?) "
                "ON CONFLICT(source,name) DO NOTHING", (SOURCE, name, handle, int(group)),
            )
            conv = db.execute("SELECT id FROM conversations WHERE source=? AND name=?",
                              (SOURCE, name)).fetchone()
            if conv is None:
                raise ValueError("Nexus recovery conversation missing")
            display, timestamp = sms_wall_time(record)
            sender = "Me" if record.kind == 2 else (address or "Unknown sender")
            existing = db.execute(
                "SELECT id,conversation_id,source,ts,ts_unix,sender,body,source_guid "
                "FROM messages WHERE hash=?", (record.identity,),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,sender,body,is_system,seq) "
                    "VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                    (record.identity, conv[0], SOURCE, display, timestamp, sender, record.body),
                )
                messages_added += 1
            elif existing[1:] != (conv[0], SOURCE, display, timestamp, sender, record.body, None):
                raise ValueError("Nexus recovery SMS identity conflict")
        for record, reason in selected_mms:
            others = tuple(x for x in record.participants if x not in owners)
            name, handle, group = conversation_name(others)
            db.execute(
                "INSERT INTO conversations(source,name,handle,is_group) VALUES(?,?,?,?) "
                "ON CONFLICT(source,name) DO NOTHING", (SOURCE, name, handle, int(group)),
            )
            conv = db.execute("SELECT id FROM conversations WHERE source=? AND name=?",
                              (SOURCE, name)).fetchone()
            if conv is None:
                raise ValueError("Nexus recovery MMS conversation missing")
            display, timestamp = mms_wall_time(record)
            sender = "Me" if record.box == 2 else record.sender
            existing = db.execute(
                "SELECT id,conversation_id,source,ts,ts_unix,sender,body,source_guid "
                "FROM messages WHERE hash=?", (record.identity,),
            ).fetchone()
            if existing is None:
                cursor = db.execute(
                    "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,sender,body,is_system,seq) "
                    "VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                    (record.identity, conv[0], SOURCE, display, timestamp, sender, record.body),
                )
                if cursor.lastrowid is None:
                    raise ValueError("Nexus recovery MMS insert returned no ID")
                message_id = cursor.lastrowid
                messages_added += 1
            elif existing[1:] == (conv[0], SOURCE, display, timestamp, sender, record.body, None):
                message_id = existing[0]
            else:
                raise ValueError("Nexus recovery MMS identity conflict")
            for part in record.parts:
                if not part.digest:
                    continue
                source_path = media.get(part.digest)
                if source_path is None:
                    raise ValueError("held MMS media missing from verified staging")
                filename = part.digest + MEDIA_EXT[part.mime]
                target = media_root / filename
                if not target.exists():
                    shutil.copyfile(source_path, target)
                    target.chmod(0o600)
                    files_copied += 1
                if target.is_symlink() or target.stat().st_size != source_path.stat().st_size:
                    raise ValueError("retained Nexus recovery media size mismatch")
                with target.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != part.digest:
                        raise ValueError("retained Nexus recovery media checksum mismatch")
                kind = "image" if part.mime.startswith("image/") else (
                    "video" if part.mime.startswith("video/") else "file")
                rel = "permanent/" + filename
                cursor = db.execute(
                    "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                    "SELECT ?,?,?,?,?,? WHERE NOT EXISTS "
                    "(SELECT 1 FROM attachments WHERE message_id=? AND rel_path=? AND kind=?)",
                    (message_id, kind, rel, part.original_name or filename, conv[0], timestamp,
                     message_id, rel, kind),
                )
                attachments_added += cursor.rowcount
    return messages_added, attachments_added, files_copied


def main() -> None:
    """Audit recovery dispositions or append them to an isolated copy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--ledger-matches", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.apply and "reconstruction" not in args.db.resolve().parts:
        raise ValueError("recovery writes require an isolated reconstruction database")
    mode = "rw" if args.apply else "ro"
    with closing(sqlite3.connect(f"file:{args.db.resolve()}?mode={mode}", uri=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        selected_sms, selected_mms, reasons = select_recovery(
            args.source_root, args.config, args.ledger_matches, db)
        _, _, owners = load_mms(args.source_root)
        added = append_isolated(db, selected_sms, selected_mms, owners, args.staging,
                               args.db.parent) if args.apply else (0, 0, 0)
    LOG.info("Nexus recovery audit: %s", json.dumps({
        "disposition": dict(reasons), "selected_sms": len(selected_sms),
        "selected_mms": len(selected_mms), "messages_added": added[0],
        "attachments_added": added[1], "media_files_copied": added[2],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
