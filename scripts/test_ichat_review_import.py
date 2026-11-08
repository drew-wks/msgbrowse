"""Regression coverage for retained iChat review GUIDs and embedded media."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest

from scripts.ichat_review_import import (
    IChatRecord, REVIEW_NAME, append_records, load_records, retain_images,
)


SCHEMA = """
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY, source TEXT, name TEXT, handle TEXT, is_group INTEGER,
    UNIQUE(source,name)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, hash TEXT UNIQUE, conversation_id INTEGER,
    source TEXT, source_guid TEXT, ts TEXT, ts_unix INTEGER, sender TEXT,
    body TEXT, is_system INTEGER, seq INTEGER
);
CREATE UNIQUE INDEX idx_guid ON messages(source_guid) WHERE source='imessage';
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY, message_id INTEGER, conversation_id INTEGER,
    ts_unix INTEGER, kind TEXT, rel_path TEXT, original_name TEXT
);
CREATE TABLE imessage_attachment_observations (
    id INTEGER PRIMARY KEY, message_id INTEGER, source_attachment_guid TEXT,
    hidden INTEGER, mime_type TEXT,
    UNIQUE(message_id,source_attachment_guid,hidden,mime_type)
);
CREATE TABLE imessage_source_assets (
    id INTEGER PRIMARY KEY, message_id INTEGER, source_attachment_guid TEXT,
    rel_path TEXT, hidden INTEGER, kind TEXT, original_name TEXT,
    UNIQUE(message_id,source_attachment_guid,rel_path)
);
CREATE TABLE imessage_excluded_messages (
    id INTEGER PRIMARY KEY, source_guid TEXT UNIQUE, message_hash TEXT,
    reason TEXT, excluded_at TEXT
);
"""


class IChatReviewImportTests(unittest.TestCase):
    """Exercise keyed parsing and append-only review retention."""

    def test_keyed_record_decodes_without_filename_identity(self) -> None:
        """Read GUID, time, sender, text, and roster from the keyed archive."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "unrelated-name.ichat"
            guid = "11111111-2222-3333-4444-555555555555"
            objects = [
                "$null",
                {"NS.keys": plistlib.UID(2), "NS.objects": plistlib.UID(3)},
                ["PresentityIDs"], [plistlib.UID(4)],
                {"NS.objects": plistlib.UID(5)}, ["owner", "+15551234567"],
                {"GUID": guid, "Flags": 1060865, "Sender": plistlib.UID(7),
                 "MessageText": plistlib.UID(8), "Time": plistlib.UID(9)},
                {"ID": "+15551234567", "AccountID": "owner", "ServiceName": "iMessage"},
                {"NSString": "source text", "NSAttributes": {}},
                {"NS.time": 1000.0},
            ]
            path.write_bytes(plistlib.dumps(
                {"$objects": objects, "$top": {"metadata": plistlib.UID(1)}},
                fmt=plistlib.FMT_BINARY,
            ))
            records = load_records([root], {guid})
            self.assertEqual(len(records[guid]), 1)
            self.assertEqual(records[guid][0].body, "source text")
            self.assertEqual(records[guid][0].sender, "+15551234567")
            self.assertEqual(records[guid][0].participants, ("+15551234567", "owner"))

    def test_repeat_and_removed_source_preserve_messages_media_and_exclusions(self) -> None:
        """A missing later source cannot erase retained bytes or GUID tombstones."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_file = root / "source.ichat"
            source_file.write_bytes(b"replaceable source")
            db = sqlite3.connect(root / "test.sqlite")
            db.executescript(SCHEMA)
            image = b"\xff\xd8\xfffixture\xff\xd9"
            timestamp = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp())
            record = IChatRecord(
                "AAAAAAAA-0000-0000-0000-000000000001", timestamp,
                "+15551234567", "photo \ufffc", 1060865, "owner", "iMessage",
                ("owner", "+15551234567"),
                "BBBBBBBB-0000-0000-0000-000000000001", image, "image/jpeg",
            )
            short = IChatRecord(
                "AAAAAAAA-0000-0000-0000-000000000002", timestamp,
                "12345", "code", 1060865, "owner", "SMS", ("owner", "12345"),
                None, None, None,
            )
            self.assertEqual(retain_images(root, [record]), 1)
            with db:
                first = append_records(db, [record], [short], root, {1060865: False})
            self.assertEqual(first["messages_added"], 1)
            self.assertEqual(first["media_references_added"], 1)
            self.assertEqual(first["short_code_guids_recorded"], 1)
            self.assertEqual(retain_images(root, [record]), 0)
            with db:
                repeat = append_records(db, [record], [short], root, {1060865: False})
            self.assertTrue(all(value == 0 for value in repeat.values()))
            source_file.unlink()
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT name FROM conversations").fetchone()[0], REVIEW_NAME)
            self.assertEqual(db.execute(
                "SELECT source_guid FROM imessage_excluded_messages"
            ).fetchone()[0], short.guid)
            rel = db.execute("SELECT rel_path FROM attachments").fetchone()[0]
            self.assertEqual((root / "media" / "imessage" /
                              rel.removeprefix("permanent/")).read_bytes(), image)
            db.close()


if __name__ == "__main__":
    unittest.main()
