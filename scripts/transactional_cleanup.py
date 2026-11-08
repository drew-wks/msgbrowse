#!/usr/bin/env python3
"""Plan and check an owner-directed cleanup of obvious transactional texts.

The classifier is deliberately narrow. It never sends message text off the
machine, and its private plan contains identities and digests rather than text.
The archive is changed only with --apply and a fresh SQLite-only checkpoint.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import cast


LOG = logging.getLogger("transactional-cleanup")
CODE = re.compile(r"\b(?:verification|security|authentication|one.time|2fa|two.factor|login|sign.in|passcode|password reset|access)\s+(?:\w+\s+){0,3}code\b|\bcode\s+(?:is|:|for)\b", re.I)
DIGITS = re.compile(r"\b\d{4,8}\b")
APPOINTMENT = re.compile(r"\b(?:appointment|visit|reservation)\s+(?:reminder|confirmation|confirmed|scheduled|tomorrow|today|at\s+\d)|\breminder\s+(?:of|for|about)\s+(?:your\s+)?(?:appointment|visit)\b", re.I)
SURVEY = re.compile(r"\b(?:take|complete|fill out|respond to|participate in|share your feedback|rate your)\b.{0,65}\b(?:survey|experience|service|visit)\b|\b(?:survey|feedback request)\b.{0,65}\b(?:visit|experience|service|complete|take|rate)\b", re.I | re.S)
DELIVERY = re.compile(r"\b(?:your\s+)?(?:order|package|shipment|delivery)\b.{0,70}\b(?:confirmed|confirmation|shipped|delivered|tracking|on its way|arriving)\b", re.I | re.S)
ACCOUNT = re.compile(r"\b(?:your\s+)?(?:account|bill|payment|statement|balance|plan data|data plan)\b.{0,75}\b(?:due|available|past due|overdue|charged|credited|low|used|usage|alert|notice|update|processed|declined)\b", re.I | re.S)
POLITICAL = re.compile(r"\b(?:vote|voting|voter|election|ballot|campaign|candidate|senate|congress|polling place|primary)\b", re.I)
POLITICAL_ACTION = re.compile(r"\b(?:donate|contribute|volunteer|register to vote|verify your vote|cast your vote|return your ballot|support our|win this race|election day|polls close|make your voice heard)\b", re.I)
OPTOUT = re.compile(r"\b(?:reply|text)\s+(?:stop|quit|unsubscribe)\b|\bstop\s+to\s+(?:end|opt out|unsubscribe)\b|\bmsg\s*&\s*data rates\b", re.I)
PROMO = re.compile(r"\b(?:offer|deal|sale|discount|save \d+%|exclusive|limited time|act now|sign up|join us|click here|learn more|donate|contribute)\b", re.I)
URL = re.compile(r"https?://|\b[a-z0-9-]+\.(?:com|org|net|gov|io|co|us)(?:/|\b)", re.I)
CONFIRMATION = re.compile(r"\b(?:confirmation|reference|ticket)\s+(?:number|code|#|is:)\b", re.I)
NOTICE = re.compile(r"\b(?:this is (?:a|an) (?:automated )?(?:notice|reminder)|important notice|do not reply|no reply)\b", re.I)
NORMALIZE = re.compile(r"https?://\S+|\b\d+\b", re.I)
HASH_DEPENDENCIES = (
    ("embeddings", "message_hash"), ("message_sentiment", "message_hash"),
    ("spam_events", "message_hash"), ("spam_findings", "message_hash"),
    ("contact_facts", "source_message_hash"), ("fact_state", "last_message_hash"),
    ("sentiment_state", "last_message_hash"), ("spam_state", "last_message_hash"),
    ("reactions", "message_hash"),
)


def category(body: str) -> str | None:
    """Classify strong transactional templates without an external model.

    Args:
        body: Locally archived message body.

    Returns:
        A conservative category or None for ambiguous content.
    """
    if CODE.search(body) and DIGITS.search(body):
        return "code"
    if DIGITS.search(body) and re.search(r"\b(?:your|use|enter|temporary|one.time|do not share)\b", body, re.I) and re.search(r"\b(?:passcode|verification|security code|auth code|access code)\b", body, re.I):
        return "code"
    if APPOINTMENT.search(body):
        return "appointment"
    if SURVEY.search(body):
        return "survey"
    if URL.search(body) and re.search(r"\b(?:survey|feedback)\b", body, re.I) and re.search(r"\b(?:your|take|complete|response|share|rate)\b", body, re.I):
        return "survey"
    if DELIVERY.search(body) and (URL.search(body) or CONFIRMATION.search(body)):
        return "delivery"
    if URL.search(body) and re.search(r"\b(?:your|tracking)\b.{0,50}\b(?:package|order|delivery|shipment|tracking)\b", body, re.I | re.S):
        return "delivery"
    if ACCOUNT.search(body) and (URL.search(body) or OPTOUT.search(body) or NOTICE.search(body)):
        return "account"
    if ACCOUNT.search(body) and DIGITS.search(body) and re.search(r"\byour\b", body, re.I):
        return "account"
    if CONFIRMATION.search(body) and DIGITS.search(body) and (URL.search(body) or NOTICE.search(body)):
        return "confirmation"
    if CONFIRMATION.search(body) and DIGITS.search(body) and re.search(r"\b(?:your|is|number|code)\b", body, re.I):
        return "confirmation"
    if POLITICAL.search(body) and (OPTOUT.search(body) or POLITICAL_ACTION.search(body)):
        return "political"
    if POLITICAL.search(body) and URL.search(body) and re.search(r"\b(?:vote (?:today|now|for|early|by|to)|your ballot|donate (?:today|now|to)|contribute (?:today|now|to))\b", body, re.I):
        return "political"
    if OPTOUT.search(body) and (URL.search(body) or PROMO.search(body)):
        return "optout_marketing"
    if NOTICE.search(body) and (URL.search(body) or OPTOUT.search(body)):
        return "notice"
    if URL.search(body) and re.search(r"\bnotice\b", body, re.I) and re.search(r"\b(?:your|account|plan|payment|bill)\b", body, re.I):
        return "notice"
    return None


def known_senders(path: Path) -> set[str]:
    """Read the owner's named orphan keep list; exclude the rejected name.

    Args:
        path: Private named-sender review JSON path.

    Returns:
        Case-folded names the owner explicitly chose to retain.
    """
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1:
        raise ValueError("unknown named sender review version")
    return {entry["sender"].casefold() for entry in data["named_senders"]
            if entry["sender"] != "Lejtman Saul"}


def looks_personal_sender(sender: str) -> bool:
    """Conservatively protect a conversation with a person-shaped sender label.

    The label shape does not prove an identity; it is only a reason to defer
    automatic deletion. Brand names may also be deferred for owner review.

    Args:
        sender: Archived sender label.

    Returns:
        Whether the label has two to four alphabetic name-like words.
    """
    return re.fullmatch(r"[^\W\d_][\w'’-]*(?: [^\W\d_][\w'’-]*){1,3}", sender) is not None


def content_fingerprint(ts_unix: int, body: str) -> str:
    """Match the Go importer's conversation-independent TXT fingerprint.

    Args:
        ts_unix: Parsed Unix time in seconds.
        body: Parsed message text.

    Returns:
        The content-v1 ledger key.
    """
    value = f"{ts_unix}\x00{body}".encode()
    return "content-v1:" + hashlib.sha256(value).hexdigest()


def classify(db: sqlite3.Connection, names: set[str]) -> list[dict[str, object]]:
    """Select messages while protecting groups and substantive personal chats.

    Args:
        db: Readable archive connection.
        names: Owner-approved names to keep.

    Returns:
        Exact message identities and metadata, without message bodies.
    """
    db.row_factory = sqlite3.Row
    rows = list(db.execute(
        "SELECT m.id,m.hash,m.conversation_id,m.ts_unix,m.sender,m.body,"
        "m.is_system,m.source,m.source_guid,c.name,c.is_group "
        "FROM messages m JOIN conversations c ON c.id=m.conversation_id"
    ))
    by_conversation: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_conversation[row["conversation_id"]].append(row)
    protected: set[int] = set()
    for conversation_id, members in by_conversation.items():
        if members[0]["is_group"] or members[0]["name"].casefold() in names:
            protected.add(conversation_id)
            continue
        if members[0]["name"] == "orphaned":
            continue
        if any(row["sender"].casefold() in names or
               (row["sender"].casefold() != "lejtman saul" and looks_personal_sender(row["sender"]))
               for row in members):
            protected.add(conversation_id)
            continue
        if any(row["sender"] == "Me" and row["body"].strip().casefold() not in ("stop", "quit", "")
               and not row["is_system"] for row in members):
            protected.add(conversation_id)
    chosen: dict[int, tuple[sqlite3.Row, str]] = {}
    for row in rows:
        if row["conversation_id"] in protected or row["sender"] == "Me" or row["is_system"]:
            continue
        if row["sender"].casefold() in names or not row["body"].strip():
            continue
        kind = category(row["body"])
        if kind:
            chosen[row["id"]] = (row, kind)
    # The owner's earlier instruction explicitly excludes an entire one-to-one
    # thread when every substantive outgoing message is STOP or QUIT.
    for conversation_id, members in by_conversation.items():
        if conversation_id in protected or members[0]["name"] == "orphaned":
            continue
        outgoing = [row["body"].strip().casefold() for row in members
                    if row["sender"] == "Me" and not row["is_system"]]
        if outgoing and all(body in ("stop", "quit", "") for body in outgoing) and any(
                body in ("stop", "quit") for body in outgoing):
            for row in members:
                if not row["is_system"]:
                    chosen[row["id"]] = (row, "owner_optout_thread")
    # Only include an unmatched message when it closely repeats a selected
    # template from the same conversation. Numbers and URLs may vary.
    templates: dict[int, list[str]] = defaultdict(list)
    for row, _ in chosen.values():
        templates[row["conversation_id"]].append(NORMALIZE.sub("#", row["body"].casefold()))
    for row in rows:
        if row["id"] in chosen or row["conversation_id"] not in templates:
            continue
        if row["sender"] == "Me" or row["is_system"] or len(row["body"]) < 30:
            continue
        normalized = NORMALIZE.sub("#", row["body"].casefold())
        if any(SequenceMatcher(None, normalized, template).ratio() >= 0.93
               for template in templates[row["conversation_id"]]):
            chosen[row["id"]] = (row, "template_neighbor")
    # An explicit whole-message opt-out in an otherwise selected one-way
    # conversation belongs with that thread; never sweep orphaned 'Me' rows.
    selected_conversations = {row["conversation_id"] for row, _ in chosen.values()}
    for row in rows:
        if (row["conversation_id"] in selected_conversations and
                row["name"] != "orphaned" and row["sender"] == "Me" and
                row["body"].strip().casefold() in ("stop", "quit")):
            chosen[row["id"]] = (row, "owner_optout_reply")
    result = []
    for row, kind in chosen.values():
        result.append({
            "id": row["id"], "hash": row["hash"], "source": row["source"],
            "guid": row["source_guid"], "conversation_id": row["conversation_id"],
            "ts_unix": row["ts_unix"], "body_sha256": hashlib.sha256(row["body"].encode()).hexdigest(),
            "content_fingerprint": content_fingerprint(row["ts_unix"], row["body"])
            if row["source"] == "imessage" else None,
            "category": kind,
        })
    return sorted(result, key=lambda item: int(str(item["id"])))


def build_plan(db_path: Path, names_path: Path) -> dict[str, object]:
    """Build a private, identity-only plan from the current archive.

    Args:
        db_path: Configured archive SQLite database.
        names_path: Owner's reviewed keep-list JSON.

    Returns:
        Checked deletion plan and aggregate statistics.
    """
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
        names = known_senders(names_path)
        records = classify(db, names)
        selected = {int(str(item["id"])) for item in records}
        kept_fingerprints = {
            content_fingerprint(ts, body)
            for message_id, ts, body in db.execute(
                "SELECT id,ts_unix,body FROM messages WHERE source='imessage'")
            if message_id not in selected
        }
        if any(item["content_fingerprint"] in kept_fingerprints for item in records
               if item["content_fingerprint"]):
            raise ValueError("content fingerprint collides with a retained message")
        existing_ledger = {value for (value,) in db.execute(
            "SELECT message_hash FROM imessage_excluded_messages")}
        if any(item["hash"] in existing_ledger for item in records):
            raise ValueError("selected message already has a ledger hash")
        existing_guids = {value for (value,) in db.execute(
            "SELECT source_guid FROM imessage_excluded_messages WHERE source_guid IS NOT NULL")}
        if any(item["guid"] in existing_guids for item in records if item["guid"]):
            raise ValueError("selected Apple GUID already has a ledger entry")
        hashes = [str(item["hash"]) for item in records]
        for table, column in HASH_DEPENDENCIES:
            if hashes and db.execute(
                f"SELECT 1 FROM {table} WHERE {column} IN ("
                + ",".join("?" for _ in hashes) + ") LIMIT 1", hashes
            ).fetchone():
                raise ValueError(f"selected message has dependent data in {table}")
        attachments = db.execute(
            "SELECT count(*) FROM attachments WHERE message_id IN ("
            + ",".join("?" for _ in records) + ")", sorted(selected)
        ).fetchone()[0] if records else 0
        assets = db.execute(
            "SELECT count(*) FROM imessage_source_assets WHERE message_id IN ("
            + ",".join("?" for _ in records) + ")", sorted(selected)
        ).fetchone()[0] if records else 0
        total = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    return {
        "schema_version": 1, "database": str(db_path.resolve()),
        "known_senders": str(names_path.resolve()),
        "known_senders_sha256": hashlib.sha256(names_path.read_bytes()).hexdigest(),
        "messages_before": total, "selected": records,
        "by_category": dict(Counter(str(item["category"]) for item in records)),
        "by_source": dict(Counter(str(item["source"]) for item in records)),
        "attachment_references": attachments, "source_asset_references": assets,
    }


def save_plan(plan: dict[str, object], path: Path) -> None:
    """Write a new private plan without archived message bodies.

    Args:
        plan: Checked cleanup plan.
        path: New private report path.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(plan, stream, indent=2)
        stream.write("\n")


def apply_plan(plan_path: Path, checkpoint: Path) -> dict[str, int]:
    """Checkpoint SQLite and remove only rows that still match the plan.

    Args:
        plan_path: Private plan generated by this script.
        checkpoint: New local SQLite-only backup path.

    Returns:
        Aggregate removal counts.
    """
    plan = json.loads(plan_path.read_text())
    if plan.get("schema_version") != 1:
        raise ValueError("unknown plan version")
    db_path = Path(plan["database"])
    names_path = Path(plan["known_senders"])
    if str(checkpoint).startswith("/Volumes/KINGSTON/") or checkpoint.exists() or checkpoint == db_path:
        raise ValueError("checkpoint must be a new local path outside KINGSTON")
    if build_plan(db_path, names_path) != plan:
        raise ValueError("archive or keep list changed after planning")
    records = plan["selected"]
    if not records:
        return {"messages_removed": 0}
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as original, closing(sqlite3.connect(checkpoint)) as saved:
        original.backup(saved)
        if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite checkpoint failed integrity check")
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            if build_plan(db_path, names_path) != plan:
                raise ValueError("archive changed after checkpoint")
            for item in records:
                row = db.execute("SELECT hash,source_guid,ts_unix,body FROM messages WHERE id=?",
                                 (item["id"],)).fetchone()
                if row is None or row[0] != item["hash"] or row[1] != item["guid"] or row[2] != item["ts_unix"] or hashlib.sha256(row[3].encode()).hexdigest() != item["body_sha256"]:
                    raise ValueError("selected row changed")
                db.execute(
                    "INSERT INTO imessage_excluded_messages(source_guid,message_hash,reason,excluded_at) "
                    "VALUES(?,?,?,?)", (item["guid"] if item["source"] == "imessage" else None,
                                       item["hash"], "owner_transactional:" + item["category"], now))
                if item["content_fingerprint"]:
                    db.execute(
                        "INSERT INTO imessage_excluded_messages(source_guid,message_hash,reason,excluded_at) "
                        "VALUES(NULL,?,?,?)", (item["content_fingerprint"],
                                             "owner_transactional:content_v1", now))
                if db.execute("DELETE FROM messages WHERE id=? AND hash=?",
                              (item["id"], item["hash"])).rowcount != 1:
                    raise ValueError("selected row disappeared")
            if db.execute("SELECT count(*) FROM messages").fetchone()[0] != plan["messages_before"] - len(records):
                raise ValueError("unexpected message count")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("foreign key check failed")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return {"messages_removed": len(records), "ledger_entries_added":
            len(records) + sum(bool(item["content_fingerprint"]) for item in records)}


def main() -> None:
    """Run a metadata-only plan or apply an exact checked cleanup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--known-senders", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    if args.apply:
        if not args.checkpoint:
            parser.error("--apply requires --checkpoint")
        result = apply_plan(args.plan, args.checkpoint)
    else:
        if not args.db or not args.known_senders:
            parser.error("planning requires --db and --known-senders")
        result = build_plan(args.db, args.known_senders)
        save_plan(result, args.plan)
        result = {"selected": len(cast(list[object], result["selected"])),
                  "by_category": result["by_category"],
                  "by_source": result["by_source"],
                  "attachment_references": result["attachment_references"],
                  "source_asset_references": result["source_asset_references"]}
    LOG.info("transactional cleanup result: %s", json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
