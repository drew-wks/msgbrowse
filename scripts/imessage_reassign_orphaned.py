#!/usr/bin/env python3
"""Reassign archived orphaned iMessages using Apple chat metadata only.

The source database is opened read-only. No message text or attachment bytes are
read. Dry-run is the default; --apply changes only archive conversation metadata.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import sqlite3
from typing import Sequence


LOG = logging.getLogger("imessage-reassign-orphaned")


@dataclass(frozen=True)
class ArchiveMessage:
    """Archive identity needed for a metadata-only move."""

    id: int
    conversation_id: int
    hash: str


@dataclass(frozen=True)
class Move:
    """One verified Apple GUID and its chosen archive destination."""

    message_id: int
    guid: str
    hash: str
    target_name: str
    category: str
    is_group: bool


@dataclass(frozen=True)
class Audit:
    """Counts and planned moves; names and message contents stay out of logs."""

    moves: tuple[Move, ...]
    skipped: Counter[str]
    orphan_before: int
    new_names: frozenset[str]


def open_database(path: Path, *, read_only: bool) -> sqlite3.Connection:
    """Open a SQLite database with foreign keys and optional read-only mode."""

    uri = path.resolve().as_uri() + ("?mode=ro" if read_only else "")
    db = sqlite3.connect(uri, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if read_only:
        db.execute("PRAGMA query_only=ON")
    return db


def audit(source: sqlite3.Connection, archive: sqlite3.Connection) -> Audit:
    """Build a fail-closed GUID-to-chat plan without reading message bodies."""

    orphan = archive.execute(
        "SELECT id FROM conversations WHERE source='imessage' AND name='orphaned'"
    ).fetchone()
    if orphan is None:
        raise ValueError("archive has no iMessage orphaned conversation")
    orphan_id = int(orphan["id"])
    orphan_before = int(
        archive.execute(
            "SELECT count(*) FROM messages WHERE conversation_id=?", (orphan_id,)
        ).fetchone()[0]
    )
    named = {
        int(row["id"]): str(row["name"])
        for row in archive.execute(
            "SELECT id,name FROM conversations WHERE source='imessage'"
        )
    }
    by_name = {name: cid for cid, name in named.items()}
    archived = {
        str(row["source_guid"]): ArchiveMessage(
            int(row["id"]), int(row["conversation_id"]), str(row["hash"])
        )
        for row in archive.execute(
            "SELECT id,conversation_id,hash,source_guid FROM messages "
            "WHERE source='imessage' AND source_guid IS NOT NULL"
        )
    }

    # A GUID must identify one source message and one Apple chat. The source
    # sender/handle label is deliberately ignored: group membership is unknown
    # when only a sender name is available.
    source_rows: dict[str, set[int]] = defaultdict(set)
    source_chats: dict[str, set[int]] = defaultdict(set)
    for row in source.execute(
        "SELECT m.guid,m.ROWID AS source_id,j.chat_id "
        "FROM message m LEFT JOIN chat_message_join j ON j.message_id=m.ROWID "
        "WHERE m.guid IS NOT NULL"
    ):
        guid = str(row["guid"])
        if guid not in archived:
            continue
        source_rows[guid].add(int(row["source_id"]))
        if row["chat_id"] is not None:
            source_chats[guid].add(int(row["chat_id"]))

    chat_meta = {
        int(row["chat_id"]): (
            str(row["chat_identifier"] or ""),
            int(row["participants"]) > 1 or bool(row["room_name"]),
        )
        for row in source.execute(
            "SELECT c.ROWID AS chat_id,c.chat_identifier,c.room_name,"
            "count(ch.handle_id) AS participants FROM chat c "
            "LEFT JOIN chat_handle_join ch ON ch.chat_id=c.ROWID "
            "GROUP BY c.ROWID"
        )
    }
    identifiers: dict[str, set[int]] = defaultdict(set)
    for chat_id, (identifier, _) in chat_meta.items():
        identifiers[identifier].add(chat_id)

    chat_orphans: dict[int, list[tuple[str, ArchiveMessage]]] = defaultdict(list)
    chat_named: dict[int, set[int]] = defaultdict(set)
    skipped: Counter[str] = Counter()
    for guid, record in archived.items():
        ids = source_chats.get(guid, set())
        if len(source_rows.get(guid, set())) != 1 or len(ids) != 1:
            if record.conversation_id == orphan_id:
                skipped["missing_or_ambiguous_source_chat"] += 1
            continue
        chat_id = next(iter(ids))
        if record.conversation_id == orphan_id:
            chat_orphans[chat_id].append((guid, record))
        else:
            chat_named[chat_id].add(record.conversation_id)

    moves: list[Move] = []
    new_names: set[str] = set()
    for chat_id, rows in chat_orphans.items():
        meta = chat_meta.get(chat_id)
        if meta is None:
            skipped["missing_chat_metadata"] += len(rows)
            continue
        identifier, is_group = meta
        candidates = chat_named[chat_id]
        exact_id = by_name.get(identifier)
        if len(candidates) == 1:
            chosen_id = next(iter(candidates))
            if exact_id is not None and exact_id != chosen_id:
                skipped["conflicting_archive_evidence"] += len(rows)
                continue
            target_name = named[chosen_id]
            category = "unique_archived_chat"
        elif candidates:
            skipped["conflicting_archive_evidence"] += len(rows)
            continue
        elif (
            not identifier
            or identifier == "orphaned"
            or len(identifiers[identifier]) != 1
            or any(ord(char) < 32 or char in "/\\" for char in identifier)
        ):
            skipped["unsafe_or_shared_chat_identifier"] += len(rows)
            continue
        elif exact_id is not None:
            target_name = identifier
            category = "exact_source_identifier"
        else:
            target_name = identifier
            new_names.add(identifier)
            category = "new_source_chat"
        if re.fullmatch(r"[0-9]{4,6}(?:\s*\([^)]*\))?", target_name):
            skipped["excluded_short_code_destination"] += len(rows)
            new_names.discard(target_name)
            continue
        for guid, record in rows:
            moves.append(
                Move(record.id, guid, record.hash, target_name, category, is_group)
            )

    return Audit(
        tuple(sorted(moves, key=lambda move: move.message_id)),
        skipped,
        orphan_before,
        frozenset(new_names),
    )


def audit_sources(source_paths: Sequence[Path], archive: sqlite3.Connection) -> Audit:
    """Union verified source plans, rejecting conflicting destinations."""

    if not source_paths:
        raise ValueError("at least one source database is required")
    plans: list[Audit] = []
    for path in source_paths:
        with closing(open_database(path, read_only=True)) as source:
            source.execute("BEGIN")
            plans.append(audit(source, archive))
            source.rollback()
    if len(plans) == 1:
        return plans[0]

    candidates: dict[str, Move] = {}
    conflicts: set[str] = set()
    for plan in plans:
        for move in plan.moves:
            previous = candidates.get(move.guid)
            if previous is not None and (
                previous.message_id != move.message_id
                or previous.target_name != move.target_name
                or previous.is_group != move.is_group
            ):
                conflicts.add(move.guid)
            else:
                candidates[move.guid] = move
    conflicts.update(
        conflicting_source_evidence(source_paths, archive, candidates)
    )
    moves = tuple(
        sorted(
            (move for guid, move in candidates.items() if guid not in conflicts),
            key=lambda move: move.message_id,
        )
    )
    skipped = Counter({"conflicting_source_destinations": len(conflicts)})
    skipped["no_verified_chat_link"] = plans[0].orphan_before - len(candidates)
    new_names = frozenset(
        move.target_name
        for move in moves
        if any(move.target_name in plan.new_names for plan in plans)
    )
    return Audit(moves, skipped, plans[0].orphan_before, new_names)


def conflicting_source_evidence(
    source_paths: Sequence[Path],
    archive: sqlite3.Connection,
    candidates: dict[str, Move],
) -> set[str]:
    """Reject a proposed move if any snapshot links it to another archive chat."""

    if not candidates:
        return set()
    orphan_id = int(
        archive.execute(
            "SELECT id FROM conversations WHERE source='imessage' AND name='orphaned'"
        ).fetchone()[0]
    )
    named = {
        str(row["source_guid"]): str(row["name"])
        for row in archive.execute(
            "SELECT m.source_guid,c.name FROM messages m "
            "JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.source='imessage' AND m.source_guid IS NOT NULL "
            "AND m.conversation_id<>?",
            (orphan_id,),
        )
    }
    conflicts: set[str] = set()
    for path in source_paths:
        with closing(open_database(path, read_only=True)) as source:
            source_rows: dict[str, set[int]] = defaultdict(set)
            source_chats: dict[str, set[int]] = defaultdict(set)
            chat_named: dict[int, set[str]] = defaultdict(set)
            for row in source.execute(
                "SELECT m.guid,m.ROWID AS source_id,j.chat_id "
                "FROM message m LEFT JOIN chat_message_join j "
                "ON j.message_id=m.ROWID WHERE m.guid IS NOT NULL"
            ):
                guid = str(row["guid"])
                chat_id = row["chat_id"]
                if guid in candidates:
                    source_rows[guid].add(int(row["source_id"]))
                    if chat_id is not None:
                        source_chats[guid].add(int(chat_id))
                if chat_id is not None and guid in named:
                    chat_named[int(chat_id)].add(named[guid])
            for guid, rows in source_rows.items():
                if len(rows) != 1:
                    conflicts.add(guid)
            for guid, chats in source_chats.items():
                if len(source_rows[guid]) != 1 or len(chats) != 1:
                    conflicts.add(guid)
                    continue
                destinations = chat_named[next(iter(chats))]
                if destinations and destinations != {candidates[guid].target_name}:
                    conflicts.add(guid)
    return conflicts


def check_related_rows(archive: sqlite3.Connection) -> None:
    """Reject stale denormalized conversation IDs before and after the move."""

    for table in ("attachments", "links"):
        mismatch = archive.execute(
            f"SELECT count(*) FROM {table} child JOIN messages m "
            "ON m.id=child.message_id "
            "WHERE child.conversation_id<>m.conversation_id"
        ).fetchone()[0]
        if mismatch:
            raise ValueError(f"{table} has {mismatch} mismatched conversation IDs")
    mismatch = archive.execute(
        "SELECT count(*) FROM reactions r JOIN messages m ON m.hash=r.message_hash "
        "WHERE r.source='imessage' AND r.conversation_id<>m.conversation_id"
    ).fetchone()[0]
    if mismatch:
        raise ValueError(f"reactions has {mismatch} mismatched conversation IDs")


def apply(archive: sqlite3.Connection, plan: Audit) -> int:
    """Move only planned GUIDs in one checked transaction, preserving row IDs."""

    if not plan.moves:
        return 0
    orphan_id = int(
        archive.execute(
            "SELECT id FROM conversations WHERE source='imessage' AND name='orphaned'"
        ).fetchone()[0]
    )
    before_total = int(archive.execute("SELECT count(*) FROM messages").fetchone()[0])
    before_media = int(archive.execute("SELECT count(*) FROM attachments").fetchone()[0])
    before_links = int(archive.execute("SELECT count(*) FROM links").fetchone()[0])
    before_reactions = int(archive.execute("SELECT count(*) FROM reactions").fetchone()[0])
    check_related_rows(archive)

    targets: dict[str, int] = {}
    for move in plan.moves:
        if move.target_name in targets:
            continue
        if move.target_name in plan.new_names:
            handle = move.target_name if move.target_name.startswith("+") or "@" in move.target_name else ""
            archive.execute(
                "INSERT INTO conversations(source,name,is_group,handle) "
                "VALUES('imessage',?,?,?)",
                (move.target_name, int(move.is_group), handle),
            )
        target = archive.execute(
            "SELECT id FROM conversations WHERE source='imessage' AND name=?",
            (move.target_name,),
        ).fetchone()
        if target is None or int(target[0]) == orphan_id:
            raise ValueError("planned destination is unavailable")
        targets[move.target_name] = int(target[0])

    for move in plan.moves:
        target_id = targets[move.target_name]
        changed = archive.execute(
            "UPDATE messages SET conversation_id=? "
            "WHERE id=? AND source='imessage' AND source_guid=? "
            "AND hash=? AND conversation_id=?",
            (target_id, move.message_id, move.guid, move.hash, orphan_id),
        ).rowcount
        if changed != 1:
            raise ValueError("planned message changed before reassignment")
        archive.execute(
            "UPDATE attachments SET conversation_id=? WHERE message_id=?",
            (target_id, move.message_id),
        )
        archive.execute(
            "UPDATE links SET conversation_id=? WHERE message_id=?",
            (target_id, move.message_id),
        )
        archive.execute(
            "UPDATE reactions SET conversation_id=? "
            "WHERE source='imessage' AND message_hash=?",
            (target_id, move.hash),
        )

    after_orphan = int(
        archive.execute(
            "SELECT count(*) FROM messages WHERE conversation_id=?", (orphan_id,)
        ).fetchone()[0]
    )
    if after_orphan != plan.orphan_before - len(plan.moves):
        raise ValueError("orphaned message count did not decrease as planned")
    for table, expected in (
        ("messages", before_total),
        ("attachments", before_media),
        ("links", before_links),
        ("reactions", before_reactions),
    ):
        actual = int(archive.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        if actual != expected:
            raise ValueError(f"{table} row count changed")
    check_related_rows(archive)
    if archive.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("foreign key check failed")
    if archive.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity check failed")
    return len(plan.moves)


def run(
    source_path: Path | Sequence[Path],
    archive_path: Path,
    *,
    apply_changes: bool,
    expected_moves: int | None = None,
) -> Audit:
    """Audit one source snapshot and optionally commit checked archive moves."""

    sources = [source_path] if isinstance(source_path, Path) else list(source_path)
    with closing(open_database(archive_path, read_only=not apply_changes)) as archive:
        archive.execute("BEGIN IMMEDIATE" if apply_changes else "BEGIN")
        try:
            plan = audit_sources(sources, archive)
            if expected_moves is not None and len(plan.moves) != expected_moves:
                raise ValueError(
                    f"audit found {len(plan.moves)} moves, expected {expected_moves}"
                )
            if apply_changes:
                count = apply(archive, plan)
                archive.commit()
                LOG.info("committed %d metadata-only message moves", count)
            else:
                archive.rollback()
        except Exception:
            archive.rollback()
            raise
    return plan


def main() -> None:
    """Parse CLI options and report aggregate results without personal data."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True, action="append")
    parser.add_argument("--archive-db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="commit checked archive moves")
    parser.add_argument(
        "--expect-moves", type=int, help="abort if the fresh audit differs from this count"
    )
    args = parser.parse_args()
    if args.apply and args.expect_moves is None:
        parser.error("--apply requires --expect-moves from a reviewed dry-run")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    plan = run(
        args.source_db,
        args.archive_db,
        apply_changes=args.apply,
        expected_moves=args.expect_moves,
    )
    categories = Counter(move.category for move in plan.moves)
    LOG.info(
        "orphan_before=%d move=%d new_chats=%d categories=%s skipped=%s",
        plan.orphan_before,
        len(plan.moves),
        len(plan.new_names),
        dict(sorted(categories.items())),
        dict(sorted(plan.skipped.items())),
    )


if __name__ == "__main__":
    main()
