"""Synthetic checks for inclusive Nexus recovery and owner exclusions."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET

from scripts.nexus_recovery_import import append_isolated, select_recovery
from scripts.nexus_sms_import import SMS, wall_time


class NexusRecoveryImportTests(unittest.TestCase):
    """Keep uncertain records available without reviving excluded senders."""

    def test_uncertain_records_repeat_and_source_removal(self) -> None:
        """Hold candidate overlaps while retaining other uncertain records."""
        with tempfile.TemporaryDirectory(prefix="nexus-recovery-test-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            staging = root / "staging"
            staging.mkdir()
            (staging / "manifest.json").write_text('{"assets": []}')
            config = root / "config.yaml"
            config.write_text("imessage_exclusions: []\n")
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({"schema_version": 1, "identities": [
                "excluded-a", "excluded-b", "excluded-c", "excluded-d"]}))
            when = int(datetime(2015, 5, 4, 12, tzinfo=timezone.utc).timestamp() * 1000)
            records = [
                SMS(when, "+15550000001", 1, "already in Apple"),
                SMS(when + 1_000, "+15550000002", 1, "already in Apple"),
                SMS(when + 30_000, "+15550000003", 1, "first observation"),
                SMS(when + 60_000, "+15550000003", 1, "first observation"),
                SMS(when + 90_000, "7535", 1, "transaction"),
                SMS(when + 120_000, "+15550000004", 2, "STOP"),
                SMS(when + 150_000, "+15550000004", 1, "reply"),
            ]
            document = ET.Element("smses", count=str(len(records)))
            for record in records:
                ET.SubElement(document, "sms", date=str(record.date_ms),
                              address=record.address, type=str(record.kind), body=record.body)
            ET.ElementTree(document).write(source / "messages.xml", encoding="utf-8")
            db_path = root / "msgbrowse.sqlite"
            with closing(sqlite3.connect(db_path)) as db:
                db.executescript("""
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE conversations (
                        id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
                        handle TEXT NOT NULL DEFAULT '', is_group INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(source,name));
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE,
                        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                        source TEXT NOT NULL, source_guid TEXT, ts TEXT NOT NULL,
                        ts_unix INTEGER NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL,
                        is_system INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE attachments (
                        id INTEGER PRIMARY KEY, message_id INTEGER REFERENCES messages(id),
                        conversation_id INTEGER NOT NULL, ts_unix INTEGER NOT NULL,
                        kind TEXT NOT NULL, rel_path TEXT NOT NULL, original_name TEXT NOT NULL);
                    CREATE VIRTUAL TABLE messages_fts USING fts5(body,content='messages',content_rowid='id');
                    CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO messages_fts(rowid,body) VALUES(new.id,new.body);
                    END;
                    INSERT INTO conversations(source,name,handle) VALUES
                        ('imessage','Apple copy','+15550000001');
                """)
                display, timestamp = wall_time(records[0])
                db.execute(
                    "INSERT INTO messages(hash,conversation_id,source,ts,ts_unix,sender,body) "
                    "VALUES('apple-row',1,'imessage',?,?,?,?)",
                    (display, timestamp, records[0].address, records[0].body))
                sms, mms, disposition = select_recovery(source, config, ledger, db)
                self.assertEqual(len(sms), 3)
                self.assertEqual(mms, [])
                self.assertEqual(disposition["possible_apple_overlap_held"], 1)
                self.assertEqual(disposition["owner_excluded"], 3)
                self.assertEqual(append_isolated(db, sms, mms, set(), staging, root), (3, 0, 0))
                self.assertEqual(append_isolated(db, sms, mms, set(), staging, root), (0, 0, 0))
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'observation'"
                ).fetchone()[0], 2)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            (source / "messages.xml").unlink()
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM messages WHERE source='nexus_recovery'"
                ).fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
