"""Synthetic checks for owner-directed transactional cleanup."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import cast

from scripts.transactional_cleanup import apply_plan, build_plan, save_plan


class TransactionalCleanupTests(unittest.TestCase):
    """Protect personal conversations and remove exact transactional rows."""

    def setUp(self) -> None:
        """Create a small archive with personal and transactional messages."""
        self.temp = tempfile.TemporaryDirectory(prefix="transactional-cleanup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "archive.sqlite"
        self.names = self.root / "named.json"
        self.names.write_text(json.dumps({"schema_version": 1,
                                          "named_senders": [{"sender": "Known Person"}]}))
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE conversations(id INTEGER PRIMARY KEY,name TEXT,is_group INTEGER);
                CREATE TABLE messages(id INTEGER PRIMARY KEY,hash TEXT UNIQUE,
                    conversation_id INTEGER REFERENCES conversations(id),ts_unix INTEGER,
                    sender TEXT,body TEXT,is_system INTEGER,source TEXT,source_guid TEXT);
                CREATE TABLE attachments(id INTEGER PRIMARY KEY,
                    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE);
                CREATE TABLE imessage_source_assets(id INTEGER PRIMARY KEY,
                    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE);
                CREATE TABLE imessage_excluded_messages(id INTEGER PRIMARY KEY,
                    source_guid TEXT UNIQUE,message_hash TEXT,reason TEXT,excluded_at TEXT);
                CREATE TABLE embeddings(message_hash TEXT);
                CREATE TABLE message_sentiment(message_hash TEXT);
                CREATE TABLE spam_events(message_hash TEXT);
                CREATE TABLE spam_findings(message_hash TEXT);
                CREATE TABLE contact_facts(source_message_hash TEXT);
                CREATE TABLE fact_state(last_message_hash TEXT);
                CREATE TABLE sentiment_state(last_message_hash TEXT);
                CREATE TABLE spam_state(last_message_hash TEXT);
                CREATE TABLE reactions(message_hash TEXT);
                INSERT INTO conversations VALUES
                    (1,'+15550000001',0),(2,'+15550000002',0),
                    (3,'Group',1),(4,'orphaned',0),(5,'Known Person',0),
                    (6,'+15550000004',0),(7,'+15550000005',0);
                INSERT INTO messages VALUES
                    (1,'txn',1,100,'+15550000001','Your verification code is 123456',0,'imessage','guid-txn'),
                    (2,'personal',2,101,'+15550000002','Your verification code is 222222',0,'imessage','guid-personal'),
                    (3,'reply',2,102,'Me','Thanks for helping me',0,'imessage','guid-reply'),
                    (4,'group',3,103,'Sender','Vote today and reply STOP',0,'imessage','guid-group'),
                    (5,'orphan-me',4,104,'Me','Stop',0,'imessage','guid-me'),
                    (6,'known',4,105,'Known Person','Your appointment reminder is here',0,'imessage','guid-known'),
                    (7,'orphan-unknown',4,106,'+15550000003','Please take our survey about your visit',0,'imessage','guid-orphan'),
                    (8,'nexus-txn',1,107,'+15550000001','Your appointment reminder is here',0,'nexus',NULL),
                    (9,'optout-in',6,108,'+15550000004','Hello there',0,'imessage','guid-optout-in'),
                    (10,'optout-out',6,109,'Me','STOP',0,'imessage','guid-optout-out'),
                    (11,'named-person',7,110,'Taylor Example','Your appointment reminder is here',0,'imessage','guid-named');
                INSERT INTO attachments VALUES(1,1);
            """)

    def test_checked_cleanup_keeps_personal_and_media_bytes(self) -> None:
        """A repeat source cannot erase personal rows or retained media bytes."""
        media = self.root / "media.bin"
        media.write_bytes(b"full original payload")
        plan = build_plan(self.database, self.names)
        self.assertEqual({item["id"] for item in cast(list[dict[str, object]], plan["selected"])},
                         {1, 7, 8, 9, 10})
        plan_path = self.root / "plan.json"
        checkpoint = self.root / "checkpoint.sqlite"
        save_plan(plan, plan_path)
        result = apply_plan(plan_path, checkpoint)
        self.assertEqual(result["messages_removed"], 5)
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(media.read_bytes(), b"full original payload")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT id FROM messages")},
                             {2, 3, 4, 5, 6, 11})
            self.assertEqual(db.execute(
                "SELECT count(*) FROM imessage_excluded_messages").fetchone()[0], 9)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
