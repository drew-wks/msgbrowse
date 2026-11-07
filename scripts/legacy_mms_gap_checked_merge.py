#!/usr/bin/env python3
"""Check and merge held legacy MMS review rows and one Apple preview variant."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3

from scripts.legacy_mms_checked_merge import copy_verified_media, open_readonly
from scripts.legacy_mms_gap import SOURCE, archive_matches, content_path, select_gaps
from scripts.legacy_mms_import import Parent, verify_sources


LOG = logging.getLogger("legacy-mms-gap-checked-merge")


def digest_rows(db: sqlite3.Connection, review: bool,
                apple_ids: tuple[int, ...], preview_rel: str) -> str:
    """Hash review rows or the untouched archive baseline."""
    digest = hashlib.sha256()
    operator = "=" if review else "!="
    queries = [
        ("conversations", f"SELECT * FROM conversations WHERE source{operator}? ORDER BY id"),
        ("messages", f"SELECT * FROM messages WHERE source{operator}? ORDER BY id"),
        ("attachments", "SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id "
         f"WHERE m.source{operator}? ORDER BY a.id"),
    ]
    if not review:
        queries.extend([
            ("contacts", "SELECT * FROM contacts ORDER BY id"),
            ("identifiers", "SELECT * FROM contact_identifiers ORDER BY id"),
            ("exclusions", "SELECT * FROM imessage_excluded_messages ORDER BY id"),
        ])
    for label, query in queries:
        params = () if label in ("contacts", "identifiers", "exclusions") else (SOURCE,)
        for row in db.execute(query, params):
            if not review and label == "attachments" and row[1] in apple_ids and row[3] == preview_rel:
                continue
            digest.update(json.dumps([label, row], ensure_ascii=False,
                                     separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def expected_media(review: list[Parent], subject: Parent) -> list[tuple[str, int]]:
    """List the four uncataloged content-addressed files in the source set."""
    entries: dict[str, int] = {}
    for parent in review + [subject]:
        for asset in parent.assets:
            if asset.already_cataloged:
                continue
            rel, _ = content_path(Path("."), asset.digest, asset.mime)
            if rel in entries and entries[rel] != asset.size:
                raise ValueError("legacy MMS payload size conflict")
            entries[rel] = asset.size
    if len(entries) != 4:
        raise ValueError("held MMS source payload count changed")
    return sorted(entries.items())


def verify_isolated_media(isolated_path: Path, entries: list[tuple[str, int]]) -> None:
    """Check all staged bytes before planning or merging."""
    root = isolated_path.parent / "media" / "imessage"
    for rel, size in entries:
        path = root / rel.removeprefix("permanent/")
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            raise ValueError("isolated held MMS file missing or changed")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != Path(rel).stem:
                raise ValueError("isolated held MMS file hash changed")


def build_plan(source_root: Path, catalog: Path, isolated_path: Path,
               target_path: Path, config: Path) -> dict[str, object]:
    """Compare selected source, staged rows, Apple augmentation, and target."""
    source_root, catalog = source_root.resolve(), catalog.resolve()
    isolated_path, target_path = isolated_path.resolve(), target_path.resolve()
    config = config.resolve()
    if isolated_path == target_path:
        raise ValueError("isolated and target databases must differ")
    source_digest = verify_sources(source_root)
    with closing(open_readonly(isolated_path)) as isolated, closing(open_readonly(target_path)) as target:
        review, subject, apple_ids, reasons = select_gaps(source_root, catalog, target, config)
        entries = expected_media(review, subject)
        verify_isolated_media(isolated_path, entries)
        preview = next(asset for asset in subject.assets if not asset.already_cataloged)
        preview_rel, _ = content_path(Path("."), preview.digest, preview.mime)
        baseline = digest_rows(target, False, apple_ids, preview_rel)
        if digest_rows(isolated, False, apple_ids, preview_rel) != baseline:
            raise ValueError("isolated archive baseline differs from target")
        accepted = {parent.identity for parent in review}
        isolated_ids = {identity for (identity,) in isolated.execute(
            "SELECT hash FROM messages WHERE source=?", (SOURCE,)
        )}
        if accepted != isolated_ids:
            raise ValueError("isolated held MMS rows differ from source")
        existing = {identity for (identity,) in target.execute(
            "SELECT hash FROM messages WHERE source=?", (SOURCE,)
        )}
        if not existing.issubset(accepted):
            raise ValueError("target has unplanned held MMS rows")
        for message_id in apple_ids:
            expected = ("file", preview_rel, preview.digest + ".png")
            row = isolated.execute(
                "SELECT kind,rel_path,original_name FROM attachments "
                "WHERE message_id=? AND rel_path=?", (message_id, preview_rel)
            ).fetchone()
            if row != expected:
                raise ValueError("isolated Apple preview augmentation differs from source")
            existing_row = target.execute(
                "SELECT kind,rel_path,original_name FROM attachments "
                "WHERE message_id=? AND rel_path=?", (message_id, preview_rel)
            ).fetchone()
            if existing_row not in (None, expected):
                raise ValueError("target Apple preview conflicts")
        augmented = sum(target.execute(
            "SELECT count(*) FROM attachments WHERE message_id=? AND rel_path=?",
            (message_id, preview_rel)
        ).fetchone()[0] for message_id in apple_ids)
        review_digest = digest_rows(isolated, True, apple_ids, preview_rel)
    return {
        "source_root": str(source_root), "catalog": str(catalog),
        "isolated_db": str(isolated_path), "target_db": str(target_path),
        "config": str(config), "source_sha256": source_digest,
        "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "baseline_sha256": baseline, "review_rows_sha256": review_digest,
        "accepted_sha256": hashlib.sha256("\n".join(sorted(accepted)).encode()).hexdigest(),
        "accepted_messages": len(accepted), "already_present": len(existing),
        "messages_to_add": len(accepted - existing),
        "apple_ids": list(apple_ids), "preview_rel": preview_rel,
        "apple_references_to_add": len(apple_ids) - augmented,
        "media_files": len(entries), "media_bytes": sum(size for _, size in entries),
        "classification": dict(reasons),
    }


def merge_review(isolated: sqlite3.Connection, target: sqlite3.Connection) -> tuple[int, int]:
    """Append review MMS rows and their immutable attachment references."""
    conversations: dict[int, int] = {}
    for source_id, name, handle, group, contact in isolated.execute(
        "SELECT id,name,handle,is_group,contact_id FROM conversations WHERE source=? ORDER BY id",
        (SOURCE,)
    ):
        if group != 0 or contact is not None:
            raise ValueError("held MMS review asserted an unverified group or contact")
        target.execute("INSERT INTO conversations(source,name,handle,is_group) VALUES(?,?,?,0) "
                       "ON CONFLICT(source,name) DO NOTHING", (SOURCE, name, handle))
        row = target.execute(
            "SELECT id,handle,is_group,contact_id FROM conversations WHERE source=? AND name=?",
            (SOURCE, name)
        ).fetchone()
        if row is None or row[1:] != (handle, 0, None):
            raise ValueError("held MMS review conversation conflict")
        conversations[source_id] = row[0]
    messages: dict[int, int] = {}
    added_messages = 0
    for source_id, identity, source_conv, ts, timestamp, sender, body in isolated.execute(
        "SELECT id,hash,conversation_id,ts,ts_unix,sender,body FROM messages "
        "WHERE source=? ORDER BY id", (SOURCE,)
    ):
        target_conv = conversations[source_conv]
        expected = (target_conv, SOURCE, ts, timestamp, sender, body, 0, 0, None)
        row = target.execute(
            "SELECT id,conversation_id,source,ts,ts_unix,sender,body,is_system,seq,source_guid "
            "FROM messages WHERE hash=?", (identity,)
        ).fetchone()
        if row is None:
            cursor = target.execute(
                "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,"
                "sender,body,is_system,seq) VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                (identity, target_conv, SOURCE, ts, timestamp, sender, body)
            )
            if cursor.lastrowid is None:
                raise ValueError("held MMS review insert returned no ID")
            messages[source_id] = cursor.lastrowid
            added_messages += 1
        elif row[1:] == expected:
            messages[source_id] = row[0]
        else:
            raise ValueError("held MMS review message conflict")
    added_attachments = 0
    for source_message, kind, rel, original, source_conv, timestamp in isolated.execute(
        "SELECT a.message_id,a.kind,a.rel_path,a.original_name,a.conversation_id,a.ts_unix "
        "FROM attachments a JOIN messages m ON m.id=a.message_id "
        "WHERE m.source=? ORDER BY a.id", (SOURCE,)
    ):
        target_message, target_conv = messages[source_message], conversations[source_conv]
        existing = target.execute(
            "SELECT kind,original_name,conversation_id,ts_unix FROM attachments "
            "WHERE message_id=? AND rel_path=?", (target_message, rel)
        ).fetchone()
        expected = (kind, original, target_conv, timestamp)
        if existing is None:
            target.execute(
                "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                "VALUES(?,?,?,?,?,?)",
                (target_message, kind, rel, original, target_conv, timestamp)
            )
            added_attachments += 1
        elif existing != expected:
            raise ValueError("held MMS review attachment conflict")
    return added_messages, added_attachments


def merge_apple_preview(target: sqlite3.Connection, subject: Parent,
                        apple_ids: tuple[int, ...], preview_rel: str) -> int:
    """Attach one inline variant to matching Apple rows without a new card."""
    if archive_matches(subject, target) != apple_ids:
        raise ValueError("Apple MMS destinations changed")
    preview = next(asset for asset in subject.assets if not asset.already_cataloged)
    added = 0
    for message_id in apple_ids:
        row = target.execute(
            "SELECT conversation_id,ts_unix FROM messages WHERE id=? AND source='imessage'",
            (message_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Apple destination message missing")
        expected = ("file", preview.digest + ".png", row[0], row[1])
        existing = target.execute(
            "SELECT kind,original_name,conversation_id,ts_unix FROM attachments "
            "WHERE message_id=? AND rel_path=?", (message_id, preview_rel)
        ).fetchone()
        if existing is None:
            target.execute(
                "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                "VALUES(?,'file',?,?,?,?)",
                (message_id, preview_rel, preview.digest + ".png", row[0], row[1])
            )
            added += 1
        elif existing != expected:
            raise ValueError("Apple preview attachment conflict")
    return added


def apply_plan(plan: dict[str, object], checkpoint: Path) -> dict[str, int]:
    """Recheck a saved plan, checkpoint SQLite, copy media, and merge rows."""
    source_root = Path(str(plan["source_root"]))
    catalog = Path(str(plan["catalog"]))
    isolated_path = Path(str(plan["isolated_db"])).resolve()
    target_path = Path(str(plan["target_db"])).resolve()
    config = Path(str(plan["config"]))
    checkpoint = checkpoint.resolve()
    if checkpoint.exists() or checkpoint == target_path:
        raise ValueError("checkpoint must be new and separate")
    if "/Volumes/KINGSTON/" in str(target_path) or "/Volumes/KINGSTON/" in str(checkpoint):
        raise ValueError("KINGSTON writes are on hold")
    if build_plan(source_root, catalog, isolated_path, target_path, config) != plan:
        raise ValueError("held MMS plan is stale")
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(open_readonly(target_path)) as original, closing(sqlite3.connect(checkpoint)) as saved:
        original.backup(saved)
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("held MMS SQLite checkpoint failed integrity check")
    with closing(open_readonly(isolated_path)) as isolated:
        with closing(open_readonly(target_path)) as target_read:
            review, subject, apple_ids, _ = select_gaps(source_root, catalog, target_read, config)
        entries = expected_media(review, subject)
        verify_isolated_media(isolated_path, entries)
        copied = copy_verified_media(isolated_path.parent, target_path.parent, entries)
        with closing(sqlite3.connect(target_path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            try:
                raw_ids = plan["apple_ids"]
                if not isinstance(raw_ids, list):
                    raise ValueError("saved Apple destination list is invalid")
                ids = tuple(int(value) for value in raw_ids)
                rel = str(plan["preview_rel"])
                if digest_rows(target, False, ids, rel) != plan["baseline_sha256"]:
                    raise ValueError("configured archive changed after planning")
                if digest_rows(isolated, True, ids, rel) != plan["review_rows_sha256"]:
                    raise ValueError("isolated review rows changed")
                if verify_sources(source_root) != plan["source_sha256"]:
                    raise ValueError("verified MMS source changed")
                if hashlib.sha256(catalog.read_bytes()).hexdigest() != plan["catalog_sha256"]:
                    raise ValueError("MMS catalog changed")
                if hashlib.sha256(config.read_bytes()).hexdigest() != plan["config_sha256"]:
                    raise ValueError("owner exclusions changed")
                added_messages, review_refs = merge_review(isolated, target)
                apple_refs = merge_apple_preview(target, subject, apple_ids, rel)
                if added_messages != plan["messages_to_add"] or apple_refs != plan["apple_references_to_add"]:
                    raise ValueError("held MMS merge counts changed")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("held MMS foreign-key check failed")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("held MMS SQLite integrity check failed")
                if digest_rows(target, False, ids, rel) != plan["baseline_sha256"]:
                    raise ValueError("preexisting archive rows changed")
                target.commit()
            except BaseException:
                target.rollback()
                raise
    return {"messages_added": added_messages, "attachment_references_added": review_refs + apple_refs,
            "media_files_copied": copied}


def main() -> None:
    """Write a private plan or apply a checked saved plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--isolated-db", type=Path)
    parser.add_argument("--target-db", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--apply-plan", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.apply_plan:
        if args.checkpoint is None:
            parser.error("--checkpoint is required with --apply-plan")
        plan = json.loads(args.apply_plan.read_text())
        LOG.info("held MMS checked merge: %s", json.dumps(
            apply_plan(plan, args.checkpoint), sort_keys=True))
    else:
        if any(value is None for value in (args.source_root, args.catalog, args.isolated_db,
                                           args.target_db, args.config, args.plan_out)):
            parser.error("source, catalog, isolated database, target, config, and plan output are required")
        plan = build_plan(args.source_root, args.catalog, args.isolated_db,
                          args.target_db, args.config)
        if args.plan_out.exists():
            raise ValueError("plan output already exists")
        args.plan_out.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        args.plan_out.chmod(0o600)
        LOG.info("held MMS checked plan: %s", json.dumps({
            "messages_to_add": plan["messages_to_add"],
            "apple_references_to_add": plan["apple_references_to_add"],
            "media_files": plan["media_files"], "media_bytes": plan["media_bytes"],
        }, sort_keys=True))


if __name__ == "__main__":
    main()
