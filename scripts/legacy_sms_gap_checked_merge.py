#!/usr/bin/env python3
"""Plan and check a source-specific legacy SMS review merge."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3

from scripts.legacy_mms_import import verify_sources
from scripts.legacy_sms_gap_import import SOURCE, classify, load_records


LOG = logging.getLogger("legacy-sms-gap-checked-merge")


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database without write permission."""
    if not path.is_file():
        raise ValueError("database file is missing")
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def row_digest(db: sqlite3.Connection, review: bool) -> str:
    """Hash the rows on either side of the new review source boundary."""
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
    digest = hashlib.sha256()
    for label, query in queries:
        params = () if label in ("contacts", "identifiers", "exclusions") else (SOURCE,)
        for row in db.execute(query, params):
            digest.update(json.dumps([label, row], ensure_ascii=False,
                                     separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def build_plan(source_root: Path, isolated_path: Path, target_path: Path,
               config_path: Path) -> dict[str, object]:
    """Recheck copied sources and compare isolated rows against the target."""
    source_root = source_root.resolve()
    isolated_path, target_path = isolated_path.resolve(), target_path.resolve()
    config_path = config_path.resolve()
    if isolated_path == target_path:
        raise ValueError("isolated and target databases must differ")
    source_digest = verify_sources(source_root)
    records = load_records(source_root)
    with closing(open_readonly(isolated_path)) as isolated, closing(open_readonly(target_path)) as target:
        baseline = row_digest(target, False)
        if row_digest(isolated, False) != baseline:
            raise ValueError("isolated baseline differs from target")
        selected, reasons = classify(records, target, config_path)
        accepted = {record.identity for record in selected}
        isolated_ids = {identity for (identity,) in isolated.execute(
            "SELECT hash FROM messages WHERE source=?", (SOURCE,)
        )}
        if accepted != isolated_ids:
            raise ValueError("isolated review rows differ from source classification")
        existing = {identity for (identity,) in target.execute(
            "SELECT hash FROM messages WHERE source=?", (SOURCE,)
        )}
        if not existing.issubset(accepted):
            raise ValueError("target contains unplanned legacy SMS review rows")
        if isolated.execute(
            "SELECT 1 FROM attachments a JOIN messages m ON m.id=a.message_id "
            "WHERE m.source=? LIMIT 1", (SOURCE,)
        ).fetchone() is not None:
            raise ValueError("legacy SMS review unexpectedly has attachments")
        review_digest = row_digest(isolated, True)
    return {
        "source_root": str(source_root), "isolated_db": str(isolated_path),
        "target_db": str(target_path), "config": str(config_path),
        "source_sha256": source_digest,
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "baseline_sha256": baseline, "review_rows_sha256": review_digest,
        "accepted_sha256": hashlib.sha256("\n".join(sorted(accepted)).encode()).hexdigest(),
        "accepted_messages": len(accepted), "already_present": len(existing),
        "messages_to_add": len(accepted - existing), "classification": dict(reasons),
    }


def merge_rows(isolated: sqlite3.Connection, target: sqlite3.Connection) -> int:
    """Append review conversations and messages with strict conflict checks."""
    conversation_ids: dict[int, int] = {}
    for source_id, name, handle, group, contact_id in isolated.execute(
        "SELECT id,name,handle,is_group,contact_id FROM conversations "
        "WHERE source=? ORDER BY id", (SOURCE,)
    ):
        if contact_id is not None or group != 0:
            raise ValueError("review source asserted an unverified contact or group")
        target.execute(
            "INSERT INTO conversations(source,name,handle,is_group) VALUES(?,?,?,0) "
            "ON CONFLICT(source,name) DO NOTHING", (SOURCE, name, handle)
        )
        row = target.execute(
            "SELECT id,handle,is_group,contact_id FROM conversations WHERE source=? AND name=?",
            (SOURCE, name)
        ).fetchone()
        if row is None or row[1:] != (handle, 0, None):
            raise ValueError("review conversation changed")
        conversation_ids[source_id] = row[0]
    added = 0
    for identity, source_conv, ts, timestamp, sender, body, system, seq, guid in isolated.execute(
        "SELECT hash,conversation_id,ts,ts_unix,sender,body,is_system,seq,source_guid "
        "FROM messages WHERE source=? ORDER BY id", (SOURCE,)
    ):
        if guid is not None or system != 0 or seq != 0:
            raise ValueError("unexpected legacy SMS review message metadata")
        target_conv = conversation_ids[source_conv]
        expected = (target_conv, SOURCE, ts, timestamp, sender, body, 0, 0, None)
        row = target.execute(
            "SELECT conversation_id,source,ts,ts_unix,sender,body,is_system,seq,source_guid "
            "FROM messages WHERE hash=?", (identity,)
        ).fetchone()
        if row is None:
            target.execute(
                "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,"
                "sender,body,is_system,seq) VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                (identity, target_conv, SOURCE, ts, timestamp, sender, body)
            )
            added += 1
        elif row != expected:
            raise ValueError("review message identity conflict")
    return added


def apply_plan(plan: dict[str, object], checkpoint: Path) -> dict[str, int]:
    """Recheck a saved plan, checkpoint SQLite, and merge atomically."""
    target_path = Path(str(plan["target_db"])).resolve()
    isolated_path = Path(str(plan["isolated_db"])).resolve()
    checkpoint = checkpoint.resolve()
    if checkpoint.exists() or checkpoint == target_path:
        raise ValueError("checkpoint must be new and separate")
    if "/Volumes/KINGSTON/" in str(target_path) or "/Volumes/KINGSTON/" in str(checkpoint):
        raise ValueError("KINGSTON writes are on hold")
    current = build_plan(Path(str(plan["source_root"])), isolated_path,
                         target_path, Path(str(plan["config"])))
    if current != plan:
        raise ValueError("legacy SMS review plan is stale")
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(open_readonly(target_path)) as original, closing(sqlite3.connect(checkpoint)) as saved:
        original.backup(saved)
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite checkpoint failed integrity check")
    with closing(open_readonly(isolated_path)) as isolated:
        if row_digest(isolated, True) != plan["review_rows_sha256"]:
            raise ValueError("isolated review rows changed")
        with closing(sqlite3.connect(target_path)) as target:
            target.execute("PRAGMA foreign_keys=ON")
            target.execute("BEGIN IMMEDIATE")
            try:
                if row_digest(target, False) != plan["baseline_sha256"]:
                    raise ValueError("target baseline changed")
                if verify_sources(Path(str(plan["source_root"]))) != plan["source_sha256"]:
                    raise ValueError("verified SMS source changed")
                config = Path(str(plan["config"]))
                if hashlib.sha256(config.read_bytes()).hexdigest() != plan["config_sha256"]:
                    raise ValueError("owner exclusions changed")
                added = merge_rows(isolated, target)
                if added != plan["messages_to_add"]:
                    raise ValueError("unexpected legacy SMS insertion count")
                if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("foreign-key check failed")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
                if row_digest(target, False) != plan["baseline_sha256"]:
                    raise ValueError("preexisting archive rows changed")
                target.commit()
            except BaseException:
                target.rollback()
                raise
    return {"messages_added": added}


def main() -> None:
    """Write a private plan or apply an already reviewed plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
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
        result = apply_plan(plan, args.checkpoint)
        LOG.info("legacy SMS review merge: %s", json.dumps(result, sort_keys=True))
    else:
        if any(value is None for value in (args.source_root, args.isolated_db,
                                           args.target_db, args.config, args.plan_out)):
            parser.error("source, isolated database, target, config, and plan output are required")
        plan = build_plan(args.source_root, args.isolated_db, args.target_db, args.config)
        if args.plan_out.exists():
            raise ValueError("plan output already exists")
        args.plan_out.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        args.plan_out.chmod(0o600)
        LOG.info("legacy SMS review plan: %s", json.dumps({
            "accepted": plan["accepted_messages"], "to_add": plan["messages_to_add"],
            "classification": plan["classification"],
        }, sort_keys=True))


if __name__ == "__main__":
    main()
