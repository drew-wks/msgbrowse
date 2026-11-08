#!/usr/bin/env python3
"""Stage and checked-merge owner-eligible .ichat GUIDs into a review thread.

The review conversation is intentionally synthetic. It retains the Apple GUID
without asserting a historical chat, so later exporter imports cannot create
a second message card for the same GUID.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

from scripts.nexus_sms_import import blocked_addresses, normalize_address


LOG = logging.getLogger("ichat-review-import")
REVIEW_NAME = "iChat review (unassigned)"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
LOCAL_ZONE = ZoneInfo("America/Toronto")
SHORT_CODE = re.compile(r"[0-9]{5,6}")
GUID = re.compile(r"(?:at_[0-9]+_)?[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}",
                  re.IGNORECASE)
SWIFT_HELPER = Path(__file__).with_name("ichat_extract_wrapper.swift")
CHECKED_TABLES = (
    "conversations", "messages", "attachments", "links", "reactions",
    "contacts", "contact_identifiers", "imessage_source_assets",
    "imessage_attachment_observations", "imessage_excluded_messages",
)


@dataclass(frozen=True)
class IChatRecord:
    """One source GUID with exact text and optional recovered image bytes."""

    guid: str
    timestamp: int
    sender: str
    body: str
    flag: int
    account: str
    service: str
    participants: tuple[str, ...]
    transfer_guid: str | None
    image: bytes | None
    image_mime: str | None

    @property
    def hash(self) -> str:
        """Return a stable Apple-GUID identity independent of source filenames."""
        return hashlib.sha256(("imessage-guid\0" + self.guid).encode()).hexdigest()

    @property
    def display_time(self) -> str:
        """Return the archive's local-wall-time string."""
        return datetime.fromtimestamp(self.timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def value(objects: list[Any], item: Any) -> Any:
    """Dereference one NSKeyedArchiver UID."""
    return objects[item.data] if isinstance(item, plistlib.UID) else item


def string(objects: list[Any], item: Any) -> str:
    """Decode a keyed NSString without coercing other keyed objects to text."""
    item = value(objects, item)
    while isinstance(item, dict) and "NS.string" in item:
        item = value(objects, item["NS.string"])
    if not isinstance(item, str):
        raise ValueError("expected keyed string")
    return item


def decode_wrapper(serialized: bytes) -> tuple[bytes, str]:
    """Extract and identify a regular image from an Apple NSFileWrapper."""
    if shutil.which("swift") is None:
        raise ValueError("Swift is required to decode embedded iChat images")
    with tempfile.TemporaryDirectory(prefix="msgbrowse-ichat-") as temporary:
        source = Path(temporary) / "wrapper.bin"
        output = Path(temporary) / "image.bin"
        source.write_bytes(serialized)
        source.chmod(0o600)
        result = subprocess.run(
            ["swift", str(SWIFT_HELPER), str(source), str(output)],
            capture_output=True, text=True, check=False, timeout=120,
        )
        if result.returncode != 0 or not output.is_file():
            raise ValueError("embedded iChat file wrapper did not decode")
        payload = output.read_bytes()
    if payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9"):
        return payload, "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in payload[-32:]:
        return payload, "image/png"
    raise ValueError("embedded iChat payload is not a verified JPEG or PNG")


def source_digest(roots: list[Path]) -> str:
    """Hash the complete copied source set, including paths and file bytes."""
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted(root.rglob("*.ichat")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("iChat source is not a regular file")
            digest.update(str(root).encode() + b"\0")
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def row_digest(db: sqlite3.Connection, limits: dict[str, list[int]] | None = None) -> str:
    """Hash the archive baseline without printing private row contents."""
    digest = hashlib.sha256()
    for table in CHECKED_TABLES:
        bound = "" if limits is None else f" WHERE id<={int(limits[table][1])}"
        for row in db.execute(f"SELECT * FROM {table}{bound} ORDER BY id"):
            digest.update(repr((table, row)).encode("utf-8", "surrogatepass"))
            digest.update(b"\n")
    return digest.hexdigest()


def load_records(roots: list[Path], candidate_guids: set[str],
                 control_guids: set[str] | None = None) -> dict[str, list[IChatRecord]]:
    """Decode only validated candidate GUIDs from immutable copied archives."""
    found: dict[str, list[IChatRecord]] = {}
    control_accounts: set[tuple[str, str]] = set()
    file_count = 0
    for root in roots:
        for path in sorted(root.rglob("*.ichat")):
            file_count += 1
            archive = plistlib.loads(path.read_bytes())
            objects = archive["$objects"]
            metadata = value(objects, archive["$top"]["metadata"])
            if not isinstance(metadata, dict):
                raise ValueError("iChat metadata is missing")
            keys = value(objects, metadata["NS.keys"])
            values = value(objects, metadata["NS.objects"])
            if not isinstance(keys, list) or not isinstance(values, list):
                raise ValueError("iChat metadata is malformed")
            info = {string(objects, k): value(objects, v) for k, v in zip(keys, values)}
            roster = info.get("PresentityIDs")
            participant_items = value(objects, roster["NS.objects"]) if isinstance(roster, dict) else []
            participants = tuple(sorted(string(objects, x) for x in participant_items))
            for item in objects:
                if not isinstance(item, dict) or "GUID" not in item or "Flags" not in item:
                    continue
                guid = string(objects, item["GUID"]).upper()
                if control_guids is not None and guid in control_guids:
                    control_sender = value(objects, item["Sender"])
                    if isinstance(control_sender, dict):
                        control_accounts.add((string(objects, control_sender["AccountID"]),
                                              string(objects, control_sender["ServiceName"])))
                if guid not in candidate_guids:
                    continue
                sender = value(objects, item["Sender"])
                text = value(objects, item["MessageText"])
                date = value(objects, item["Time"])
                if not isinstance(sender, dict) or not isinstance(text, dict) or not isinstance(date, dict):
                    raise ValueError("iChat candidate fields are malformed")
                raw_date = APPLE_EPOCH + timedelta(seconds=float(date["NS.time"]))
                local = raw_date.astimezone(LOCAL_ZONE)
                timestamp = int(local.replace(tzinfo=timezone.utc).timestamp())
                attrs = value(objects, text["NSAttributes"])
                attr_values: dict[str, Any] = {}
                if isinstance(attrs, dict) and "NS.keys" in attrs and "NS.objects" in attrs:
                    attr_keys = value(objects, attrs["NS.keys"])
                    attr_objects = value(objects, attrs["NS.objects"])
                    attr_values = {string(objects, k): value(objects, v)
                                   for k, v in zip(attr_keys, attr_objects)}
                transfer = attr_values.get("__kIMFileTransferGUIDAttributeName")
                transfer_guid = transfer if isinstance(transfer, str) else None
                if transfer_guid is not None and not GUID.fullmatch(transfer_guid):
                    raise ValueError("invalid iChat file-transfer GUID")
                image = mime = None
                attachment = attr_values.get("NSAttachment")
                if isinstance(attachment, dict):
                    wrapper = value(objects, attachment["NSFileWrapper"])
                    wrapped = value(objects, wrapper["NSFileWrapperData"])
                    serialized = value(objects, wrapped["NS.data"])
                    if not isinstance(serialized, bytes):
                        raise ValueError("embedded iChat attachment is not bytes")
                    image, mime = decode_wrapper(serialized)
                record = IChatRecord(
                    guid, timestamp, string(objects, sender["ID"]),
                    string(objects, text["NSString"]), int(item["Flags"]),
                    string(objects, sender["AccountID"]),
                    string(objects, sender["ServiceName"]), participants,
                    transfer_guid, image, mime,
                )
                found.setdefault(guid, []).append(record)
    if control_guids is not None and file_count != 1849:
        raise ValueError("verified iChat source file count changed")
    if set(found) != candidate_guids:
        raise ValueError("validated iChat GUID set differs from source files")
    if control_guids is not None and any(
        (record.account, record.service) not in control_accounts
        for variants in found.values() for record in variants
    ):
        raise ValueError("iChat candidate account has no archived GUID control")
    return found


def select_records(records: dict[str, list[IChatRecord]], validation_dir: Path,
                   db: sqlite3.Connection, config: Path) -> tuple[list[IChatRecord], list[IChatRecord]]:
    """Apply pinned validation, owner exclusions, and existing GUID checks."""
    metadata = json.loads((validation_dir / "ichat-validation-metadata.json").read_text())
    exclusions = json.loads((validation_dir / "ichat-exclusion-validation.json").read_text())
    expected = {item["guid"]: item for item in metadata["candidates"]}
    excluded = {item["guid"]: item for item in exclusions["entries"]}
    if set(records) != set(expected) or set(records) != set(excluded) or len(records) != 237:
        raise ValueError("iChat validation roster changed")
    blocked = {normalize_address(x) for x in blocked_addresses(config)}
    optout = {normalize_address(row[0] or "") for row in db.execute(
        "SELECT DISTINCT c.handle FROM messages m JOIN conversations c "
        "ON c.id=m.conversation_id WHERE m.sender='Me' "
        "AND lower(trim(m.body)) IN ('stop','quit')"
    )}
    archived = dict(db.execute(
        "SELECT source_guid,c.name FROM messages m JOIN conversations c "
        "ON c.id=m.conversation_id WHERE m.source='imessage' AND source_guid IS NOT NULL"
    ))
    ledger = {row[0] for row in db.execute(
        "SELECT source_guid FROM imessage_excluded_messages WHERE source_guid IS NOT NULL"
    )}
    flag_controls = {item["flag"]: item["outgoing"]
                     for item in metadata["flag_direction_controls"]}
    accepted: list[IChatRecord] = []
    short_codes: list[IChatRecord] = []
    for guid in sorted(records):
        variants = records[guid]
        pin = expected[guid]
        if (sorted({hashlib.sha256(v.body.encode()).hexdigest() for v in variants}) != pin["body_sha256"]
                or sorted({v.timestamp for v in variants}) != pin["timestamp_values"]
                or sorted({v.flag for v in variants}) != pin["flag_values"]
                or sorted({hashlib.sha256(v.sender.encode()).hexdigest() for v in variants}) != pin["sender_sha256"]):
            raise ValueError("iChat candidate differs from validated source")
        if len({(v.body, v.sender, v.flag, v.account, v.service,
                 v.transfer_guid, v.image) for v in variants}) != 1:
            raise ValueError("same iChat GUID has conflicting source variants")
        record = min(variants, key=lambda v: v.timestamp)
        record = replace(record, participants=tuple(sorted({person for variant in variants
                                                            for person in variant.participants})))
        if guid in archived and archived[guid] != REVIEW_NAME:
            raise ValueError("validated iChat candidate already has another disposition")
        participants = {normalize_address(x) for x in record.participants}
        if participants & blocked or participants & optout:
            raise ValueError("iChat candidate now matches an owner exclusion")
        if ("appointment reminder" in record.body.casefold() or
                record.body.strip().casefold() in ("stop", "quit")):
            raise ValueError("iChat candidate now matches a content exclusion")
        short = bool(SHORT_CODE.fullmatch(record.sender))
        if short != excluded[guid]["short_sender"]:
            raise ValueError("iChat short-code classification changed")
        if short:
            if flag_controls.get(record.flag) is not False:
                raise ValueError("short-code iChat direction is unverified")
            if guid in archived:
                raise ValueError("excluded short-code GUID is present as a message")
            short_codes.append(record)
        else:
            if guid in ledger:
                raise ValueError("accepted iChat GUID is in the exclusion ledger")
            accepted.append(record)
    if len(accepted) != 118 or len(short_codes) != 119:
        raise ValueError("validated iChat candidate counts changed")
    return accepted, short_codes


def media_path(root: Path, record: IChatRecord) -> tuple[str, Path] | None:
    """Return the immutable content-addressed image location, if present."""
    if record.image is None or record.image_mime is None:
        return None
    digest = hashlib.sha256(record.image).hexdigest()
    extension = ".jpg" if record.image_mime == "image/jpeg" else ".png"
    rel = f"permanent/{digest[:2]}/{digest}{extension}"
    return rel, root / "media" / "imessage" / rel.removeprefix("permanent/")


def retain_images(root: Path, records: list[IChatRecord]) -> int:
    """Write source-backed image bytes without replacing an existing file."""
    copied = 0
    for record in records:
        location = media_path(root, record)
        if location is None:
            continue
        _, path = location
        if path.is_symlink():
            raise ValueError("permanent iChat media path is a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            with tempfile.NamedTemporaryFile(prefix=".ichat-", dir=path.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(record.image or b"")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                try:
                    os.link(temporary, path)
                    copied += 1
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        if path.read_bytes() != record.image:
            raise ValueError("permanent iChat media content conflict")
    return copied


def append_records(db: sqlite3.Connection, records: list[IChatRecord],
                   short_codes: list[IChatRecord], data_dir: Path,
                   flag_controls: dict[int, bool]) -> dict[str, int]:
    """Append review messages, observed media, and short-code GUID tombstones."""
    db.execute("INSERT INTO conversations(source,name,handle,is_group) "
               "VALUES('imessage',?,'',0) ON CONFLICT(source,name) DO NOTHING", (REVIEW_NAME,))
    conv = db.execute("SELECT id,handle,is_group FROM conversations "
                      "WHERE source='imessage' AND name=?", (REVIEW_NAME,)).fetchone()
    if conv is None or conv[1:] != ("", 0):
        raise ValueError("iChat review conversation conflict")
    added = observations = media_refs = ledger_added = 0
    for record in records:
        direction = flag_controls.get(record.flag)
        sender = "Me" if direction is True else record.sender if direction is False else "Direction unverified"
        expected = (conv[0], record.display_time, record.timestamp, sender, record.body)
        existing = db.execute(
            "SELECT id,conversation_id,ts,ts_unix,sender,body FROM messages "
            "WHERE source='imessage' AND source_guid=?", (record.guid,)
        ).fetchone()
        if existing is None:
            db.execute(
                "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,"
                "sender,body,is_system,seq) VALUES(?,?,'imessage',?,?,?,?,?,0,0)",
                (record.hash, conv[0], record.guid, *expected[1:])
            )
            message_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            added += 1
        elif existing[1:] == expected:
            message_id = existing[0]
        else:
            raise ValueError("iChat review GUID conflicts with existing message")
        if record.transfer_guid:
            cursor = db.execute(
                "INSERT INTO imessage_attachment_observations(message_id,source_attachment_guid,hidden,mime_type) "
                "VALUES(?,?,0,?) ON CONFLICT(message_id,source_attachment_guid,hidden,mime_type) DO NOTHING",
                (message_id, record.transfer_guid, record.image_mime or "")
            )
            observations += cursor.rowcount
        location = media_path(data_dir, record)
        if location is not None:
            rel, _ = location
            kind = "image"
            label = hashlib.sha256(record.image or b"").hexdigest() + (
                ".jpg" if record.image_mime == "image/jpeg" else ".png")
            cursor = db.execute(
                "INSERT INTO attachments(message_id,conversation_id,ts_unix,kind,rel_path,original_name) "
                "SELECT ?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM attachments "
                "WHERE message_id=? AND rel_path=? AND kind=?)",
                (message_id, conv[0], record.timestamp, kind, rel, label,
                 message_id, rel, kind)
            )
            media_refs += cursor.rowcount
            if record.transfer_guid:
                db.execute(
                    "INSERT INTO imessage_source_assets(message_id,source_attachment_guid,rel_path,hidden,kind,original_name) "
                    "VALUES(?,?,?,0,?,?) ON CONFLICT(message_id,source_attachment_guid,rel_path) DO NOTHING",
                    (message_id, record.transfer_guid, rel, kind, label)
                )
    for record in short_codes:
        cursor = db.execute(
            "INSERT INTO imessage_excluded_messages(source_guid,message_hash,reason,excluded_at) "
            "VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
            (record.guid, record.hash, "short_code", datetime.now(timezone.utc).isoformat())
        )
        ledger_added += cursor.rowcount
        row = db.execute("SELECT message_hash,reason FROM imessage_excluded_messages "
                         "WHERE source_guid=?", (record.guid,)).fetchone()
        if row != (record.hash, "short_code"):
            raise ValueError("iChat short-code ledger conflict")
    return {"messages_added": added, "media_references_added": media_refs,
            "attachment_observations_added": observations, "short_code_guids_recorded": ledger_added}


def build_plan(roots: list[Path], validation_dir: Path, config: Path,
               db_path: Path, data_dir: Path) -> tuple[dict[str, Any], list[IChatRecord],
                                                    list[IChatRecord], dict[int, bool]]:
    """Recheck the source and produce a metadata-only merge plan."""
    roots = [root.resolve() for root in roots]
    with closing(sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)) as db:
        metadata = json.loads((validation_dir / "ichat-validation-metadata.json").read_text())
        candidates = {item["guid"] for item in metadata["candidates"]}
        controls = {guid for (guid,) in db.execute(
            "SELECT m.source_guid FROM messages m JOIN conversations c "
            "ON c.id=m.conversation_id WHERE m.source='imessage' "
            "AND m.source_guid IS NOT NULL AND c.name!=?", (REVIEW_NAME,)
        )}
        found = load_records(roots, candidates, controls)
        accepted, short_codes = select_records(found, validation_dir, db, config)
        flags = {item["flag"]: item["outgoing"] for item in metadata["flag_direction_controls"]}
        existing = sum(db.execute("SELECT count(*) FROM messages WHERE source='imessage' "
                                  "AND source_guid=? AND conversation_id IN "
                                  "(SELECT id FROM conversations WHERE name=?)",
                                  (record.guid, REVIEW_NAME)).fetchone()[0] for record in accepted)
        ledger_existing = sum(db.execute("SELECT count(*) FROM imessage_excluded_messages "
                                         "WHERE source_guid=?", (record.guid,)).fetchone()[0]
                              for record in short_codes)
        baseline = {table: list(db.execute(f"SELECT count(*),coalesce(max(id),0) FROM {table}").fetchone())
                    for table in CHECKED_TABLES}
        baseline_digest = row_digest(db)
    images = sorted([record.guid, hashlib.sha256(record.image).hexdigest(), len(record.image)]
                    for record in accepted if record.image is not None)
    plan: dict[str, Any] = {
        "schema_version": 1, "roots": [str(x) for x in roots],
        "validation_dir": str(validation_dir.resolve()), "config": str(config.resolve()),
        "db": str(db_path.resolve()), "data_dir": str(data_dir.resolve()),
        "source_digest": source_digest(roots),
        "validation_digest": hashlib.sha256(b"".join(
            (validation_dir / name).read_bytes() for name in
            ("ichat-validation-metadata.json", "ichat-exclusion-validation.json")
        )).hexdigest(),
        "config_digest": hashlib.sha256(config.read_bytes()).hexdigest(),
        "baseline": baseline, "baseline_digest": baseline_digest,
        "accepted_guids": sorted(x.guid for x in accepted),
        "short_code_guids": sorted(x.guid for x in short_codes),
        "already_present": existing, "ledger_existing": ledger_existing,
        "embedded_images": images,
    }
    return plan, accepted, short_codes, flags


def apply_plan(plan: dict[str, Any], checkpoint: Path | None) -> dict[str, int]:
    """Recheck the plan and append within a transaction after a SQLite checkpoint."""
    db_path = Path(str(plan["db"]))
    data_dir = Path(str(plan["data_dir"]))
    if str(db_path).startswith("/Volumes/KINGSTON/") or str(data_dir).startswith("/Volumes/KINGSTON/"):
        raise ValueError("KINGSTON writes are on hold")
    current, records, short_codes, flags = build_plan(
        [Path(x) for x in plan["roots"]], Path(str(plan["validation_dir"])),
        Path(str(plan["config"])), db_path, data_dir,
    )
    if current != plan:
        raise ValueError("iChat review plan is stale")
    if checkpoint is not None:
        if checkpoint.exists() or checkpoint.resolve() == db_path.resolve():
            raise ValueError("SQLite checkpoint path must be new")
        if str(checkpoint.resolve()).startswith("/Volumes/KINGSTON/"):
            raise ValueError("KINGSTON writes are on hold")
        checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)) as source, \
             closing(sqlite3.connect(checkpoint)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite checkpoint failed integrity check")
        checkpoint.chmod(0o600)
    elif "reconstruction" not in db_path.parts:
        raise ValueError("configured archive merge requires a SQLite checkpoint")
    copied = retain_images(data_dir, records)
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            now, _, _, _ = build_plan(
                [Path(x) for x in plan["roots"]], Path(str(plan["validation_dir"])),
                Path(str(plan["config"])), db_path, data_dir,
            )
            if now != plan:
                raise ValueError("iChat review plan changed before transaction")
            result = append_records(db, records, short_codes, data_dir, flags)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("iChat review foreign-key check failed")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("iChat review SQLite integrity check failed")
            if row_digest(db, plan["baseline"]) != plan["baseline_digest"]:
                raise ValueError("preexisting archive rows changed during iChat merge")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    result["media_files_copied"] = copied
    return result


def main() -> None:
    """Build or apply a private metadata-only iChat review plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", action="append", type=Path)
    parser.add_argument("--validation-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--apply-plan", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.apply_plan:
        plan = json.loads(args.apply_plan.read_text())
        result = apply_plan(plan, args.checkpoint)
        LOG.info("iChat review merge: %s", json.dumps(result, sort_keys=True))
        return
    if not (args.source_root and args.validation_dir and args.config and args.db
            and args.data_dir and args.plan_out):
        parser.error("source roots, validation, config, database, data dir, and plan are required")
    plan, _, _, _ = build_plan(args.source_root, args.validation_dir,
                               args.config, args.db, args.data_dir)
    if args.plan_out.exists():
        raise ValueError("iChat review plan output already exists")
    args.plan_out.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    args.plan_out.chmod(0o600)
    LOG.info("iChat review plan: %s", json.dumps({
        "accepted": len(plan["accepted_guids"]),
        "to_add": len(plan["accepted_guids"]) - int(plan["already_present"]),
        "short_code_guids": len(plan["short_code_guids"]),
        "embedded_images": len(plan["embedded_images"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
