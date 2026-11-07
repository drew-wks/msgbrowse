#!/usr/bin/env python3
"""Plan and perform a checked, append-only merge of isolated Nexus records.

The plan compares the current archive with the isolated copy and reclassifies
the immutable Nexus XML. Applying requires that exact plan and a fresh SQLite
checkpoint path. No existing media file or archived row is overwritten.
"""

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

from scripts.nexus_mms_import import classify as classify_mms, load_mms
from scripts.nexus_sms_import import (
    archive_times,
    blocked_addresses,
    classify as classify_sms,
    load_sms,
    normalize_address,
)


LOG = logging.getLogger("nexus-checked-merge")
SOURCE = "nexus"


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without permitting writes."""
    if not path.is_file():
        raise ValueError("database does not exist")
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def digest_rows(db: sqlite3.Connection) -> str:
    """Hash original messages, child attachments, and the exclusion ledger."""
    queries = (
        ("messages", "SELECT * FROM messages WHERE source!='nexus' ORDER BY id"),
        ("attachments", "SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id "
         "WHERE m.source!='nexus' ORDER BY a.id"),
        ("ledger", "SELECT * FROM imessage_excluded_messages ORDER BY id"),
    )
    digest = hashlib.sha256()
    for label, query in queries:
        for row in db.execute(query):
            digest.update(json.dumps([label, row], ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def digest_nexus(db: sqlite3.Connection) -> str:
    """Hash isolated Nexus conversations, messages, and attachments."""
    queries = (
        ("conversations", "SELECT * FROM conversations WHERE source='nexus' ORDER BY id"),
        ("messages", "SELECT * FROM messages WHERE source='nexus' ORDER BY id"),
        ("attachments", "SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id "
         "WHERE m.source='nexus' ORDER BY a.id"),
    )
    digest = hashlib.sha256()
    for label, query in queries:
        for row in db.execute(query):
            digest.update(json.dumps([label, row], ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def digest_source_tree(root: Path) -> tuple[str, int]:
    """Hash every copied Nexus XML and its relative path."""
    files = sorted(root.rglob("*.xml"))
    if not files:
        raise ValueError("no Nexus XML inputs found")
    digest = hashlib.sha256()
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Nexus XML source is not a regular file")
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            file_hash = hashlib.file_digest(stream, "sha256").digest()
        digest.update(file_hash)
    return digest.hexdigest(), len(files)


def accepted_hashes(
    source_root: Path, config_path: Path, target: sqlite3.Connection
) -> tuple[set[str], dict[str, int]]:
    """Reapply the current exclusion and overlap rules to all Nexus XML."""
    sms, _ = load_sms(source_root)
    blocked = blocked_addresses(config_path)
    times = archive_times(target)
    selected_sms, _, held_sms = classify_sms(sms, blocked, times)
    mms, _, owner_handles = load_mms(source_root)
    optout = {
        normalize_address(record.address)
        for record in sms
        if record.kind == 2 and record.body.strip().casefold() in ("stop", "quit")
    }
    selected_mms, _, held_mms = classify_mms(mms, owner_handles, blocked, optout, times)
    accepted = {record.identity for record in selected_sms}
    accepted.update(record.identity for record in selected_mms)
    return accepted, {
        "sms_accepted": len(selected_sms), "sms_held": len(held_sms),
        "mms_accepted": len(selected_mms), "mms_held": len(held_mms),
    }


def media_entries(isolated: sqlite3.Connection, isolated_dir: Path) -> list[tuple[str, int]]:
    """Verify every source MMS file and return unique relative paths and sizes."""
    paths = {
        row[0] for row in isolated.execute(
            "SELECT a.rel_path FROM attachments a JOIN messages m ON m.id=a.message_id "
            "WHERE m.source='nexus'"
        )
    }
    entries: list[tuple[str, int]] = []
    for rel in sorted(paths):
        if not rel.startswith("permanent/"):
            raise ValueError("Nexus attachment is not in permanent media")
        name = rel.removeprefix("permanent/")
        if re.fullmatch(r"[0-9a-f]{64}\.[A-Za-z0-9]+", name) is None:
            raise ValueError("Nexus media name lacks a SHA-256 digest")
        expected = name.split(".", 1)[0]
        path = isolated_dir / "media" / SOURCE / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Nexus media source is missing or not a regular file")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("Nexus media checksum mismatch")
        entries.append((rel, path.stat().st_size))
    return entries


def build_plan(
    source_root: Path, isolated_path: Path, target_path: Path, config_path: Path
) -> dict[str, object]:
    """Validate the source and target, returning a content-free merge plan."""
    source_root = source_root.resolve()
    isolated_path = isolated_path.resolve()
    target_path = target_path.resolve()
    config_path = config_path.resolve()
    if isolated_path == target_path or not source_root.is_dir():
        raise ValueError("source and target must be separate existing inputs")
    with closing(open_readonly(isolated_path)) as isolated, closing(open_readonly(target_path)) as target:
        baseline = digest_rows(isolated)
        if baseline != digest_rows(target):
            raise ValueError("configured archive differs from the isolated baseline")
        accepted, classification = accepted_hashes(source_root, config_path, target)
        source_hashes = {row[0] for row in isolated.execute("SELECT hash FROM messages WHERE source='nexus'")}
        if accepted != source_hashes:
            raise ValueError("isolated Nexus messages differ from current source classification")
        existing = {row[0] for row in target.execute("SELECT hash FROM messages WHERE source='nexus'")}
        if not existing.issubset(source_hashes):
            raise ValueError("target has unknown Nexus messages")
        entries = media_entries(isolated, isolated_path.parent)
        attachment_count = isolated.execute(
            "SELECT count(*) FROM attachments a JOIN messages m ON m.id=a.message_id "
            "WHERE m.source='nexus'"
        ).fetchone()[0]
        conversation_count = isolated.execute(
            "SELECT count(*) FROM conversations WHERE source='nexus'"
        ).fetchone()[0]
        nexus_digest = digest_nexus(isolated)
    digest = hashlib.sha256("\n".join(sorted(accepted)).encode()).hexdigest()
    source_digest, source_count = digest_source_tree(source_root)
    return {
        "source_root": str(source_root), "isolated_db": str(isolated_path),
        "target_db": str(target_path), "config": str(config_path),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "baseline_sha256": baseline, "accepted_identity_sha256": digest,
        "nexus_rows_sha256": nexus_digest, "source_tree_sha256": source_digest,
        "source_xml_files": source_count,
        "accepted_messages": len(accepted), "already_present": len(existing),
        "messages_to_add": len(accepted - existing),
        "nexus_conversations": conversation_count,
        "attachment_references": attachment_count,
        "media_files": len(entries), "media_bytes": sum(size for _, size in entries),
        "classification": classification,
    }


def copy_verified_media(isolated_dir: Path, target_dir: Path, entries: list[tuple[str, int]]) -> int:
    """Copy missing content-addressed files without replacing any existing file."""
    media_parent = target_dir / "media"
    if media_parent.is_symlink() or not media_parent.resolve().is_relative_to(target_dir.resolve()):
        raise ValueError("Nexus media destination escapes the target archive")
    destination = media_parent / SOURCE
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink() or not destination.resolve().is_relative_to(target_dir.resolve()):
        raise ValueError("Nexus media destination escapes the target archive")
    copied = 0
    for rel, size in entries:
        name = rel.removeprefix("permanent/")
        expected = name.split(".", 1)[0]
        source = isolated_dir / "media" / SOURCE / name
        target = destination / name
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=destination, prefix=".nexus-", delete=False) as stream:
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
            raise ValueError("target Nexus media is missing or changed")
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("target Nexus media checksum mismatch")
    return copied


def merge_rows(isolated: sqlite3.Connection, target: sqlite3.Connection) -> tuple[int, int]:
    """Append Nexus conversations, messages, and attachments in one transaction."""
    source_conversations = {
        row[0]: row[1:] for row in isolated.execute(
            "SELECT id,name,is_group,handle FROM conversations WHERE source='nexus' ORDER BY id"
        )
    }
    mapping: dict[int, int] = {}
    for source_id, (name, group, handle) in source_conversations.items():
        target.execute(
            "INSERT INTO conversations(source,name,is_group,handle) VALUES('nexus',?,?,?) "
            "ON CONFLICT(source,name) DO NOTHING", (name, group, handle)
        )
        row = target.execute(
            "SELECT id,is_group,handle FROM conversations WHERE source='nexus' AND name=?", (name,)
        ).fetchone()
        if row is None or row[1:] != (group, handle):
            raise ValueError("Nexus conversation conflict")
        mapping[source_id] = row[0]
    messages_added = 0
    message_ids: dict[int, int] = {}
    for row in isolated.execute(
        "SELECT id,hash,conversation_id,ts,ts_unix,sender,body,is_system,seq,source_guid "
        "FROM messages WHERE source='nexus' ORDER BY id"
    ):
        source_id, identity, source_conversation, ts, ts_unix, sender, body, system, seq, guid = row
        if guid is not None:
            raise ValueError("Nexus message unexpectedly has an Apple GUID")
        target_conversation = mapping[source_conversation]
        existing = target.execute(
            "SELECT id,conversation_id,ts,ts_unix,sender,body,is_system,seq,source,source_guid "
            "FROM messages WHERE hash=?", (identity,)
        ).fetchone()
        expected = (target_conversation, ts, ts_unix, sender, body, system, seq, SOURCE, None)
        if existing is None:
            cursor = target.execute(
                "INSERT INTO messages(hash,conversation_id,ts,ts_unix,sender,body,is_system,seq,source,source_guid) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (identity, target_conversation, ts, ts_unix, sender, body, system, seq, SOURCE),
            )
            if cursor.lastrowid is None:
                raise ValueError("Nexus message insert returned no ID")
            message_ids[source_id] = cursor.lastrowid
            messages_added += 1
        elif existing[1:] == expected:
            message_ids[source_id] = existing[0]
        else:
            raise ValueError("Nexus message identity conflict")
    attachments_added = 0
    for row in isolated.execute(
        "SELECT a.message_id,a.kind,a.rel_path,a.original_name,a.conversation_id,a.ts_unix "
        "FROM attachments a JOIN messages m ON m.id=a.message_id WHERE m.source='nexus' ORDER BY a.id"
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
                "VALUES(?,?,?,?,?,?)",
                (target_message, kind, rel, original, target_conversation, ts_unix),
            )
            attachments_added += 1
        elif existing != (original, target_conversation, ts_unix):
            raise ValueError("Nexus attachment identity conflict")
    return messages_added, attachments_added


def apply_plan(plan: dict[str, object], checkpoint: Path) -> dict[str, int]:
    """Recheck a saved plan, checkpoint SQLite, then append verified records."""
    target_path = Path(str(plan["target_db"])).resolve()
    isolated_path = Path(str(plan["isolated_db"])).resolve()
    checkpoint = checkpoint.resolve()
    if checkpoint.exists() or target_path == checkpoint:
        raise ValueError("checkpoint path must be new and separate")
    if str(target_path).startswith("/Volumes/KINGSTON/") or str(checkpoint).startswith("/Volumes/KINGSTON/"):
        raise ValueError("KINGSTON writes are on hold")
    current = build_plan(
        Path(str(plan["source_root"])), isolated_path, target_path,
        Path(str(plan["config"])),
    )
    if current != plan:
        raise ValueError("merge plan is stale")
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(open_readonly(target_path)) as original, closing(sqlite3.connect(checkpoint)) as saved:
        original.backup(saved)
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite checkpoint failed integrity check")
    with closing(open_readonly(isolated_path)) as isolated:
        if digest_nexus(isolated) != plan["nexus_rows_sha256"]:
            raise ValueError("isolated Nexus rows changed after planning")
        entries = media_entries(isolated, isolated_path.parent)
        copied = copy_verified_media(isolated_path.parent, target_path.parent, entries)
        with closing(sqlite3.connect(target_path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            try:
                if digest_rows(target) != plan["baseline_sha256"]:
                    raise ValueError("archive changed after planning")
                if hashlib.sha256(Path(str(plan["config"])).read_bytes()).hexdigest() != plan["config_sha256"]:
                    raise ValueError("configuration changed after planning")
                if digest_source_tree(Path(str(plan["source_root"])))[0] != plan["source_tree_sha256"]:
                    raise ValueError("Nexus XML changed after planning")
                present = target.execute("SELECT count(*) FROM messages WHERE source='nexus'").fetchone()[0]
                if present != plan["already_present"]:
                    raise ValueError("Nexus rows changed after planning")
                added_messages, added_attachments = merge_rows(isolated, target)
                if added_messages != plan["messages_to_add"]:
                    raise ValueError("unexpected Nexus insertion count")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("foreign-key check failed")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
                if digest_rows(target) != plan["baseline_sha256"]:
                    raise ValueError("preexisting archive rows changed")
                target.commit()
            except BaseException:
                target.rollback()
                raise
    return {"messages_added": added_messages, "attachments_added": added_attachments,
            "media_files_copied": copied}


def main() -> None:
    """Write a private plan or apply one with an explicit SQLite checkpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
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
        plan = build_plan(args.source_root, args.isolated_db, args.target_db, args.config)
        descriptor = os.open(args.write_plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, indent=2, sort_keys=True)
            stream.write("\n")
        LOG.info("Nexus merge plan: %s", json.dumps({k: plan[k] for k in (
            "messages_to_add", "attachment_references", "media_files", "media_bytes"
        )}, sort_keys=True))
    else:
        if args.checkpoint is None:
            parser.error("--checkpoint is required with --apply-plan")
        plan = json.loads(args.apply_plan.read_text())
        expected_paths = (str(args.source_root.resolve()), str(args.isolated_db.resolve()),
                          str(args.target_db.resolve()), str(args.config.resolve()))
        if expected_paths != tuple(plan[k] for k in ("source_root", "isolated_db", "target_db", "config")):
            raise ValueError("command paths differ from saved plan")
        LOG.info("Nexus merge result: %s", json.dumps(apply_plan(plan, args.checkpoint), sort_keys=True))


if __name__ == "__main__":
    main()
