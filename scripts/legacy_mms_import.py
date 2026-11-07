#!/usr/bin/env python3
"""Classify and append verified legacy iPhone MMS to an isolated archive.

Only parents with previously uncataloged payload bytes are considered in this
pass. The copied sms.db files and content-addressed staging are read-only.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3

from scripts.nexus_sms_import import LOCAL_ZONE, blocked_addresses, normalize_address


LOG = logging.getLogger("legacy-mms-import")
SOURCE = "legacy_mms"
EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "video/3gpp": ".3gp",
    "text/x-vcard": ".vcf", "text/vcard": ".vcf", "text/x-vlocation": ".txt",
}


@dataclass(frozen=True)
class Asset:
    """One independently hashed payload observation."""

    part_id: int
    origin: str
    digest: str
    mime: str
    size: int
    preview_part: int
    already_cataloged: bool


@dataclass(frozen=True)
class Parent:
    """One source MMS reconstructed across overlapping backup snapshots."""

    date: int
    flags: int
    handle: str
    subject: str
    body: str
    assets: tuple[Asset, ...]

    @property
    def identity(self) -> str:
        """Return a source-specific identity independent of snapshot row IDs."""
        fields = [self.date, self.flags, self.handle, self.subject, self.body,
                  sorted((a.part_id, a.origin, a.digest) for a in self.assets)]
        return hashlib.sha256(b"legacy-mms\0" + json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def verify_sources(source_root: Path) -> str:
    """Verify every copied source file against its owner-checked manifest."""
    manifest_path = source_root / "verified-copy-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256()
    for item in manifest["files"]:
        relative = Path(item["file"])
        path = source_root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"]:
            raise ValueError("copied legacy source file is missing or changed")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != item["sha256"]:
            raise ValueError("copied legacy source checksum changed")
        digest.update(str(relative).encode() + b"\0" + bytes.fromhex(actual))
    return digest.hexdigest()


def load_parents(source_root: Path, catalog_path: Path) -> tuple[list[Parent], int]:
    """Join catalog payloads to verified sms.db parents without exposing text."""
    catalog = json.loads(catalog_path.read_text())
    if catalog["schema_version"] != 1:
        raise ValueError("unsupported legacy payload catalog")
    grouped: dict[tuple[str, int], list[Asset]] = defaultdict(list)
    uncataloged: set[tuple[str, int]] = set()
    for item in catalog["parts"]:
        key = (item["snapshot"], item["source_parent_id"])
        mime = item["detected_type"]
        if mime not in EXTENSIONS or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("unsupported or invalid catalog payload")
        grouped[key].append(Asset(item["part_id"], item["origin"], item["sha256"],
                                  mime, item["bytes"], item["preview_part"],
                                  item["already_cataloged"]))
        if not item["already_cataloged"]:
            uncataloged.add(key)
    observations: dict[tuple[int, int, str], list[Parent]] = defaultdict(list)
    snapshots = sorted({snapshot for snapshot, _ in grouped})
    for snapshot in snapshots:
        path = source_root / snapshot / "sms.db"
        with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("copied legacy sms.db failed integrity check")
            for (name, parent_id), assets in grouped.items():
                if name != snapshot:
                    continue
                row = db.execute(
                    "SELECT address,date,flags,group_id,coalesce(subject,'') "
                    "FROM message WHERE rowid=?", (parent_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("catalog parent missing from copied sms.db")
                address, date, flags, group_id, subject = row
                members = [value for (value,) in db.execute(
                    "SELECT DISTINCT address FROM group_member WHERE group_id=?", (group_id,)
                )]
                if len(members) != 1:
                    if (snapshot, parent_id) in uncataloged:
                        raise ValueError("candidate legacy MMS group has ambiguous participants")
                    continue
                handle = normalize_address(address or members[0] or "")
                if address and normalize_address(address) != normalize_address(members[0]):
                    raise ValueError("legacy MMS address and group member disagree")
                text_parts = [bytes(value).decode("utf-8") for (value,) in db.execute(
                    "SELECT data FROM msg_pieces WHERE message_id=? "
                    "AND content_type='text/plain' AND data IS NOT NULL "
                    "ORDER BY part_id,rowid", (parent_id,)
                )]
                parent = Parent(date, flags, handle, subject, "\n".join(text_parts),
                                tuple(sorted(assets, key=lambda a: (a.part_id, a.origin, a.digest))))
                observations[(date, flags, handle)].append(parent)
    selected_keys = set()
    for snapshot, parent_id in uncataloged:
        path = source_root / snapshot / "sms.db"
        with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as db:
            address, date, flags, group_id = db.execute(
                "SELECT address,date,flags,group_id FROM message WHERE rowid=?", (parent_id,)
            ).fetchone()
            member = db.execute("SELECT address FROM group_member WHERE group_id=?", (group_id,)).fetchone()[0]
            selected_keys.add((date, flags, normalize_address(address or member or "")))
    parents: list[Parent] = []
    for key in sorted(selected_keys):
        variants = observations[key]
        if len({parent.identity for parent in variants}) != 1:
            raise ValueError("overlapping legacy snapshots disagree on MMS content")
        parents.append(variants[0])
    return parents, len(grouped)


def classify(parents: list[Parent], db: sqlite3.Connection, config_path: Path) -> tuple[list[Parent], Counter[str], dict[str, str]]:
    """Hold uncertain identities, owner exclusions, and near archive matches."""
    contacts: dict[str, set[int]] = defaultdict(set)
    names: dict[int, str] = {}
    for contact_id, identifier, name in db.execute(
        "SELECT ci.contact_id,ci.identifier,c.display_name FROM contact_identifiers ci "
        "JOIN contacts c ON c.id=ci.contact_id WHERE ci.source='imessage'"
    ):
        contacts[normalize_address(identifier)].add(contact_id)
        names[contact_id] = name
    archived: dict[str, list[tuple[int, bool, str]]] = defaultdict(list)
    optout: set[str] = set()
    for ts, sender, body, handle in db.execute(
        "SELECT m.ts_unix,m.sender,m.body,c.handle FROM messages m "
        "JOIN conversations c ON c.id=m.conversation_id "
        "WHERE c.is_group=0 AND m.source!='legacy_mms'"
    ):
        normalized = normalize_address(handle)
        archived[normalized].append((ts, sender == "Me", body))
        if sender == "Me" and body.strip().casefold() in ("stop", "quit"):
            optout.add(normalized)
    blocked = blocked_addresses(config_path)
    held: dict[str, str] = {}
    reasons: Counter[str] = Counter()
    selected: list[Parent] = []
    for parent in parents:
        reason = ""
        contact_ids = contacts[parent.handle]
        local = datetime.fromtimestamp(parent.date, LOCAL_ZONE)
        timestamp = int(local.replace(tzinfo=timezone.utc).timestamp())
        matches = [body for ts, outgoing, body in archived[parent.handle]
                   if outgoing == (parent.flags == 3) and abs(ts - timestamp) <= 86400]
        if parent.flags not in (2, 3):
            reason = "unverified_direction"
        elif len(contact_ids) != 1:
            reason = "unverified_contact"
        elif not re.fullmatch(r"\+1[0-9]{10}", parent.handle):
            reason = "unverified_address"
        elif parent.subject:
            reason = "subject_representation"
        elif parent.handle in blocked or normalize_address(names[next(iter(contact_ids))]) in blocked:
            reason = "owner_blocked"
        elif parent.handle in optout:
            reason = "owner_optout_thread"
        elif parent.flags == 2 and "appointment reminder" in parent.body.casefold():
            reason = "appointment_reminder"
        elif any(body == parent.body or (parent.body and body and
                       difflib.SequenceMatcher(None, parent.body, body).ratio() >= 0.85)
                 for body in matches):
            reason = "possible_archive_overlap"
        if reason:
            held[parent.identity] = reason
        else:
            selected.append(parent)
            reason = "provisional_new"
        reasons[reason] += 1
    return selected, reasons, held


def media_paths(db: sqlite3.Connection, media_root: Path) -> dict[str, Path]:
    """Locate cataloged archive bytes by SHA-256 filename."""
    result: dict[str, Path] = {}
    rows = db.execute(
        "SELECT rel_path FROM attachments WHERE rel_path LIKE 'permanent/%' "
        "UNION SELECT rel_path FROM imessage_source_assets WHERE rel_path LIKE 'permanent/%'"
    )
    for (rel,) in rows:
        name = Path(rel).name
        digest = name.split(".", 1)[0]
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            path = media_root / rel.removeprefix("permanent/")
            if path.is_file() and not path.is_symlink():
                result.setdefault(digest, path)
    return result


def append_isolated(db: sqlite3.Connection, selected: list[Parent], held: dict[str, str],
                    staging: Path, existing_media: Path, isolated_media: Path) -> tuple[int, int, int]:
    """Append selected parents and verified bytes to a disposable archive."""
    if isolated_media.resolve() == existing_media.resolve():
        raise ValueError("isolated media must differ from configured archive media")
    known_paths = media_paths(db, existing_media)
    contact_ids: dict[str, set[int]] = defaultdict(set)
    for contact_id, identifier in db.execute(
        "SELECT contact_id,identifier FROM contact_identifiers WHERE source='imessage'"
    ):
        contact_ids[normalize_address(identifier)].add(contact_id)
    added_messages = added_attachments = copied_files = 0
    isolated_media.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db:
        for parent in selected:
            if parent.identity in held:
                raise ValueError("selected MMS is also held")
            db.execute(
                "INSERT INTO conversations(source,name,handle,contact_id,is_group) "
                "VALUES(?,?,?,?,0) ON CONFLICT(source,name) DO NOTHING",
                (SOURCE, parent.handle, parent.handle, next(iter(contact_ids[parent.handle]))),
            )
            conversation_id = db.execute(
                "SELECT id FROM conversations WHERE source=? AND name=?",
                (SOURCE, parent.handle),
            ).fetchone()[0]
            local = datetime.fromtimestamp(parent.date, LOCAL_ZONE)
            ts = local.strftime("%Y-%m-%d %H:%M:%S")
            ts_unix = int(local.replace(tzinfo=timezone.utc).timestamp())
            existing = db.execute(
                "SELECT id,source,conversation_id,ts,ts_unix,sender,body,is_system,seq,source_guid "
                "FROM messages WHERE hash=?",
                                  (parent.identity,)).fetchone()
            expected = (SOURCE, conversation_id, ts, ts_unix,
                        "Me" if parent.flags == 3 else parent.handle,
                        parent.body, 0, 0, None)
            if existing is not None and existing[1:] != expected:
                raise ValueError("legacy message identity conflicts with archive")
            if existing is None:
                cursor = db.execute(
                    "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,sender,body,is_system,seq) "
                    "VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                    (parent.identity, conversation_id, SOURCE, ts, ts_unix,
                     "Me" if parent.flags == 3 else parent.handle, parent.body),
                )
                message_id = cursor.lastrowid
                added_messages += 1
            else:
                message_id = existing[0]
            primary: dict[int, str] = {}
            for asset in parent.assets:
                if asset.origin == "indexed_file":
                    primary.setdefault(asset.part_id, asset.digest)
            for asset in parent.assets:
                primary.setdefault(asset.part_id, asset.digest)
            by_digest: dict[str, Asset] = {}
            for asset in parent.assets:
                by_digest.setdefault(asset.digest, asset)
            for asset in by_digest.values():
                source_path = known_paths.get(asset.digest) if asset.already_cataloged else staging / asset.digest
                if source_path is None or not source_path.is_file() or source_path.is_symlink():
                    raise ValueError("legacy MMS media source missing")
                rel = ("permanent/" + asset.digest[:2] + "/" + asset.digest +
                       EXTENSIONS[asset.mime])
                target = isolated_media / rel.removeprefix("permanent/")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not target.exists():
                    shutil.copyfile(source_path, target)
                    target.chmod(0o600)
                    copied_files += 1
                if target.is_symlink() or target.stat().st_size != asset.size:
                    raise ValueError("isolated legacy MMS media size mismatch")
                with target.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != asset.digest:
                        raise ValueError("isolated legacy MMS media checksum mismatch")
                visible = primary[asset.part_id] == asset.digest
                kind = ("image" if asset.mime.startswith("image/") else
                        "video" if asset.mime.startswith("video/") else "file") if visible else "file"
                cursor = db.execute(
                    "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                    "SELECT ?,?,?,?,?,? WHERE NOT EXISTS "
                    "(SELECT 1 FROM attachments WHERE message_id=? AND rel_path=?)",
                    (message_id, kind, rel, asset.digest + EXTENSIONS[asset.mime],
                     conversation_id, ts_unix, message_id, rel),
                )
                added_attachments += cursor.rowcount
    return added_messages, added_attachments, copied_files


def main() -> None:
    """Run a private aggregate audit or append to a disposable database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--archive-media", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.apply and "reconstruction" not in args.db.resolve().parts:
        raise ValueError("legacy MMS writes require an isolated reconstruction database")
    source_digest = verify_sources(args.source_root)
    parents, observations = load_parents(args.source_root, args.catalog)
    uri = f"file:{args.db.resolve()}?mode={'rw' if args.apply else 'ro'}"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        selected, reasons, held = classify(parents, db, args.config)
        added = append_isolated(db, selected, held, args.staging, args.archive_media,
                               args.db.parent / "media" / "imessage") if args.apply else (0, 0, 0)
    LOG.info("legacy MMS audit: %s", json.dumps({
        "source_sha256": source_digest, "parent_observations": observations,
        "distinct_candidates": len(parents), "classification": reasons,
        "added_messages": added[0], "added_attachments": added[1],
        "copied_media_files": added[2],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
