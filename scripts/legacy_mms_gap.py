#!/usr/bin/env python3
"""Retain held legacy MMS media in review or augment matching Apple rows."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import difflib
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3

from scripts.legacy_mms_import import (
    EXTENSIONS, LOCAL_ZONE, Parent, classify, load_parents, verify_sources,
)
from scripts.nexus_sms_import import normalize_address


LOG = logging.getLogger("legacy-mms-gap")
SOURCE = "legacy_mms_review"


def archive_matches(parent: Parent, db: sqlite3.Connection) -> tuple[int, ...]:
    """Find Apple rows with the same second, handle, text, and retained image."""
    local = datetime.fromtimestamp(parent.date, LOCAL_ZONE)
    timestamp = int(local.replace(tzinfo=timezone.utc).timestamp())
    expected_hashes = {asset.digest for asset in parent.assets if asset.already_cataloged}
    matches: list[tuple[int, str, frozenset[str]]] = []
    for message_id, body, handle, sender in db.execute(
        "SELECT m.id,m.body,c.handle,m.sender FROM messages m "
        "JOIN conversations c ON c.id=m.conversation_id "
        "WHERE m.source='imessage' AND m.ts_unix=? AND c.is_group=0", (timestamp,)
    ):
        if normalize_address(handle) != parent.handle or (sender == "Me") != (parent.flags == 3):
            continue
        if not parent.body or difflib.SequenceMatcher(None, parent.body, body).ratio() < 0.9:
            continue
        hashes = frozenset(Path(rel).stem for (rel,) in db.execute(
            "SELECT rel_path FROM attachments WHERE message_id=?", (message_id,)
        ))
        if expected_hashes & hashes:
            matches.append((message_id, body, hashes))
    if len(matches) != 2 or len({body for _, body, _ in matches}) != 1 or len({hashes for _, _, hashes in matches}) != 1:
        raise ValueError("subject MMS does not have exactly two equivalent Apple rows")
    return tuple(sorted(message_id for message_id, _, _ in matches))


def select_gaps(source_root: Path, catalog: Path, db: sqlite3.Connection,
                config: Path) -> tuple[list[Parent], Parent, tuple[int, ...], Counter[str]]:
    """Select three media-only reviews and one byte augmentation."""
    verify_sources(source_root)
    parents, _ = load_parents(source_root, catalog)
    _, reasons, held = classify(parents, db, config)
    review = [parent for parent in parents if held.get(parent.identity) in
              ("unverified_contact", "unverified_direction")]
    subject = [parent for parent in parents if held.get(parent.identity) == "subject_representation"]
    if len(review) != 3 or len(subject) != 1 or any(parent.body or parent.subject for parent in review):
        raise ValueError("held legacy MMS set changed; review classification again")
    if any(len({asset.digest for asset in parent.assets if not asset.already_cataloged}) != 1
           for parent in review + subject):
        raise ValueError("held MMS media variants changed")
    return review, subject[0], archive_matches(subject[0], db), reasons


def content_path(media_root: Path, digest: str, mime: str) -> tuple[str, Path]:
    """Return a permanent content-addressed path inside the given media root."""
    rel = "permanent/" + digest[:2] + "/" + digest + EXTENSIONS[mime]
    return rel, media_root / rel.removeprefix("permanent/")


def retain_file(source: Path, target: Path, digest: str, size: int) -> bool:
    """Copy one immutable payload and verify its content hash."""
    if source.is_symlink() or not source.is_file() or source.stat().st_size != size:
        raise ValueError("verified staged MMS payload is unavailable")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied = not target.exists()
    if copied:
        shutil.copyfile(source, target)
        target.chmod(0o600)
    if target.is_symlink() or target.stat().st_size != size:
        raise ValueError("retained MMS payload size changed")
    with target.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("retained MMS payload hash changed")
    return copied


def append_isolated(db: sqlite3.Connection, review: list[Parent], subject: Parent,
                    apple_ids: tuple[int, ...], staging: Path,
                    isolated_media: Path) -> tuple[int, int, int]:
    """Append review messages and the extra Apple preview to a disposable DB."""
    added_messages = added_attachments = copied_files = 0
    with db:
        for parent in review:
            name = "Review legacy MMS: " + parent.handle
            db.execute(
                "INSERT INTO conversations(source,name,handle,is_group) VALUES(?,?,?,0) "
                "ON CONFLICT(source,name) DO NOTHING", (SOURCE, name, parent.handle)
            )
            conversation = db.execute(
                "SELECT id,handle,contact_id,is_group FROM conversations WHERE source=? AND name=?",
                (SOURCE, name)
            ).fetchone()
            if conversation is None or conversation[1:] != (parent.handle, None, 0):
                raise ValueError("legacy MMS review conversation conflict")
            local = datetime.fromtimestamp(parent.date, LOCAL_ZONE)
            display = local.strftime("%Y-%m-%d %H:%M:%S")
            timestamp = int(local.replace(tzinfo=timezone.utc).timestamp())
            sender = "Me" if parent.flags == 3 else "Direction unverified"
            expected = (conversation[0], SOURCE, display, timestamp, sender, parent.body, None)
            row = db.execute(
                "SELECT id,conversation_id,source,ts,ts_unix,sender,body,source_guid "
                "FROM messages WHERE hash=?", (parent.identity,)
            ).fetchone()
            if row is None:
                cursor = db.execute(
                    "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,"
                    "sender,body,is_system,seq) VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                    (parent.identity, conversation[0], SOURCE, display, timestamp,
                     sender, parent.body)
                )
                if cursor.lastrowid is None:
                    raise ValueError("legacy MMS review insert returned no ID")
                message_id = cursor.lastrowid
                added_messages += 1
            elif row[1:] == expected:
                message_id = row[0]
            else:
                raise ValueError("legacy MMS review identity conflict")
            by_digest = {asset.digest: asset for asset in parent.assets if not asset.already_cataloged}
            for asset in by_digest.values():
                rel, target = content_path(isolated_media, asset.digest, asset.mime)
                copied_files += int(retain_file(staging / asset.digest, target,
                                                asset.digest, asset.size))
                kind = "image" if asset.mime.startswith("image/") else (
                    "video" if asset.mime.startswith("video/") else "file")
                cursor = db.execute(
                    "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                    "SELECT ?,?,?,?,?,? WHERE NOT EXISTS "
                    "(SELECT 1 FROM attachments WHERE message_id=? AND rel_path=?)",
                    (message_id, kind, rel, asset.digest + EXTENSIONS[asset.mime],
                     conversation[0], timestamp, message_id, rel)
                )
                added_attachments += cursor.rowcount
        asset = next(asset for asset in subject.assets if not asset.already_cataloged)
        if asset.mime != "image/png" or asset.origin != "inline":
            raise ValueError("subject MMS preview representation changed")
        rel, target = content_path(isolated_media, asset.digest, asset.mime)
        copied_files += int(retain_file(staging / asset.digest, target, asset.digest, asset.size))
        for message_id in apple_ids:
            row = db.execute("SELECT conversation_id,ts_unix,source FROM messages WHERE id=?",
                             (message_id,)).fetchone()
            if row is None or row[2] != "imessage":
                raise ValueError("subject MMS Apple destination changed")
            cursor = db.execute(
                "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                "SELECT ?,'file',?,?,?,? WHERE NOT EXISTS "
                "(SELECT 1 FROM attachments WHERE message_id=? AND rel_path=?)",
                (message_id, rel, asset.digest + ".png", row[0], row[1], message_id, rel)
            )
            added_attachments += cursor.rowcount
    return added_messages, added_attachments, copied_files


def main() -> None:
    """Audit or stage held MMS in an isolated database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.apply and "reconstruction" not in args.db.resolve().parts:
        raise ValueError("held MMS writes require an isolated reconstruction database")
    mode = "rw" if args.apply else "ro"
    with closing(sqlite3.connect(f"file:{args.db.resolve()}?mode={mode}", uri=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        review, subject, apple_ids, reasons = select_gaps(
            args.source_root, args.catalog, db, args.config
        )
        added = append_isolated(db, review, subject, apple_ids, args.staging,
                               args.db.parent / "media" / "imessage") if args.apply else (0, 0, 0)
    LOG.info("held MMS gap audit: %s", json.dumps({
        "review_messages": len(review), "apple_rows_to_augment": len(apple_ids),
        "classification": dict(reasons), "added_messages": added[0],
        "added_attachment_references": added[1], "new_media_files": added[2],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
