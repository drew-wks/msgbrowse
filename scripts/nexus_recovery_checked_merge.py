#!/usr/bin/env python3
"""Checked append-only transfer of isolated Nexus recovery rows to the archive."""

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

from scripts.nexus_recovery_import import SOURCE, select_recovery
from scripts.nexus_sms_import import xml_files


LOG = logging.getLogger("nexus-recovery-merge")


def readonly(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without write access."""
    if not path.is_file():
        raise ValueError("database is absent")
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def source_digest(root: Path) -> str:
    """Hash all copied source XML in a stable order."""
    digest = hashlib.sha256()
    for path in sorted(xml_files(root)):
        digest.update(path.name.encode())
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def rows_digest(db: sqlite3.Connection, *, recovery: bool) -> str:
    """Hash core archive rows on one side of the recovery source boundary."""
    where = "=" if recovery else "!="
    queries = (
        ("conversations", f"SELECT * FROM conversations WHERE source{where}? ORDER BY id", (SOURCE,)),
        ("messages", f"SELECT * FROM messages WHERE source{where}? ORDER BY id", (SOURCE,)),
        ("attachments", "SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id "
         f"WHERE m.source{where}? ORDER BY a.id", (SOURCE,)),
    )
    digest = hashlib.sha256()
    for table, query, parameters in queries:
        for row in db.execute(query, parameters):
            digest.update(json.dumps([table, row], ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    if not recovery:
        for table in ("contacts", "contact_identifiers", "imessage_excluded_messages"):
            for row in db.execute(f"SELECT * FROM {table} ORDER BY id"):
                digest.update(json.dumps([table, row], ensure_ascii=False, separators=(",", ":")).encode())
                digest.update(b"\n")
    return digest.hexdigest()


def media_entries(db: sqlite3.Connection, isolated_dir: Path) -> list[tuple[str, int]]:
    """Verify isolated recovery media and return relative paths and sizes."""
    entries: list[tuple[str, int]] = []
    rels = {row[0] for row in db.execute(
        "SELECT a.rel_path FROM attachments a JOIN messages m ON m.id=a.message_id "
        "WHERE m.source=?", (SOURCE,))}
    for rel in sorted(rels):
        if re.fullmatch(r"permanent/[0-9a-f]{64}\.[A-Za-z0-9]+", rel) is None:
            raise ValueError("recovery attachment path is not content addressed")
        path = isolated_dir / "media" / "nexus" / rel.removeprefix("permanent/")
        if path.is_symlink() or not path.is_file():
            raise ValueError("isolated recovery media is absent")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != path.stem:
                raise ValueError("isolated recovery media checksum differs")
        entries.append((rel, path.stat().st_size))
    return entries


def build_plan(source_root: Path, staging: Path, config: Path, ledger: Path,
               isolated_path: Path, target_path: Path) -> dict[str, object]:
    """Reclassify source records and prove the isolated rows match them."""
    paths = [p.resolve() for p in (source_root, staging, config, ledger, isolated_path, target_path)]
    source_root, staging, config, ledger, isolated_path, target_path = paths
    if isolated_path == target_path:
        raise ValueError("isolated and configured databases are the same")
    with closing(readonly(target_path)) as target, closing(readonly(isolated_path)) as isolated:
        baseline = rows_digest(target, recovery=False)
        if rows_digest(isolated, recovery=False) != baseline:
            raise ValueError("isolated baseline differs from configured archive")
        selected_sms, selected_mms, reasons = select_recovery(source_root, config, ledger, target)
        accepted = {record.identity for record, _ in (*selected_sms, *selected_mms)}
        isolated_ids = {row[0] for row in isolated.execute(
            "SELECT hash FROM messages WHERE source=?", (SOURCE,))}
        if accepted != isolated_ids:
            raise ValueError("isolated recovery rows differ from current source classification")
        existing = {row[0] for row in target.execute(
            "SELECT hash FROM messages WHERE source=?", (SOURCE,))}
        if not existing.issubset(isolated_ids):
            raise ValueError("configured archive contains unplanned recovery rows")
        entries = media_entries(isolated, isolated_path.parent)
        attachments = isolated.execute(
            "SELECT count(*) FROM attachments a JOIN messages m ON m.id=a.message_id "
            "WHERE m.source=?", (SOURCE,)).fetchone()[0]
        recovered = rows_digest(isolated, recovery=True)
    return {
        "source_root": str(source_root), "staging": str(staging), "config": str(config),
        "ledger": str(ledger), "isolated_db": str(isolated_path), "target_db": str(target_path),
        "source_sha256": source_digest(source_root),
        "staging_manifest_sha256": hashlib.sha256((staging / "manifest.json").read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest(),
        "baseline_sha256": baseline, "recovery_rows_sha256": recovered,
        "accepted_sha256": hashlib.sha256("\n".join(sorted(accepted)).encode()).hexdigest(),
        "accepted_messages": len(accepted), "already_present": len(existing),
        "messages_to_add": len(accepted - existing), "attachment_references": attachments,
        "media_files": len(entries), "media_bytes": sum(size for _, size in entries),
        "disposition": dict(reasons),
    }


def copy_media(isolated_dir: Path, target_dir: Path,
               entries: list[tuple[str, int]]) -> int:
    """Copy verified media by content hash without replacing existing files."""
    target_root = target_dir / "media" / "nexus"
    if target_root.is_symlink() or not target_root.resolve().is_relative_to(target_dir.resolve()):
        raise ValueError("configured Nexus media root escapes archive")
    copied = 0
    for rel, size in entries:
        filename = rel.removeprefix("permanent/")
        source = isolated_dir / "media" / "nexus" / filename
        target = target_root / filename
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent.is_symlink():
            raise ValueError("configured media directory is a symlink")
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".nexus-recovery-", delete=False) as stream:
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
            raise ValueError("configured recovery media is absent or changed")
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != Path(filename).stem:
                raise ValueError("configured recovery media checksum differs")
    return copied


def merge_rows(isolated: sqlite3.Connection, target: sqlite3.Connection) -> tuple[int, int]:
    """Append all isolated recovery rows and reject identity conflicts."""
    conversation_ids: dict[int, int] = {}
    for old_id, name, contact_id, handle, group in isolated.execute(
        "SELECT id,name,contact_id,handle,is_group FROM conversations WHERE source=? ORDER BY id", (SOURCE,)):
        target.execute(
            "INSERT INTO conversations(source,name,contact_id,handle,is_group) VALUES(?,?,?,?,?) "
            "ON CONFLICT(source,name) DO NOTHING", (SOURCE, name, contact_id, handle, group))
        row = target.execute(
            "SELECT id,contact_id,handle,is_group FROM conversations WHERE source=? AND name=?",
            (SOURCE, name)).fetchone()
        if row is None or row[1:] != (contact_id, handle, group):
            raise ValueError("recovery conversation conflict")
        conversation_ids[old_id] = row[0]
    message_ids: dict[int, int] = {}
    added_messages = 0
    for row in isolated.execute(
        "SELECT id,hash,conversation_id,ts,ts_unix,sender,body,is_system,seq,source_guid "
        "FROM messages WHERE source=? ORDER BY id", (SOURCE,)):
        old_id, identity, old_conversation, ts, timestamp, sender, body, system, seq, guid = row
        if guid is not None:
            raise ValueError("recovery message has unexpected Apple GUID")
        conversation_id = conversation_ids[old_conversation]
        expected = (conversation_id, ts, timestamp, sender, body, system, seq, SOURCE, None)
        existing = target.execute(
            "SELECT id,conversation_id,ts,ts_unix,sender,body,is_system,seq,source,source_guid "
            "FROM messages WHERE hash=?", (identity,)).fetchone()
        if existing is None:
            cursor = target.execute(
                "INSERT INTO messages(hash,conversation_id,ts,ts_unix,sender,body,is_system,seq,source,source_guid) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (identity, conversation_id, ts, timestamp, sender, body, system, seq, SOURCE))
            if cursor.lastrowid is None:
                raise ValueError("recovery insert returned no message ID")
            message_ids[old_id] = cursor.lastrowid
            added_messages += 1
        elif existing[1:] == expected:
            message_ids[old_id] = existing[0]
        else:
            raise ValueError("recovery message identity conflict")
    added_attachments = 0
    for row in isolated.execute(
        "SELECT a.message_id,a.kind,a.rel_path,a.original_name,a.conversation_id,a.ts_unix "
        "FROM attachments a JOIN messages m ON m.id=a.message_id WHERE m.source=? ORDER BY a.id", (SOURCE,)):
        old_message, kind, rel, original, old_conversation, timestamp = row
        message_id = message_ids[old_message]
        conversation_id = conversation_ids[old_conversation]
        existing = target.execute(
            "SELECT original_name,conversation_id,ts_unix FROM attachments "
            "WHERE message_id=? AND kind=? AND rel_path=?", (message_id, kind, rel)).fetchone()
        if existing is None:
            target.execute(
                "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                "VALUES(?,?,?,?,?,?)", (message_id, kind, rel, original, conversation_id, timestamp))
            added_attachments += 1
        elif existing != (original, conversation_id, timestamp):
            raise ValueError("recovery attachment conflict")
    return added_messages, added_attachments


def apply_plan(plan: dict[str, object], checkpoint: Path) -> dict[str, int]:
    """Revalidate the saved plan, checkpoint SQLite, and commit the merge."""
    checkpoint = checkpoint.resolve()
    target_path = Path(str(plan["target_db"])).resolve()
    isolated_path = Path(str(plan["isolated_db"])).resolve()
    if checkpoint.exists() or checkpoint == target_path or str(checkpoint).startswith("/Volumes/KINGSTON/"):
        raise ValueError("checkpoint must be new, local, and separate")
    if str(target_path).startswith("/Volumes/KINGSTON/"):
        raise ValueError("KINGSTON writes are on hold")
    current = build_plan(*(Path(str(plan[key])) for key in (
        "source_root", "staging", "config", "ledger", "isolated_db", "target_db")))
    if current != plan:
        raise ValueError("Nexus recovery plan changed")
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(readonly(target_path)) as original, closing(sqlite3.connect(checkpoint)) as saved:
        original.backup(saved)
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite checkpoint failed")
    with closing(readonly(isolated_path)) as isolated:
        entries = media_entries(isolated, isolated_path.parent)
        copied = copy_media(isolated_path.parent, target_path.parent, entries)
        with closing(sqlite3.connect(target_path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            try:
                if rows_digest(target, recovery=False) != plan["baseline_sha256"]:
                    raise ValueError("configured archive changed after planning")
                added_messages, added_attachments = merge_rows(isolated, target)
                if added_messages != plan["messages_to_add"]:
                    raise ValueError("unexpected recovery insertion count")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("foreign key check failed")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
                if rows_digest(target, recovery=False) != plan["baseline_sha256"]:
                    raise ValueError("preexisting archive rows changed")
                if rows_digest(target, recovery=True) != plan["recovery_rows_sha256"]:
                    # New row IDs can differ only when the live archive changed;
                    # in that case the baseline check above has already failed.
                    raise ValueError("merged recovery rows differ from isolated rows")
                target.commit()
            except BaseException:
                target.rollback()
                raise
    return {"messages_added": added_messages, "attachments_added": added_attachments,
            "media_files_copied": copied}


def main() -> None:
    """Save a private checked plan or apply an existing one."""
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source-root", "staging", "config", "ledger", "isolated-db", "target-db"):
        parser.add_argument("--" + flag, required=True, type=Path)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--write-plan", type=Path)
    operation.add_argument("--apply-plan", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    paths = (args.source_root, args.staging, args.config, args.ledger,
             args.isolated_db, args.target_db)
    if args.write_plan:
        plan = build_plan(*paths)
        descriptor = os.open(args.write_plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, indent=2, sort_keys=True)
            stream.write("\n")
        LOG.info("recovery plan: %s", json.dumps({key: plan[key] for key in (
            "messages_to_add", "attachment_references", "media_files", "media_bytes")}, sort_keys=True))
    else:
        if args.checkpoint is None:
            parser.error("--checkpoint is required for apply")
        plan = json.loads(args.apply_plan.read_text())
        if tuple(str(p.resolve()) for p in paths) != tuple(plan[key] for key in (
            "source_root", "staging", "config", "ledger", "isolated_db", "target_db")):
            raise ValueError("command paths differ from saved plan")
        LOG.info("recovery result: %s", json.dumps(apply_plan(plan, args.checkpoint), sort_keys=True))


if __name__ == "__main__":
    main()
