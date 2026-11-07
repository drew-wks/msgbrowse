#!/usr/bin/env python3
"""Audit and import Nexus MMS into an isolated msgbrowse reconstruction copy."""

from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import xml.etree.ElementTree as ET

from scripts.nexus_sms_import import (
    LOCAL_ZONE,
    archive_times,
    blocked_addresses,
    load_sms,
    normalize_address,
    supported_address,
    xml_files,
)


LOG = logging.getLogger("nexus-mms-import")
MEDIA_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "video/3gpp": ".3gp",
    "video/mp4": ".mp4",
    "text/x-vcard": ".vcf",
}


@dataclass(frozen=True)
class Part:
    """One MMS part with stable content hashes and optional staged media."""

    seq: str
    mime: str
    text: str
    digest: str
    original_name: str

    @property
    def signature(self) -> tuple[str, str, str, str]:
        """Return the part identity without unstable exported filenames."""
        return (self.seq, self.mime, self.digest, hashlib.sha256(self.text.encode()).hexdigest())


@dataclass(frozen=True)
class MMS:
    """One source MMS, including all recipient and part observations."""

    date_ms: int
    participants: tuple[str, ...]
    box: int
    sender: str
    subject: str
    parts: tuple[Part, ...]

    @property
    def identity(self) -> str:
        """Return a stable Android identity after participant normalization."""
        fields = [
            self.date_ms,
            self.participants,
            self.box,
            self.subject,
            sorted(part.signature for part in self.parts),
        ]
        payload = b"nexus-mms\x00" + json.dumps(fields, ensure_ascii=False).encode()
        return hashlib.sha256(payload).hexdigest()

    @property
    def body(self) -> str:
        """Retain the subject and user-visible text parts in source order."""
        text = [part.text for part in self.parts if part.mime == "text/plain" and part.text]
        return "\n".join(([self.subject] if self.subject else []) + text)


def parse_mms(element: ET.Element) -> MMS:
    """Parse and validate one SMS Backup & Restore MMS XML element.

    Args:
        element: Complete ``mms`` element including addrs and parts.

    Returns:
        Normalized MMS observation.

    Raises:
        ValueError: Required sender, date, participant, or part data is invalid.
    """
    date = element.get("date", "")
    box = element.get("msg_box", "")
    if not date.isdigit() or box not in ("1", "2"):
        raise ValueError("MMS has invalid date or message box")
    participants = tuple(sorted(normalize_address(x) for x in element.get("address", "").split("~") if x))
    if not participants:
        raise ValueError("MMS has no participants")
    senders = [normalize_address(x.get("address", "")) for x in element.iter("addr") if x.get("type") == "137"]
    if len(senders) != 1 or not senders[0]:
        raise ValueError("MMS has no unique sender address")
    if (box == "1") != (senders[0] in participants):
        raise ValueError("MMS sender and message box disagree")
    parts: list[Part] = []
    for element_part in element.iter("part"):
        mime = element_part.get("ct", "").lower()
        encoded = element_part.get("data", "")
        if encoded and mime not in MEDIA_EXT:
            raise ValueError("MMS has unsupported inline media type")
        payload = base64.b64decode(encoded, validate=True) if encoded else b""
        original = element_part.get("fn") or element_part.get("name") or ""
        parts.append(
            Part(
                seq=element_part.get("seq", ""),
                mime=mime,
                text=element_part.get("text", ""),
                digest=hashlib.sha256(payload).hexdigest() if encoded else "",
                original_name=Path(original).name,
            )
        )
    subject = element.get("sub", "")
    if subject.casefold() == "null":
        subject = ""
    return MMS(int(date), participants, int(box), senders[0], subject, tuple(parts))


def load_mms(root: Path) -> tuple[list[MMS], int, set[str]]:
    """Stream snapshots and deduplicate MMS by participants and part content.

    Args:
        root: Copied Nexus XML directory or a single XML file.

    Returns:
        Distinct MMS, observation count, and outgoing sender handles.
    """
    unique: dict[str, MMS] = {}
    observations = 0
    owner_handles: set[str] = set()
    for path in xml_files(root):
        parser = ET.iterparse(path, events=("start", "end"))
        _, document = next(parser)
        if document.tag != "smses":
            raise ValueError(f"unexpected XML root in {path.name}")
        for event, element in parser:
            if event != "end" or element.tag != "mms":
                continue
            record = parse_mms(element)
            unique[record.identity] = record
            observations += 1
            if record.box == 2:
                owner_handles.add(record.sender)
            element.clear()
        document.clear()
    return list(unique.values()), observations, owner_handles


def wall_time(record: MMS) -> tuple[str, int]:
    """Map the source timestamp to msgbrowse's local wall-clock key."""
    local = datetime.fromtimestamp(record.date_ms / 1000, LOCAL_ZONE)
    return local.strftime("%Y-%m-%d %H:%M:%S"), int(local.replace(tzinfo=timezone.utc).timestamp())


def classify(
    records: list[MMS], owner_handles: set[str], blocked: set[str],
    optout: set[str], times: dict[tuple[str, bool], set[int]],
) -> tuple[list[MMS], Counter[str], dict[str, str]]:
    """Apply conservative MMS admission rules and retain held identities."""
    selected: list[MMS] = []
    reasons: Counter[str] = Counter()
    held: dict[str, str] = {}
    near_groups: dict[tuple[tuple[str, ...], int, str, tuple[tuple[str, str, str, str], ...]], list[MMS]] = defaultdict(list)
    for record in records:
        parts = tuple(sorted(part.signature for part in record.parts))
        near_groups[(record.participants, record.box, record.subject, parts)].append(record)
    near_source: set[str] = set()
    for group in near_groups.values():
        ordered = sorted(group, key=lambda record: record.date_ms)
        for left, right in zip(ordered, ordered[1:]):
            if 0 < right.date_ms - left.date_ms <= 300_000:
                near_source.update((left.identity, right.identity))
    for record in records:
        others = tuple(x for x in record.participants if x not in owner_handles)
        reason = ""
        if len(others) == 1 and others[0].isdigit() and 4 <= len(others[0]) <= 6:
            reason = "short_code"
        elif not others or any(not supported_address(x) for x in others):
            reason = "unverified_participants"
        elif len(others) == 1 and others[0] in blocked:
            reason = "owner_blocked"
        elif len(others) == 1 and others[0] in optout:
            reason = "owner_optout_thread"
        elif record.box == 1 and "appointment reminder" in record.body.casefold():
            reason = "appointment_reminder"
        elif record.identity in near_source:
            reason = "near_source_duplicate_candidate"
        elif not record.body and not any(part.digest for part in record.parts):
            reason = "empty_mms"
        elif record.body:
            _, timestamp = wall_time(record)
            if any(abs(t - timestamp) <= 86400 for t in times.get((record.body, record.box == 2), set())):
                reason = "possible_apple_overlap"
        if reason:
            reasons[reason] += 1
            held[record.identity] = reason
        else:
            selected.append(record)
            reasons["provisional_new"] += 1
    return selected, reasons, held


def staged_media(staging: Path) -> dict[str, Path]:
    """Validate the private decoded-asset manifest and return SHA paths."""
    manifest = json.loads((staging / "manifest.json").read_text())
    result: dict[str, Path] = {}
    for item in manifest["assets"]:
        digest = item["sha256"]
        path = staging / item["file"]
        if path.stat().st_size != item["bytes"]:
            raise ValueError("staged MMS asset size changed")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("staged MMS asset checksum changed")
        result[digest] = path
    return result


def append_isolated(
    db: sqlite3.Connection, records: list[MMS], held: dict[str, str],
    owner_handles: set[str], data_dir: Path, media: dict[str, Path],
) -> tuple[int, int]:
    """Append MMS and independently copied parts to an isolated archive.

    Args:
        db: Isolated SQLite connection.
        records: Provisional MMS observations.
        held: Source identities excluded or held by the full catalog.
        owner_handles: Outgoing source sender handles.
        data_dir: Isolated archive folder containing the database.
        media: Verified staged media by SHA-256.

    Returns:
        Newly inserted message and attachment counts.
    """
    media_root = data_dir / "media" / "nexus"
    media_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    added_messages = 0
    added_attachments = 0
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS nexus_mms_holds (identity TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        for identity in held:
            if db.execute("SELECT 1 FROM messages WHERE hash=? AND source='nexus'", (identity,)).fetchone():
                raise ValueError("previously imported Nexus MMS is now held for review")
        db.executemany("INSERT OR IGNORE INTO nexus_mms_holds(identity,reason) VALUES(?,?)", held.items())
        for record in records:
            if db.execute("SELECT 1 FROM nexus_mms_holds WHERE identity=?", (record.identity,)).fetchone():
                continue
            participants = tuple(x for x in record.participants if x not in owner_handles)
            name = ", ".join(participants)
            db.execute(
                "INSERT INTO conversations(source,name,handle,is_group) VALUES('nexus',?,?,?) "
                "ON CONFLICT(source,name) DO NOTHING",
                (name, participants[0] if len(participants) == 1 else "", int(len(participants) > 1)),
            )
            conv_id = db.execute("SELECT id FROM conversations WHERE source='nexus' AND name=?", (name,)).fetchone()[0]
            display, timestamp = wall_time(record)
            existing = db.execute("SELECT id,source FROM messages WHERE hash=?", (record.identity,)).fetchone()
            if existing:
                if existing[1] != "nexus":
                    raise ValueError("MMS identity hash belongs to another source")
                message_id = existing[0]
            else:
                cursor = db.execute(
                    "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,sender,body,is_system,seq) "
                    "VALUES(?,?,'nexus',NULL,?,?,?,?,0,0)",
                    (record.identity, conv_id, display, timestamp,
                     "Me" if record.box == 2 else record.sender, record.body),
                )
                message_id = cursor.lastrowid
                added_messages += 1
            for part in record.parts:
                if not part.digest:
                    continue
                source_path = media.get(part.digest)
                if source_path is None:
                    raise ValueError("MMS part missing from verified staging")
                extension = MEDIA_EXT[part.mime]
                filename = part.digest + extension
                target = media_root / filename
                if not target.exists():
                    shutil.copyfile(source_path, target)
                    target.chmod(0o600)
                with target.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != part.digest:
                        raise ValueError("isolated MMS media checksum mismatch")
                rel_path = "permanent/" + filename
                kind = "image" if part.mime.startswith("image/") else ("video" if part.mime.startswith("video/") else "file")
                original_name = part.original_name or filename
                cursor = db.execute(
                    "INSERT INTO attachments(message_id,conversation_id,ts_unix,kind,rel_path,original_name) "
                    "SELECT ?,?,?,?,?,? WHERE NOT EXISTS "
                    "(SELECT 1 FROM attachments WHERE message_id=? AND rel_path=? AND kind=?)",
                    (message_id, conv_id, timestamp, kind, rel_path, original_name,
                     message_id, rel_path, kind),
                )
                added_attachments += cursor.rowcount
    return added_messages, added_attachments


def run(source_root: Path, catalog_root: Path, db_path: Path, config_path: Path,
        staging: Path, apply: bool) -> dict[str, int]:
    """Audit MMS and optionally import them into a reconstruction-only DB."""
    if apply and not any(part.startswith("reconstruction") for part in db_path.resolve().parts):
        raise ValueError("writes require an isolated reconstruction database")
    catalog, observations, owner_handles = load_mms(catalog_root)
    selected_ids = {record.identity for record in load_mms(source_root)[0]}
    optout = {
        normalize_address(record.address)
        for record in load_sms(catalog_root)[0]
        if record.kind == 2 and record.body.strip().casefold() in ("stop", "quit")
    }
    uri = f"file:{db_path.resolve()}?mode={'rw' if apply else 'ro'}"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        selected, reasons, held = classify(catalog, owner_handles, blocked_addresses(config_path), optout, archive_times(db))
        selected = [record for record in selected if record.identity in selected_ids]
        media = staged_media(staging) if apply else {}
        messages, attachments = append_isolated(db, selected, held, owner_handles, db_path.parent, media) if apply else (0, 0)
    result = dict(reasons)
    result.update(observations=observations, distinct_mms=len(catalog), added_messages=messages, added_attachments=attachments)
    return result


def main() -> None:
    """Parse CLI options and log aggregate MMS results without contents."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--catalog-root", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    LOG.info("nexus MMS audit: %s", json.dumps(run(args.source_root, args.catalog_root,
        args.db, args.config, args.staging, args.apply), sort_keys=True))


if __name__ == "__main__":
    main()
