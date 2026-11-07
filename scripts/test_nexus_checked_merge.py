"""Regression checks for the append-only Nexus merge path."""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.nexus_checked_merge import copy_verified_media, digest_rows, media_entries, merge_rows


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
    is_group INTEGER NOT NULL DEFAULT 0, handle TEXT NOT NULL DEFAULT '',
    UNIQUE(source,name)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    ts TEXT NOT NULL, ts_unix INTEGER NOT NULL, sender TEXT NOT NULL,
    body TEXT NOT NULL, is_system INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
    source_guid TEXT
);
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
    kind TEXT NOT NULL, rel_path TEXT NOT NULL, original_name TEXT NOT NULL,
    conversation_id INTEGER NOT NULL, ts_unix INTEGER NOT NULL
);
CREATE TABLE imessage_excluded_messages (
    id INTEGER PRIMARY KEY, source_guid TEXT, message_hash TEXT NOT NULL,
    reason TEXT NOT NULL, excluded_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE messages_fts USING fts5(body,content='messages',content_rowid='id');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid,body) VALUES(new.id,new.body);
END;
INSERT INTO conversations(id,source,name,handle)
VALUES(1,'imessage','+15550000001','+15550000001');
INSERT INTO messages(id,hash,conversation_id,ts,ts_unix,sender,body,source,source_guid)
VALUES(1,'apple-hash',1,'2015-05-04 08:00:00',1430740800,'Me','old message','imessage','apple-guid');
INSERT INTO imessage_excluded_messages(source_guid,message_hash,reason,excluded_at)
VALUES('excluded-guid','excluded-hash','owner','2026-11-07');
"""


class NexusCheckedMergeTests(unittest.TestCase):
    """Verify repeat merges, media retention, and baseline preservation."""

    def setUp(self) -> None:
        """Create separate synthetic source and target archives."""
        self.temp = tempfile.TemporaryDirectory(prefix="reconstruction-nexus-merge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.isolated_dir = self.root / "isolated"
        self.target_dir = self.root / "target"
        self.isolated_dir.mkdir()
        self.target_dir.mkdir()
        self.isolated_db = self.isolated_dir / "msgbrowse.sqlite"
        self.target_db = self.target_dir / "msgbrowse.sqlite"
        for path in (self.isolated_db, self.target_db):
            with closing(sqlite3.connect(path)) as db:
                db.executescript(SCHEMA)
        self.payload = b"retained synthetic MMS image"
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.name = self.digest + ".jpg"
        source_media = self.isolated_dir / "media" / "nexus"
        source_media.mkdir(parents=True)
        (source_media / self.name).write_bytes(self.payload)
        with closing(sqlite3.connect(self.isolated_db)) as db:
            db.execute(
                "INSERT INTO conversations(id,source,name,handle) "
                "VALUES(2,'nexus','+15550000002','+15550000002')"
            )
            db.execute(
                "INSERT INTO messages(id,hash,conversation_id,ts,ts_unix,sender,body,source) "
                "VALUES(2,'nexus-hash',2,'2015-05-04 08:01:00',1430740860,"
                "'+15550000002','synthetic message','nexus')"
            )
            db.execute(
                "INSERT INTO attachments(message_id,kind,rel_path,original_name,conversation_id,ts_unix) "
                "VALUES(2,'image',?,'image.jpg',2,1430740860)",
                ("permanent/" + self.name,),
            )
            db.commit()

    def test_repeat_and_source_omission_retain_rows_and_media(self) -> None:
        """A repeated merge adds nothing and source removal cannot erase target data."""
        with closing(sqlite3.connect(self.isolated_db)) as isolated, closing(sqlite3.connect(self.target_db)) as target:
            before = digest_rows(target)
            self.assertEqual(before, digest_rows(isolated))
            entries = media_entries(isolated, self.isolated_dir)
            self.assertEqual(copy_verified_media(self.isolated_dir, self.target_dir, entries), 1)
            self.assertEqual(copy_verified_media(self.isolated_dir, self.target_dir, entries), 0)
            target.execute("PRAGMA foreign_keys=ON")
            with target:
                self.assertEqual(merge_rows(isolated, target), (1, 1))
            with target:
                self.assertEqual(merge_rows(isolated, target), (0, 0))
            self.assertEqual(digest_rows(target), before)
            self.assertEqual(target.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(target.execute("SELECT count(*) FROM messages WHERE source='nexus'").fetchone()[0], 1)
            self.assertEqual(target.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'synthetic'").fetchone()[0], 1)
        (self.isolated_dir / "media" / "nexus" / self.name).unlink()
        retained = self.target_dir / "media" / "nexus" / self.name
        self.assertEqual(retained.read_bytes(), self.payload)
        with closing(sqlite3.connect(self.target_db)) as target:
            self.assertEqual(target.execute("SELECT count(*) FROM messages WHERE source='nexus'").fetchone()[0], 1)

    def test_existing_media_mismatch_is_rejected(self) -> None:
        """The merge never overwrites a changed permanent file."""
        with closing(sqlite3.connect(self.isolated_db)) as isolated:
            entries = media_entries(isolated, self.isolated_dir)
        target_media = self.target_dir / "media" / "nexus"
        target_media.mkdir(parents=True)
        (target_media / self.name).write_bytes(b"different bytes")
        with self.assertRaisesRegex(ValueError, "target Nexus media"):
            copy_verified_media(self.isolated_dir, self.target_dir, entries)
        self.assertEqual((target_media / self.name).read_bytes(), b"different bytes")


if __name__ == "__main__":
    unittest.main()
