"""Regression checks for held legacy MMS media and Apple augmentation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.legacy_mms_gap import append_isolated, archive_matches
from scripts.legacy_mms_import import Asset, LOCAL_ZONE, Parent


class LegacyMMSGapTest(unittest.TestCase):
    """Keep held media and avoid a third visible copy of an Apple message."""

    def test_review_media_and_inline_augmentation_survive_source_deletion(self) -> None:
        """Repeat the import, then remove disposable source bytes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            media = root / "media"
            staging.mkdir()
            db = sqlite3.connect(":memory:")
            db.executescript("""
                CREATE TABLE conversations(id INTEGER PRIMARY KEY,source TEXT,name TEXT,
                    handle TEXT,contact_id INTEGER,is_group INTEGER,UNIQUE(source,name));
                CREATE TABLE messages(id INTEGER PRIMARY KEY,hash TEXT UNIQUE,
                    conversation_id INTEGER,source TEXT,source_guid TEXT,ts TEXT,
                    ts_unix INTEGER,sender TEXT,body TEXT,is_system INTEGER,seq INTEGER);
                CREATE TABLE attachments(id INTEGER PRIMARY KEY,message_id INTEGER,
                    kind TEXT,rel_path TEXT,original_name TEXT,conversation_id INTEGER,
                    ts_unix INTEGER);
                INSERT INTO conversations VALUES(1,'imessage','fixture','+15551234567',NULL,0);
            """)
            subject = Parent(1_288_000_000, 2, "+15551234567", "Sub",
                             "fixture body", ())
            timestamp = int(datetime.fromtimestamp(subject.date, LOCAL_ZONE)
                            .replace(tzinfo=timezone.utc).timestamp())
            original_hash = hashlib.sha256(b"existing image").hexdigest()
            for message_id in (1, 2):
                db.execute("INSERT INTO messages(id,hash,conversation_id,source,ts,ts_unix,"
                           "sender,body,is_system,seq) VALUES(?, ?,1,'imessage','',?,"
                           "'+15551234567','fixture body!',0,0)",
                           (message_id, f"apple-{message_id}", timestamp))
                db.execute("INSERT INTO attachments(message_id,kind,rel_path,original_name,"
                           "conversation_id,ts_unix) VALUES(?,'image',?,'fixture.jpg',1,?)",
                           (message_id, "permanent/aa/" + original_hash + ".jpg", timestamp))
            payloads = [b"review one", b"review two", b"review three", b"inline preview"]
            digests = [hashlib.sha256(payload).hexdigest() for payload in payloads]
            for digest, payload in zip(digests, payloads):
                (staging / digest).write_bytes(payload)
            review = [Parent(subject.date + index + 1, flag, f"+1555123456{index}",
                             "", "", (Asset(0, "indexed_file", digests[index],
                                           "image/jpeg", len(payloads[index]), -1, False),))
                      for index, flag in enumerate((3, 35, 33))]
            subject = Parent(subject.date, 2, subject.handle, "Sub", "fixture body", (
                Asset(1, "indexed_file", original_hash, "image/jpeg", 14, -1, True),
                Asset(1, "inline", digests[3], "image/png", len(payloads[3]), 0, False),
            ))
            apple_ids = archive_matches(subject, db)
            self.assertEqual(apple_ids, (1, 2))
            self.assertEqual(append_isolated(db, review, subject, apple_ids, staging, media),
                             (3, 5, 4))
            self.assertEqual(append_isolated(db, review, subject, apple_ids, staging, media),
                             (0, 0, 0))
            for path in staging.iterdir():
                path.unlink()
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT count(*) FROM attachments").fetchone()[0], 7)
            for digest, payload in zip(digests, payloads):
                extension = ".png" if digest == digests[3] else ".jpg"
                self.assertEqual((media / digest[:2] / (digest + extension)).read_bytes(),
                                 payload)
            db.close()


if __name__ == "__main__":
    unittest.main()
