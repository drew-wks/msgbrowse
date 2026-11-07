#!/usr/bin/env python3
"""Deduplicate verified Apple row pairs while retaining GUID tombstones."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3


LOG = logging.getLogger("dedupe-apple-pairs")
FINGERPRINT_TABLES = (
    "conversations", "messages", "attachments", "links", "reactions",
    "imessage_source_assets", "imessage_attachment_observations",
    "imessage_excluded_messages", "contact_identifiers", "contacts",
)
HASH_DEPENDENCIES = (
    ("embeddings", "message_hash"), ("message_sentiment", "message_hash"),
    ("spam_events", "message_hash"), ("spam_findings", "message_hash"),
    ("contact_facts", "source_message_hash"),
    ("fact_state", "last_message_hash"),
    ("sentiment_state", "last_message_hash"),
    ("spam_state", "last_message_hash"),
)


def load_pairs(path: Path) -> list[tuple[int, int]]:
    """Read the private cross-platform candidate report without message bodies."""
    report = json.loads(path.read_text())
    if report.get("schema_version") != 1:
        raise ValueError("candidate report version differs")
    pairs = [tuple(sorted(item["apple_message_ids"])) for item in report["candidates"]
             if len(item["apple_message_ids"]) == 2]
    if len(pairs) != 1190 or len({value for pair in pairs for value in pair}) != 2380:
        raise ValueError("expected 1,190 disjoint Apple pairs")
    return sorted(pairs)


def fingerprint(db: sqlite3.Connection) -> str:
    """Hash rows whose changes would make a saved dedupe plan stale."""
    digest = hashlib.sha256()
    for table in FINGERPRINT_TABLES:
        for row in db.execute(f"SELECT * FROM {table} ORDER BY id"):
            digest.update(json.dumps((table, row), ensure_ascii=False,
                                     separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def audit_pairs(db: sqlite3.Connection, pairs: list[tuple[int, int]],
                media_root: Path) -> dict[str, int]:
    """Reject ambiguous pairs or child data that deletion could discard."""
    stats: dict[str, int] = {"pairs": len(pairs), "attachment_references": 0,
                             "source_assets_to_move": 0, "observations_to_move": 0,
                             "media_files_verified": 0}
    hashes: set[str] = set()
    verified_media: set[str] = set()
    for keep, remove in pairs:
        first = db.execute(
            "SELECT hash,conversation_id,source,source_guid,ts,ts_unix,sender,body,is_system,seq "
            "FROM messages WHERE id=?", (keep,)).fetchone()
        second = db.execute(
            "SELECT hash,conversation_id,source,source_guid,ts,ts_unix,sender,body,is_system,seq "
            "FROM messages WHERE id=?", (remove,)).fetchone()
        if (first is None or second is None or first[1] != second[1] or
                first[2] != "imessage" or second[2] != "imessage" or
                not first[3] or not second[3] or first[3] == second[3] or
                first[4:] != second[4:] or first[8] != 0):
            raise ValueError("Apple pair is not identical on retained message metadata")
        group = db.execute("SELECT is_group FROM conversations WHERE id=?",
                           (first[1],)).fetchone()
        if group != (0,):
            raise ValueError("Apple pair is not a one-to-one chat")
        if db.execute(
            "SELECT 1 FROM imessage_excluded_messages WHERE source_guid=? OR message_hash=?",
            (second[3], second[0]),
        ).fetchone():
            raise ValueError("Apple duplicate is already in the exclusion ledger")
        hashes.add(second[0])
        attachments = []
        for message_id in (keep, remove):
            values = db.execute(
                "SELECT kind,rel_path FROM attachments WHERE message_id=? ORDER BY kind,rel_path",
                (message_id,)).fetchall()
            attachments.append(values)
            if db.execute("SELECT 1 FROM links WHERE message_id=?", (message_id,)).fetchone():
                raise ValueError("Apple pair has a link child")
        if attachments[0] != attachments[1]:
            raise ValueError("Apple pair attachment bytes or kinds differ")
        stats["attachment_references"] += len(attachments[1])
        for _, rel in attachments[1]:
            if rel in verified_media:
                continue
            if re.fullmatch(r"permanent/[0-9a-f]{2}/[0-9a-f]{64}\.[A-Za-z0-9]+", rel) is None:
                raise ValueError("Apple pair media path is not content addressed")
            path = media_root / rel.removeprefix("permanent/")
            if path.is_symlink() or not path.is_file():
                raise ValueError("Apple pair media is missing")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != path.stem:
                    raise ValueError("Apple pair media checksum differs")
            verified_media.add(rel)
        for table, key_columns, counter in (
            ("imessage_source_assets", "source_attachment_guid,rel_path", "source_assets_to_move"),
            ("imessage_attachment_observations", "source_attachment_guid,hidden,mime_type",
             "observations_to_move"),
        ):
            keep_keys = set(db.execute(
                f"SELECT {key_columns} FROM {table} WHERE message_id=?", (keep,)).fetchall())
            remove_keys = db.execute(
                f"SELECT {key_columns} FROM {table} WHERE message_id=?", (remove,)).fetchall()
            if keep_keys.intersection(remove_keys):
                raise ValueError("moving Apple attachment observations would conflict")
            stats[counter] += len(remove_keys)
    # No message-hash dependent data may be abandoned. The pair audit above
    # verifies every duplicate hash before the lookup.
    for table, column in HASH_DEPENDENCIES:
        for (value,) in db.execute(f"SELECT {column} FROM {table}"):
            if value in hashes:
                raise ValueError(f"Apple duplicate has dependent data in {table}")
    stats["media_files_verified"] = len(verified_media)
    return stats


def build_plan(db_path: Path, report_path: Path, media_root: Path) -> dict[str, object]:
    """Build a reproducible dedupe plan against the current archive."""
    db_path, report_path, media_root = (p.resolve() for p in
                                        (db_path, report_path, media_root))
    pairs = load_pairs(report_path)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
        stats = audit_pairs(db, pairs, media_root)
        baseline = fingerprint(db)
        messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    return {
        "db": str(db_path), "candidate_report": str(report_path),
        "media_root": str(media_root),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "baseline_sha256": baseline, "messages_before": messages,
        **stats,
    }


def dedupe_pairs(db: sqlite3.Connection, pairs: list[tuple[int, int]], now: str) -> int:
    """Retain alternate GUIDs and asset observations, then remove extra rows."""
    for keep, remove in pairs:
        canonical = db.execute("SELECT source_guid FROM messages WHERE id=?",
                               (keep,)).fetchone()[0]
        removed = db.execute("SELECT source_guid,hash FROM messages WHERE id=?",
                             (remove,)).fetchone()
        db.execute(
            "INSERT INTO imessage_excluded_messages(source_guid,message_hash,reason,excluded_at) "
            "VALUES(?,?,?,?)", (removed[0], removed[1],
                                 "deduplicated_to_guid:" + canonical, now))
        for table in ("imessage_source_assets", "imessage_attachment_observations"):
            db.execute(f"UPDATE {table} SET message_id=? WHERE message_id=?",
                       (keep, remove))
        if db.execute("DELETE FROM messages WHERE id=?", (remove,)).rowcount != 1:
            raise ValueError("Apple duplicate disappeared during dedupe")
    return len(pairs)


def apply_plan(plan: dict[str, object], checkpoint: Path) -> dict[str, int]:
    """Checkpoint SQLite, preserve GUIDs, and remove only validated pair rows."""
    db_path = Path(str(plan["db"])).resolve()
    checkpoint = checkpoint.resolve()
    if (checkpoint.exists() or checkpoint == db_path or
            str(checkpoint).startswith("/Volumes/KINGSTON/") or
            str(db_path).startswith("/Volumes/KINGSTON/")):
        raise ValueError("checkpoint must be new, local, and separate")
    current = build_plan(db_path, Path(str(plan["candidate_report"])),
                         Path(str(plan["media_root"])))
    if current != plan:
        raise ValueError("Apple dedupe plan changed")
    pairs = load_pairs(Path(str(plan["candidate_report"])))
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as original, closing(sqlite3.connect(checkpoint)) as saved:
        original.backup(saved)
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite checkpoint failed integrity check")
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            if fingerprint(db) != plan["baseline_sha256"]:
                raise ValueError("archive changed after checkpoint")
            audit_pairs(db, pairs, Path(str(plan["media_root"])))
            dedupe_pairs(db, pairs, datetime.now(timezone.utc).isoformat())
            if db.execute("SELECT count(*) FROM messages").fetchone()[0] != int(str(plan["messages_before"])) - len(pairs):
                raise ValueError("unexpected message count after dedupe")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("foreign key check failed")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return {"messages_removed": len(pairs), "guids_recorded": len(pairs),
            "source_assets_moved": int(str(plan["source_assets_to_move"])),
            "observations_moved": int(str(plan["observations_to_move"]))}


def main() -> None:
    """Write a private plan or apply one after a SQLite-only checkpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--candidate-report", required=True, type=Path)
    parser.add_argument("--media-root", required=True, type=Path)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--write-plan", type=Path)
    operation.add_argument("--apply-plan", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.write_plan:
        plan = build_plan(args.db, args.candidate_report, args.media_root)
        descriptor = os.open(args.write_plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, indent=2, sort_keys=True)
            stream.write("\n")
        LOG.info("Apple dedupe plan: %s", json.dumps({key: plan[key] for key in (
            "pairs", "attachment_references", "source_assets_to_move",
            "observations_to_move", "media_files_verified")}, sort_keys=True))
    else:
        if args.checkpoint is None:
            parser.error("--checkpoint is required for apply")
        plan = json.loads(args.apply_plan.read_text())
        if tuple(str(p.resolve()) for p in (args.db, args.candidate_report, args.media_root)) != (
                plan["db"], plan["candidate_report"], plan["media_root"]):
            raise ValueError("command paths differ from saved plan")
        LOG.info("Apple dedupe result: %s", json.dumps(apply_plan(plan, args.checkpoint), sort_keys=True))


if __name__ == "__main__":
    main()
