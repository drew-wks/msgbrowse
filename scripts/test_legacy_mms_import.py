"""Regression checks for isolated legacy MMS retention and idempotency."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.legacy_mms_import import Asset, Parent, append_isolated, classify


class LegacyMMSImportTest(unittest.TestCase):
    """Exercise a source deletion after a repeated content-addressed import."""

    def test_repeated_import_keeps_bytes_after_source_deletion(self) -> None:
        """Retain one MMS and its file independently of disposable staging."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            original = root / "configured-media"
            original.mkdir()
            isolated = root / "isolated-media"
            config = root / "config.yaml"
            config.write_text("", encoding="utf-8")
            payload = b"fixture-image-bytes"
            digest = hashlib.sha256(payload).hexdigest()
            (stage / digest).write_bytes(payload)
            parent = Parent(1_288_000_000, 2, "+15551234567", "", "fixture text", (
                Asset(1, "indexed_file", digest, "image/jpeg", len(payload), -1, False),
            ))
            db = sqlite3.connect(root / "isolated.sqlite")
            db.executescript("""
                CREATE TABLE contacts(id INTEGER PRIMARY KEY,display_name TEXT);
                CREATE TABLE contact_identifiers(contact_id INTEGER,source TEXT,identifier TEXT);
                CREATE TABLE conversations(id INTEGER PRIMARY KEY,source TEXT,name TEXT,
                    contact_id INTEGER,handle TEXT,is_group INTEGER,
                    UNIQUE(source,name));
                CREATE TABLE messages(id INTEGER PRIMARY KEY,hash TEXT UNIQUE,
                    conversation_id INTEGER,source TEXT,source_guid TEXT,ts TEXT,
                    ts_unix INTEGER,sender TEXT,body TEXT,is_system INTEGER,seq INTEGER);
                CREATE TABLE attachments(id INTEGER PRIMARY KEY,message_id INTEGER,
                    kind TEXT,rel_path TEXT,original_name TEXT,conversation_id INTEGER,ts_unix INTEGER);
                CREATE TABLE imessage_source_assets(rel_path TEXT);
                INSERT INTO contacts VALUES(1,'Fixture Contact');
                INSERT INTO contact_identifiers VALUES(1,'imessage','+15551234567');
            """)
            selected, _, held = classify([parent], db, config)
            self.assertEqual(len(selected), 1)
            self.assertEqual(append_isolated(db, selected, held, stage, original, isolated), (1, 1, 1))
            self.assertEqual(append_isolated(db, selected, held, stage, original, isolated), (0, 0, 0))
            (stage / digest).unlink()
            retained = isolated / digest[:2] / (digest + ".jpg")
            self.assertEqual(retained.read_bytes(), payload)
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM attachments").fetchone()[0], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
