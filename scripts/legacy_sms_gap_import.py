#!/usr/bin/env python3
"""Recover distinct legacy iPhone SMS text into an isolated review source.

The verified copied databases are read-only. Similar Apple rows remain held,
while materially different text is retained under a source-specific identity.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3

from scripts.legacy_mms_import import LOCAL_ZONE, verify_sources
from scripts.nexus_sms_import import blocked_addresses, normalize_address


LOG = logging.getLogger("legacy-sms-gap-import")
SOURCE = "legacy_sms_review"
PHONE = re.compile(r"\+[1-9][0-9]{6,14}")


def canonical_address(value: str) -> str:
    """Normalize a phone address without inventing an unknown country code."""
    normalized = normalize_address(value or "")
    digits = re.sub(r"\D", "", normalized)
    if normalized.startswith("+") and 7 <= len(digits) <= 15 and digits[0] != "0":
        return "+" + digits
    return normalized


@dataclass(frozen=True)
class LegacySMS:
    """One deduplicated source text and its verified group-member evidence."""

    date: int
    address: str
    flags: int
    body: str
    members: frozenset[str]

    @property
    def identity(self) -> str:
        """Return a stable identity independent of snapshot row IDs."""
        fields = [self.date, self.address, self.flags, self.body]
        raw = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
        return hashlib.sha256(b"legacy-sms-review\0" + raw).hexdigest()

    @property
    def wall_time(self) -> tuple[str, int]:
        """Use the legacy exporter's verified local wall-time convention."""
        local = datetime.fromtimestamp(self.date, LOCAL_ZONE)
        return (local.strftime("%Y-%m-%d %H:%M:%S"),
                int(local.replace(tzinfo=timezone.utc).timestamp()))


def load_records(source_root: Path) -> list[LegacySMS]:
    """Read flags 2/3 from all copied snapshots and collapse repeat observations."""
    verify_sources(source_root)
    records: dict[tuple[int, str, int, str], set[frozenset[str]]] = defaultdict(set)
    paths = sorted(source_root.glob("*/sms.db"))
    if len(paths) != 6:
        raise ValueError("expected six verified legacy SMS databases")
    for path in paths:
        with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("legacy source database failed integrity check")
            members: dict[int, set[str]] = defaultdict(set)
            for group_id, address in db.execute("SELECT group_id,address FROM group_member"):
                members[group_id].add(canonical_address(address or ""))
            for address, date, flags, body, group_id in db.execute(
                "SELECT address,date,flags,coalesce(text,''),group_id "
                "FROM message WHERE flags IN (2,3)"
            ):
                if not isinstance(body, str):
                    raise ValueError("legacy SMS has a non-text body")
                key = (int(date), canonical_address(address or ""), flags, body)
                records[key].add(frozenset(members[group_id]))
    return [LegacySMS(*key, next(iter(member_sets)) if len(member_sets) == 1
                      else frozenset()) for key, member_sets in sorted(records.items())]


def classify(records: list[LegacySMS], db: sqlite3.Connection,
             config_path: Path) -> tuple[list[LegacySMS], Counter[str]]:
    """Select recoverable texts and account for overlaps and owner exclusions."""
    archive: dict[tuple[str, bool], list[tuple[int, str]]] = defaultdict(list)
    optout: set[str] = set()
    for ts, sender, body, handle in db.execute(
        "SELECT m.ts_unix,m.sender,m.body,c.handle FROM messages m "
        "JOIN conversations c ON c.id=m.conversation_id "
        "WHERE m.source!=? AND c.is_group=0", (SOURCE,)
    ):
        address = canonical_address(handle or sender or "")
        archive[(address, sender == "Me")].append((ts, body or ""))
        if sender == "Me" and (body or "").strip().casefold() in ("stop", "quit"):
            optout.add(address)
    optout.update(record.address for record in records if record.flags == 3
                  and record.body.strip().casefold() in ("stop", "quit"))
    blocked = {canonical_address(address) for address in blocked_addresses(config_path)}
    selected: list[LegacySMS] = []
    reasons: Counter[str] = Counter()
    for record in records:
        address = record.address
        if not record.body.strip():
            reason = "blank_text"
        elif re.fullmatch(r"[0-9]{5,6}", address):
            reason = "short_code"
        elif address in blocked:
            reason = "owner_blocked"
        elif address in optout:
            reason = "owner_optout_thread"
        elif record.flags == 2 and "appointment reminder" in record.body.casefold():
            reason = "appointment_reminder"
        elif not PHONE.fullmatch(address):
            reason = "unverified_address"
        elif record.members != frozenset((address,)):
            reason = "unverified_group"
        else:
            _, ts = record.wall_time
            close = [body for archived_ts, body in archive[(address, record.flags == 3)]
                     if abs(archived_ts - ts) <= 60]
            if record.body in close:
                reason = "archive_exact"
            elif any(difflib.SequenceMatcher(None, record.body, body).ratio() >= 0.9
                     for body in close):
                reason = "possible_text_variant"
            else:
                reason = "review_addition"
                selected.append(record)
        reasons[reason] += 1
    if sum(reasons.values()) != len(records):
        raise ValueError("legacy SMS accounting is incomplete")
    return selected, reasons


def append_isolated(db: sqlite3.Connection, selected: list[LegacySMS]) -> int:
    """Append review texts without altering existing archive rows."""
    added = 0
    with db:
        for record in selected:
            name = "Review legacy SMS: " + record.address
            db.execute(
                "INSERT INTO conversations(source,name,handle,is_group) VALUES(?,?,?,0) "
                "ON CONFLICT(source,name) DO NOTHING", (SOURCE, name, record.address)
            )
            conversation = db.execute(
                "SELECT id,handle,is_group FROM conversations WHERE source=? AND name=?",
                (SOURCE, name)
            ).fetchone()
            if conversation is None or conversation[1:] != (record.address, 0):
                raise ValueError("legacy SMS review conversation conflict")
            display, timestamp = record.wall_time
            sender = "Me" if record.flags == 3 else record.address
            expected = (conversation[0], SOURCE, display, timestamp, sender, record.body, None)
            existing = db.execute(
                "SELECT conversation_id,source,ts,ts_unix,sender,body,source_guid "
                "FROM messages WHERE hash=?", (record.identity,)
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,"
                    "sender,body,is_system,seq) VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                    (record.identity, conversation[0], SOURCE, display, timestamp,
                     sender, record.body)
                )
                added += 1
            elif existing != expected:
                raise ValueError("legacy SMS review identity conflict")
    return added


def main() -> None:
    """Audit or append source-specific records to a disposable archive copy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.apply and "reconstruction" not in args.db.resolve().parts:
        raise ValueError("legacy SMS import writes require an isolated reconstruction database")
    records = load_records(args.source_root)
    mode = "rw" if args.apply else "ro"
    with closing(sqlite3.connect(f"file:{args.db.resolve()}?mode={mode}", uri=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        selected, reasons = classify(records, db, args.config)
        added = append_isolated(db, selected) if args.apply else 0
    LOG.info("legacy SMS gap audit: %s", json.dumps({
        "distinct_source_texts": len(records), "classification": dict(reasons),
        "selected": len(selected), "added": added,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
