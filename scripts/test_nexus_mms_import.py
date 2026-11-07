"""Synthetic identity, media, and retention checks for Nexus MMS imports."""

from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET

from scripts.nexus_mms_import import run


class NexusMMSImportTests(unittest.TestCase):
    """Verify normalized MMS identity and permanent isolated media copies."""

    def setUp(self) -> None:
        """Create a small archive-shaped SQLite database and media staging."""
        self.temp = tempfile.TemporaryDirectory(prefix="reconstruction-mms-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "msgbrowse.sqlite"
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text("imessage_exclusions: []\n")
        self.staging = self.root / "staged-media"
        self.staging.mkdir()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE conversations (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
                    handle TEXT NOT NULL DEFAULT '', is_group INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source,name)
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE,
                    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                    source TEXT NOT NULL, source_guid TEXT, ts TEXT NOT NULL,
                    ts_unix INTEGER NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL,
                    is_system INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE attachments (
                    id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
                    conversation_id INTEGER NOT NULL, ts_unix INTEGER NOT NULL,
                    kind TEXT NOT NULL, rel_path TEXT NOT NULL, original_name TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE messages_fts USING fts5(body,content='messages',content_rowid='id');
                CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid,body) VALUES(new.id,new.body);
                END;
                """
            )
        self.date_ms = int(datetime(2015, 5, 4, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def _write_mms(self, filename: str, participants: str, payload: bytes, offset_ms: int = 0) -> Path:
        """Write one MMS element with a text part and one inline image."""
        root = ET.Element("smses", count="1")
        mms = ET.SubElement(root, "mms", date=str(self.date_ms + offset_ms), address=participants, msg_box="1", sub="null")
        addrs = ET.SubElement(mms, "addrs")
        sender = participants.split("~")[0]
        ET.SubElement(addrs, "addr", type="137", address=sender)
        ET.SubElement(addrs, "addr", type="151", address=participants.split("~")[-1])
        parts = ET.SubElement(mms, "parts")
        ET.SubElement(parts, "part", seq="0", ct="text/plain", text="retain mms")
        ET.SubElement(parts, "part", seq="1", ct="image/jpeg", data=base64.b64encode(payload).decode(), fn="picture.jpg")
        path = self.root / filename
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path

    def _stage(self, payloads: list[bytes]) -> None:
        """Create a verified content-addressed MMS staging manifest."""
        assets = []
        for payload in payloads:
            digest = hashlib.sha256(payload).hexdigest()
            filename = digest + ".jpg"
            (self.staging / filename).write_bytes(payload)
            assets.append({"sha256": digest, "mime": "image/jpeg", "bytes": len(payload), "file": filename})
        (self.staging / "manifest.json").write_text(json.dumps({"assets": assets}))

    def test_normalized_overlap_repeat_and_media_copy(self) -> None:
        """Reordered/renumbered recipients deduplicate while distinct parts remain."""
        first = self._write_mms("first.xml", "+15550000002~+15550000003", b"image-one")
        self._write_mms("second.xml", "5550000002~5550000003", b"image-one")
        self._write_mms("third.xml", "+15550000002~+15550000003", b"image-two")
        self._write_mms("fourth.xml", "+15550000004", b"image-three", 10_000)
        self._write_mms("fifth.xml", "+15550000004", b"image-three", 40_000)
        self._stage([b"image-one", b"image-two", b"image-three"])

        first_run = run(self.root, self.root, self.db_path, self.config_path, self.staging, True)
        self.assertEqual(first_run["distinct_mms"], 4)
        self.assertEqual(first_run["added_messages"], 2)
        self.assertEqual(first_run["added_attachments"], 2)
        self.assertEqual(first_run["near_source_duplicate_candidate"], 2)
        self.assertEqual(run(self.root, self.root, self.db_path, self.config_path, self.staging, True)["added_messages"], 0)
        self.assertEqual(run(first, self.root, self.db_path, self.config_path, self.staging, True)["added_messages"], 0)

        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE source='nexus'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM attachments").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'retain'").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            paths = [row[0] for row in db.execute("SELECT rel_path FROM attachments")]
        for rel in paths:
            self.assertTrue(rel.startswith("permanent/"))
            target = self.root / "media" / "nexus" / rel.removeprefix("permanent/")
            self.assertTrue(target.is_file())
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), target.stem)


if __name__ == "__main__":
    unittest.main()
