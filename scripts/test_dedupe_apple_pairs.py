"""Synthetic regression checks for Apple duplicate row reconciliation."""

from __future__ import annotations

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.dedupe_apple_pairs import dedupe_pairs


class ApplePairDedupeTests(unittest.TestCase):
    """Keep one visible row while retaining alternate source identities."""

    def test_guid_ledger_and_media_observations_survive(self) -> None:
        """Deduplication keeps content, media, search, and reimport tombstone."""
        with tempfile.TemporaryDirectory(prefix="apple-pair-dedupe-") as directory:
            path = Path(directory) / "archive.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE conversations(id INTEGER PRIMARY KEY, is_group INTEGER NOT NULL);
                    CREATE TABLE messages(
                        id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE,
                        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                        source TEXT NOT NULL, source_guid TEXT UNIQUE, ts TEXT NOT NULL,
                        ts_unix INTEGER NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL,
                        is_system INTEGER NOT NULL, seq INTEGER NOT NULL);
                    CREATE TABLE attachments(
                        id INTEGER PRIMARY KEY,
                        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL, rel_path TEXT NOT NULL, original_name TEXT NOT NULL);
                    CREATE TABLE imessage_source_assets(
                        id INTEGER PRIMARY KEY,
                        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                        source_attachment_guid TEXT NOT NULL, rel_path TEXT NOT NULL,
                        hidden INTEGER NOT NULL, kind TEXT NOT NULL, original_name TEXT NOT NULL,
                        UNIQUE(message_id,source_attachment_guid,rel_path));
                    CREATE TABLE imessage_attachment_observations(
                        id INTEGER PRIMARY KEY,
                        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                        source_attachment_guid TEXT NOT NULL, hidden INTEGER NOT NULL,
                        mime_type TEXT NOT NULL,
                        UNIQUE(message_id,source_attachment_guid,hidden,mime_type));
                    CREATE TABLE imessage_excluded_messages(
                        id INTEGER PRIMARY KEY, source_guid TEXT UNIQUE,
                        message_hash TEXT NOT NULL, reason TEXT NOT NULL,
                        excluded_at TEXT NOT NULL);
                    CREATE VIRTUAL TABLE messages_fts USING fts5(
                        body,content='messages',content_rowid='id');
                    CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO messages_fts(rowid,body) VALUES(new.id,new.body);
                    END;
                    CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts,rowid,body)
                        VALUES('delete',old.id,old.body);
                    END;
                    INSERT INTO conversations VALUES(1,0);
                    INSERT INTO messages VALUES
                        (1,'hash-first',1,'imessage','guid-first','2010-01-01',1,'Me','hello',0,0),
                        (2,'hash-second',1,'imessage','guid-second','2010-01-01',1,'Me','hello',0,0);
                    INSERT INTO attachments VALUES
                        (1,1,'image','permanent/aa/hash.jpg','one.jpg'),
                        (2,2,'image','permanent/aa/hash.jpg','two.jpg');
                    INSERT INTO imessage_source_assets VALUES
                        (1,1,'asset-first','permanent/aa/hash.jpg',0,'image','one.jpg'),
                        (2,2,'asset-second','permanent/aa/hash.jpg',0,'image','two.jpg');
                    INSERT INTO imessage_attachment_observations VALUES
                        (1,1,'asset-first',0,'image/jpeg'),
                        (2,2,'asset-second',0,'image/jpeg');
                """)
                with db:
                    self.assertEqual(dedupe_pairs(db, [(1, 2)], "2026-11-07T00:00:00Z"), 1)
                self.assertEqual(db.execute("SELECT id FROM messages").fetchall(), [(1,)])
                self.assertEqual(db.execute("SELECT message_id FROM attachments").fetchall(), [(1,)])
                self.assertEqual(db.execute(
                    "SELECT message_id FROM imessage_source_assets ORDER BY id"
                ).fetchall(), [(1,), (1,)])
                self.assertEqual(db.execute(
                    "SELECT message_id FROM imessage_attachment_observations ORDER BY id"
                ).fetchall(), [(1,), (1,)])
                self.assertEqual(db.execute(
                    "SELECT source_guid,message_hash,reason FROM imessage_excluded_messages"
                ).fetchone(), ("guid-second", "hash-second", "deduplicated_to_guid:guid-first"))
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'hello'"
                ).fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
