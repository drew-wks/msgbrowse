#!/usr/bin/env python3
"""Plan and apply a checked, append-only legacy iPhone MMS merge."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from scripts.legacy_mms_import import SOURCE, classify, load_parents, verify_sources


LOG = logging.getLogger("legacy-mms-checked-merge")


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file without permitting writes."""
    if not path.is_file():
        raise ValueError("SQLite input does not exist")
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def digest_baseline(db: sqlite3.Connection) -> str:
    """Hash all preexisting rows that the legacy merge must preserve."""
    queries = (
        ("conversations", "SELECT * FROM conversations WHERE source!='legacy_mms' ORDER BY id"),
        ("messages", "SELECT * FROM messages WHERE source!='legacy_mms' ORDER BY id"),
        ("attachments", "SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id "
         "WHERE m.source!='legacy_mms' ORDER BY a.id"),
        ("contacts", "SELECT * FROM contacts ORDER BY id"),
        ("identifiers", "SELECT * FROM contact_identifiers ORDER BY id"),
        ("exclusions", "SELECT * FROM imessage_excluded_messages ORDER BY id"),
    )
    digest = hashlib.sha256()
    for label, query in queries:
        for row in db.execute(query):
            digest.update(json.dumps([label, row], ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def digest_legacy(db: sqlite3.Connection) -> str:
    """Hash isolated legacy conversations, messages, and attachment rows."""
    queries = (
        ("conversations", "SELECT * FROM conversations WHERE source='legacy_mms' ORDER BY id"),
        ("messages", "SELECT * FROM messages WHERE source='legacy_mms' ORDER BY id"),
        ("attachments", "SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id "
         "WHERE m.source='legacy_mms' ORDER BY a.id"),
    )
    digest = hashlib.sha256()
    for label, query in queries:
        for row in db.execute(query):
            digest.update(json.dumps([label, row], ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def media_entries(isolated: sqlite3.Connection, isolated_dir: Path) -> list[tuple[str, int]]:
    """Verify referenced isolated media and return its path and byte count."""
    rels = {rel for (rel,) in isolated.execute(
        "SELECT a.rel_path FROM attachments a JOIN messages m ON m.id=a.message_id "
        "WHERE m.source='legacy_mms'"
    )}
    entries: list[tuple[str, int]] = []
    for rel in sorted(rels):
        if re.fullmatch(r"permanent/[0-9a-f]{2}/[0-9a-f]{64}\.[A-Za-z0-9]+", rel) is None:
            raise ValueError("legacy MMS attachment path is not content-addressed")
        path = isolated_dir / "media" / "imessage" / rel.removeprefix("permanent/")
        if path.is_symlink() or not path.is_file():
            raise ValueError("isolated legacy MMS media is absent")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != Path(rel).stem:
                raise ValueError("isolated legacy MMS media checksum mismatch")
        entries.append((rel, path.stat().st_size))
    return entries


def build_plan(source_root: Path, catalog: Path, isolated_path: Path,
               target_path: Path, config_path: Path) -> dict[str, object]:
    """Reclassify source data and compare isolated rows with the live archive."""
    source_root, catalog = source_root.resolve(), catalog.resolve()
    isolated_path, target_path = isolated_path.resolve(), target_path.resolve()
    config_path = config_path.resolve()
    if isolated_path == target_path:
        raise ValueError("isolated and configured databases must differ")
    source_digest = verify_sources(source_root)
    parents, _ = load_parents(source_root, catalog)
    with closing(open_readonly(isolated_path)) as isolated, closing(open_readonly(target_path)) as target:
        baseline = digest_baseline(target)
        if digest_baseline(isolated) != baseline:
            raise ValueError("isolated baseline differs from configured archive")
        selected, reasons, _ = classify(parents, target, config_path)
        accepted = {parent.identity for parent in selected}
        isolated_ids = {identity for (identity,) in isolated.execute(
            "SELECT hash FROM messages WHERE source='legacy_mms'"
        )}
        if accepted != isolated_ids:
            raise ValueError("isolated legacy rows differ from current source classification")
        existing = {identity for (identity,) in target.execute(
            "SELECT hash FROM messages WHERE source='legacy_mms'"
        )}
        if not existing.issubset(isolated_ids):
            raise ValueError("configured archive contains unplanned legacy MMS")
        entries = media_entries(isolated, isolated_path.parent)
        row_digest = digest_legacy(isolated)
        attachment_count = isolated.execute(
            "SELECT count(*) FROM attachments a JOIN messages m ON m.id=a.message_id "
            "WHERE m.source='legacy_mms'"
        ).fetchone()[0]
    return {
        "source_root": str(source_root), "catalog": str(catalog),
        "isolated_db": str(isolated_path), "target_db": str(target_path),
        "config": str(config_path), "source_sha256": source_digest,
        "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "baseline_sha256": baseline, "legacy_rows_sha256": row_digest,
        "accepted_sha256": hashlib.sha256("\n".join(sorted(accepted)).encode()).hexdigest(),
        "accepted_messages": len(accepted), "already_present": len(existing),
        "messages_to_add": len(accepted - existing),
        "attachment_references": attachment_count,
        "media_files": len(entries), "media_bytes": sum(size for _, size in entries),
        "classification": dict(reasons),
    }


def copy_verified_media(isolated_dir: Path, target_dir: Path,
                        entries: list[tuple[str, int]]) -> int:
    """Copy new immutable media without replacing an existing destination."""
    target_root = target_dir / "media" / "imessage"
    if target_root.is_symlink() or not target_root.resolve().is_relative_to(target_dir.resolve()):
        raise ValueError("configured media root escapes archive")
    copied = 0
    for rel, size in entries:
        name = rel.removeprefix("permanent/")
        source = isolated_dir / "media" / "imessage" / name
        target = target_root / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent.is_symlink() or not target.parent.resolve().is_relative_to(target_root.resolve()):
            raise ValueError("configured media subfolder escapes archive")
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".legacy-mms-", delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    with source.open("rb") as input_stream:
                        shutil.copyfileobj(input_stream, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.link(temporary, target)
                copied += 1
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
        if target.is_symlink() or not target.is_file() or target.stat().st_size != size:
            raise ValueError("configured legacy MMS media is missing or changed")
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != Path(name).stem:
                raise ValueError("configured legacy MMS media checksum mismatch")
    return copied


def merge_rows(isolated: sqlite3.Connection, target: sqlite3.Connection) -> tuple[int, int]:
    """Append isolated legacy rows while verifying every existing identity."""
    mapping: dict[int, int] = {}
    for source_id, name, contact_id, handle, group in isolated.execute(
        "SELECT id,name,contact_id,handle,is_group FROM conversations "
        "WHERE source='legacy_mms' ORDER BY id"
    ):
        target.execute(
            "INSERT INTO conversations(source,name,contact_id,handle,is_group) "
            "VALUES(?,?,?,?,?) ON CONFLICT(source,name) DO NOTHING",
            (SOURCE, name, contact_id, handle, group),
        )
        row = target.execute(
            "SELECT id,contact_id,handle,is_group FROM conversations "
            "WHERE source=? AND name=?", (SOURCE, name),
        ).fetchone()
        if row is None or row[1:] != (contact_id, handle, group):
            raise ValueError("legacy MMS conversation conflict")
        mapping[source_id] = row[0]
    message_ids: dict[int, int] = {}
    messages_added = 0
    for row in isolated.execute(
        "SELECT id,hash,conversation_id,ts,ts_unix,sender,body,is_system,seq,source_guid "
        "FROM messages WHERE source='legacy_mms' ORDER BY id"
    ):
        source_id, identity, source_conversation, ts, ts_unix, sender, body, system, seq, guid = row
        if guid is not None:
            raise ValueError("legacy MMS unexpectedly has an Apple GUID")
        target_conversation = mapping[source_conversation]
        expected = (target_conversation, ts, ts_unix, sender, body, system, seq, SOURCE, None)
        existing = target.execute(
            "SELECT id,conversation_id,ts,ts_unix,sender,body,is_system,seq,source,source_guid "
            "FROM messages WHERE hash=?", (identity,)
        ).fetchone()
        if existing is None:
            cursor = target.execute(
                "INSERT INTO messages(hash,conversation_id,ts,ts_unix,sender,body,is_system,seq,source,source_guid) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (identity, target_conversation, ts, ts_unix, sender, body, system, seq, SOURCE),
            )
            if cursor.lastrowid is None:
                raise ValueError("legacy MMS insertion returned no message ID")
            message_ids[source_id] = cursor.lastrowid
            messages_added += 1
        elif existing[1:] == expected:
            message_ids[source_id] = existing[0]
        else:
            raise ValueError("legacy MMS message identity conflict")
    attachments_added = 0
    for row in isolated.execute(
        "SELECT a.message_id,a.kind,a.rel_path,a.original_name,a.conversation_id,a.ts_unix "
        "FROM attachments a JOIN messages m ON m.id=a.message_id "
        "WHERE m.source='legacy_mms' ORDER BY a.id"
    ):
        source_message, kind, rel, original, source_conversation, ts_unix = row
        target_message = message_ids[source_message]
        target_conversation = mapping[source_conversation]
        existing = target.execute(
            "SELECT original_name,conversation_id,ts_unix FROM attachments "
            "WHERE message_id=? AND kind=? AND rel_path=?", (target_message, kind, rel)
        ).fetchone()
        if existing is None:
            target.execute(
                "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                "VALUES(?,?,?,?,?,?)", (target_message, kind, rel, original, target_conversation, ts_unix),
            )
            attachments_added += 1
        elif existing != (original, target_conversation, ts_unix):
            raise ValueError("legacy MMS attachment identity conflict")
    return messages_added, attachments_added


def apply_plan(plan: dict[str, object], checkpoint: Path) -> dict[str, int]:
    """Recheck the plan, checkpoint SQLite, then merge without overwrites."""
    target_path = Path(str(plan["target_db"])).resolve()
    isolated_path = Path(str(plan["isolated_db"])).resolve()
    checkpoint = checkpoint.resolve()
    if checkpoint.exists() or checkpoint == target_path:
        raise ValueError("checkpoint path must be new and separate")
    if "/Volumes/KINGSTON/" in str(target_path) or "/Volumes/KINGSTON/" in str(checkpoint):
        raise ValueError("KINGSTON writes are on hold")
    current = build_plan(Path(str(plan["source_root"])), Path(str(plan["catalog"])),
                         isolated_path, target_path, Path(str(plan["config"])))
    if current != plan:
        raise ValueError("legacy MMS merge plan is stale")
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(open_readonly(target_path)) as original, closing(sqlite3.connect(checkpoint)) as saved:
        original.backup(saved)
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite checkpoint failed integrity check")
    with closing(open_readonly(isolated_path)) as isolated:
        if digest_legacy(isolated) != plan["legacy_rows_sha256"]:
            raise ValueError("isolated legacy rows changed")
        entries = media_entries(isolated, isolated_path.parent)
        copied = copy_verified_media(isolated_path.parent, target_path.parent, entries)
        with closing(sqlite3.connect(target_path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            try:
                if digest_baseline(target) != plan["baseline_sha256"]:
                    raise ValueError("configured archive changed after planning")
                if verify_sources(Path(str(plan["source_root"]))) != plan["source_sha256"]:
                    raise ValueError("legacy source changed after planning")
                if hashlib.sha256(Path(str(plan["catalog"])).read_bytes()).hexdigest() != plan["catalog_sha256"]:
                    raise ValueError("legacy catalog changed after planning")
                if hashlib.sha256(Path(str(plan["config"])).read_bytes()).hexdigest() != plan["config_sha256"]:
                    raise ValueError("configuration changed after planning")
                added_messages, added_attachments = merge_rows(isolated, target)
                if added_messages != plan["messages_to_add"]:
                    raise ValueError("unexpected legacy MMS insertion count")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("foreign-key check failed")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
                if digest_baseline(target) != plan["baseline_sha256"]:
                    raise ValueError("preexisting archive rows changed")
                target.commit()
            except BaseException:
                target.rollback()
                raise
    return {"messages_added": added_messages, "attachments_added": added_attachments,
            "media_files_copied": copied}


def main() -> None:
    """Write a private plan or apply it with a fresh SQLite checkpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--isolated-db", required=True, type=Path)
    parser.add_argument("--target-db", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-plan", type=Path)
    group.add_argument("--apply-plan", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.write_plan:
        plan = build_plan(args.source_root, args.catalog, args.isolated_db,
                          args.target_db, args.config)
        descriptor = os.open(args.write_plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, indent=2, sort_keys=True)
            stream.write("\n")
        LOG.info("legacy MMS merge plan: %s", json.dumps({k: plan[k] for k in (
            "messages_to_add", "attachment_references", "media_files", "media_bytes"
        )}, sort_keys=True))
    else:
        if args.checkpoint is None:
            parser.error("--checkpoint is required when applying a plan")
        plan = json.loads(args.apply_plan.read_text())
        expected = (str(args.source_root.resolve()), str(args.catalog.resolve()),
                    str(args.isolated_db.resolve()), str(args.target_db.resolve()),
                    str(args.config.resolve()))
        if expected != tuple(plan[k] for k in (
            "source_root", "catalog", "isolated_db", "target_db", "config"
        )):
            raise ValueError("command paths differ from saved plan")
        LOG.info("legacy MMS merge result: %s", json.dumps(apply_plan(plan, args.checkpoint), sort_keys=True))


if __name__ == "__main__":
    main()
