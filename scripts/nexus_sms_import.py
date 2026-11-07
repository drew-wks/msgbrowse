#!/usr/bin/env python3
"""Audit Nexus SMS Backup & Restore XML and append safe SMS to an isolated DB.

This reconstruction tool never writes to the configured archive. It keeps
Android observations under the separate ``nexus`` source and leaves Apple
GUIDs untouched. MMS is intentionally handled by a separate media audit.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


LOG = logging.getLogger("nexus-sms-import")
LOCAL_ZONE = ZoneInfo("America/Toronto")
SOURCE = "nexus"
SHORT_CODE = re.compile(r"^[0-9]{4,6}$")
CONVERSATION_RULE = re.compile(r'^\s+conversation: "([^"]+)"', re.MULTILINE)


@dataclass(frozen=True)
class SMS:
    """One distinct Android SMS observation with its original identity fields."""

    date_ms: int
    address: str
    kind: int
    body: str

    @property
    def identity(self) -> str:
        """Return a stable source-specific hash without calling it an Apple GUID."""
        fields = [self.date_ms, normalize_address(self.address), self.kind, self.body]
        payload = b"nexus-sms\x00" + json.dumps(fields, ensure_ascii=False).encode()
        return hashlib.sha256(payload).hexdigest()


def normalize_address(value: str) -> str:
    """Normalize North American phone variants for exclusion and grouping.

    Args:
        value: Address as it appears in the Nexus XML.

    Returns:
        An E.164-style North American number, or the trimmed original.
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return value.strip().casefold()


def supported_address(value: str) -> bool:
    """Accept a phone-shaped address; hold aliases and email for review."""
    normalized = normalize_address(value)
    return bool(re.fullmatch(r"\+?[0-9]{7,15}", normalized))


def xml_files(root: Path) -> list[Path]:
    """Find message XML inputs under a file or directory.

    Args:
        root: A single XML file or the copied Nexus source directory.

    Returns:
        Sorted XML paths. Call-log files are rejected by their root tag.
    """
    paths = [root] if root.is_file() else sorted(root.rglob("*.xml"))
    if not paths:
        raise ValueError("no Nexus XML files found")
    return paths


def load_sms(root: Path) -> tuple[list[SMS], int]:
    """Stream XML and deduplicate observations after phone normalization.

    Args:
        root: Copied Nexus XML file or directory.

    Returns:
        Distinct SMS records and the raw SMS observation count.

    Raises:
        ValueError: A source has an unexpected root or malformed SMS fields.
    """
    unique: dict[tuple[int, str, int, str], SMS] = {}
    observations = 0
    for path in xml_files(root):
        parser = ET.iterparse(path, events=("start", "end"))
        _, document = next(parser)
        if document.tag != "smses":
            raise ValueError(f"unexpected XML root in {path.name}")
        for event, element in parser:
            if event != "end":
                continue
            if element.tag != "sms":
                if element.tag == "mms":
                    element.clear()
                continue
            date = element.get("date", "")
            kind = element.get("type", "")
            if not date.isdigit() or kind not in ("1", "2"):
                raise ValueError(f"invalid SMS date or direction in {path.name}")
            record = SMS(int(date), element.get("address", ""), int(kind), element.get("body", ""))
            unique[(record.date_ms, normalize_address(record.address), record.kind, record.body)] = record
            observations += 1
            element.clear()
        document.clear()
    return list(unique.values()), observations


def blocked_addresses(config_path: Path) -> set[str]:
    """Read exact owner-blocked conversation addresses from the current config.

    Args:
        config_path: msgbrowse YAML configuration.

    Returns:
        Normalized addresses in whole-conversation exclusion rules.

    Raises:
        ValueError: A conditional rule cannot be represented safely here.
    """
    config = config_path.read_text()
    if re.search(r"^\s+body_contains:", config, re.MULTILINE):
        raise ValueError("conditional exclusion rules need manual review")
    rules = list(CONVERSATION_RULE.finditer(config))
    if len(rules) != len(re.findall(r"^\s+- name:", config, re.MULTILINE)):
        raise ValueError("could not parse every whole-conversation exclusion")
    return {normalize_address(m.group(1)) for m in rules}


def archive_times(db: sqlite3.Connection) -> dict[tuple[str, bool], set[int]]:
    """Index Apple rows by body and direction for conservative overlap holds."""
    result: dict[tuple[str, bool], set[int]] = defaultdict(set)
    rows = db.execute(
        "SELECT ts_unix, sender, body FROM messages "
        "WHERE source='imessage' AND is_system=0"
    )
    for timestamp, sender, body in rows:
        result[(body, sender == "Me")].add(timestamp)
    return result


def wall_time(record: SMS) -> tuple[str, int]:
    """Map an Android UTC timestamp to the archive's local wall-clock key."""
    local = datetime.fromtimestamp(record.date_ms / 1000, LOCAL_ZONE)
    display = local.strftime("%Y-%m-%d %H:%M:%S")
    return display, int(local.replace(tzinfo=timezone.utc).timestamp())


def classify(
    records: list[SMS], blocked: set[str], times: dict[tuple[str, bool], set[int]]
) -> tuple[list[SMS], Counter[str], dict[str, str]]:
    """Hold source collisions, exclusions, and possible Apple overlaps.

    Args:
        records: Deduplicated SMS observations.
        blocked: Owner-defined whole-conversation exclusions.
        times: Indexed archived Apple rows.

    Returns:
        Provisional additions, counts, and held source identities.
    """
    group_bodies: dict[tuple[int, str, int], set[str]] = defaultdict(set)
    near_groups: dict[tuple[str, int, str], list[SMS]] = defaultdict(list)
    for record in records:
        group_bodies[(record.date_ms, normalize_address(record.address), record.kind)].add(record.body)
        near_groups[(normalize_address(record.address), record.kind, record.body)].append(record)
    near_source: set[str] = set()
    for group in near_groups.values():
        ordered = sorted(group, key=lambda record: record.date_ms)
        for left, right in zip(ordered, ordered[1:]):
            if 0 < right.date_ms - left.date_ms <= 300_000:
                near_source.update((left.identity, right.identity))
    optout = {
        normalize_address(record.address)
        for record in records
        if record.kind == 2 and record.body.strip().casefold() in ("stop", "quit")
    }
    selected: list[SMS] = []
    reasons: Counter[str] = Counter()
    held: dict[str, str] = {}
    for record in records:
        address = normalize_address(record.address)
        group = (record.date_ms, address, record.kind)
        reason = ""
        if len(group_bodies[group]) > 1:
            reason = "source_body_conflict"
        elif address in blocked:
            reason = "owner_blocked"
        elif SHORT_CODE.fullmatch(record.address):
            reason = "short_code"
        elif address in optout:
            reason = "owner_optout_thread"
        elif record.kind == 1 and "appointment reminder" in record.body.casefold():
            reason = "appointment_reminder"
        elif record.identity in near_source:
            reason = "near_source_duplicate_candidate"
        elif not record.body:
            reason = "empty_body"
        elif not supported_address(record.address):
            reason = "unverified_address"
        else:
            _, timestamp = wall_time(record)
            candidates = times.get((record.body, record.kind == 2), set())
            if any(abs(existing - timestamp) <= 86400 for existing in candidates):
                reason = "possible_apple_overlap"
        if reason:
            reasons[reason] += 1
            held[record.identity] = reason
        else:
            selected.append(record)
            reasons["provisional_new"] += 1
    return selected, reasons, held


def append_isolated(db: sqlite3.Connection, records: list[SMS], held: dict[str, str]) -> int:
    """Append selected records under a distinct Nexus source in one transaction.

    Args:
        db: Isolated archive database connection.
        records: Provisional additions from :func:`classify`.
        held: Source identities that must not be admitted by a later partial pass.

    Returns:
        Count of newly inserted messages.
    """
    added = 0
    with db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS nexus_import_holds "
            "(identity TEXT PRIMARY KEY, reason TEXT NOT NULL)"
        )
        for identity in held:
            if db.execute(
                "SELECT 1 FROM messages WHERE hash=? AND source=?", (identity, SOURCE)
            ).fetchone():
                raise ValueError("previously imported Nexus record is now held for review")
        db.executemany(
            "INSERT OR IGNORE INTO nexus_import_holds(identity,reason) VALUES(?,?)",
            held.items(),
        )
        for record in records:
            if db.execute(
                "SELECT 1 FROM nexus_import_holds WHERE identity=?", (record.identity,)
            ).fetchone():
                continue
            address = normalize_address(record.address)
            db.execute(
                "INSERT INTO conversations(source,name,handle,is_group) VALUES(?,?,?,0) "
                "ON CONFLICT(source,name) DO NOTHING",
                (SOURCE, address, address),
            )
            row = db.execute(
                "SELECT id FROM conversations WHERE source=? AND name=?", (SOURCE, address)
            ).fetchone()
            if row is None:
                raise ValueError("Nexus conversation was not created")
            display, timestamp = wall_time(record)
            existing = db.execute("SELECT source FROM messages WHERE hash=?", (record.identity,)).fetchone()
            if existing is not None:
                if existing[0] != SOURCE:
                    raise ValueError("message hash belongs to another source")
                continue
            db.execute(
                "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,"
                "sender,body,is_system,seq) VALUES(?,?,?,NULL,?,?,?,?,0,0)",
                (
                    record.identity,
                    row[0],
                    SOURCE,
                    display,
                    timestamp,
                    "Me" if record.kind == 2 else address,
                    record.body,
                ),
            )
            added += 1
    return added


def run(source_root: Path, db_path: Path, config_path: Path, apply: bool) -> dict[str, int]:
    """Audit Nexus SMS and optionally append into reconstruction-only SQLite.

    Args:
        source_root: Hash-verified local source XML directory or one XML file.
        db_path: Archive-shaped SQLite database.
        config_path: msgbrowse configuration with exclusion rules.
        apply: Whether to write eligible SMS to an isolated database.

    Returns:
        Aggregate counts without personal message content.
    """
    if apply and not any(part.startswith("reconstruction") for part in db_path.resolve().parts):
        raise ValueError("writes require an isolated reconstruction database")
    uri = f"file:{db_path.resolve()}?mode={'rw' if apply else 'ro'}"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        records, observations = load_sms(source_root)
        selected, reasons, held = classify(records, blocked_addresses(config_path), archive_times(db))
        added = append_isolated(db, selected, held) if apply else 0
        result = dict(reasons)
        result.update(observations=observations, distinct_sms=len(records), added=added)
        return result


def main() -> None:
    """Parse CLI arguments and log aggregate audit results only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="write only to an isolated reconstruction DB")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    LOG.info("nexus SMS audit: %s", json.dumps(run(args.source_root, args.db, args.config, args.apply), sort_keys=True))


if __name__ == "__main__":
    main()
