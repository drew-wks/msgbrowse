"""Regression checks for reviewed legacy SMS recovery and retention."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.legacy_sms_gap_import import (
    LegacySMS, append_isolated, canonical_address, classify, load_records,
)
from scripts.legacy_sms_gap_checked_merge import merge_rows


SCHEMA = """
CREATE TABLE conversations(id INTEGER PRIMARY KEY,source TEXT,name TEXT,
    contact_id INTEGER,handle TEXT,is_group INTEGER,UNIQUE(source,name));
CREATE TABLE messages(id INTEGER PRIMARY KEY,hash TEXT UNIQUE,conversation_id INTEGER,
    source TEXT,source_guid TEXT,ts TEXT,ts_unix INTEGER,sender TEXT,body TEXT,
    is_system INTEGER,seq INTEGER);
"""


class LegacySMSGapTest(unittest.TestCase):
    """Check source overlap, review labeling, and repeated merge behavior."""

    def test_review_import_is_repeatable_and_source_independent(self) -> None:
        """Keep a distinct SMS after its disposable source is removed."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            manifest: list[dict[str, object]] = []
            for number in range(6):
                folder = source / f"snapshot-{number}"
                folder.mkdir()
                db_path = folder / "sms.db"
                with sqlite3.connect(db_path) as db:
                    db.executescript("""
                        CREATE TABLE message(address TEXT,date INTEGER,flags INTEGER,
                            text TEXT,group_id INTEGER);
                        CREATE TABLE group_member(group_id INTEGER,address TEXT);
                        INSERT INTO group_member VALUES(1,'+15551234567');
                        INSERT INTO message VALUES('+1 (555) 123-4567',1288000000,2,
                            'a distinct old text',1);
                    """)
                payload = db_path.read_bytes()
                manifest.append({"snapshot": folder.name,
                                 "file": f"{folder.name}/sms.db", "bytes": len(payload),
                                 "sha256": hashlib.sha256(payload).hexdigest()})
            (source / "verified-copy-manifest.json").write_text(
                json.dumps({"files": manifest, "total_bytes": sum(
                    path.stat().st_size for path in source.glob("*/sms.db"))}),
                encoding="utf-8"
            )
            records = load_records(source)
            self.assertEqual(len(records), 1)
            self.assertEqual(canonical_address("+1 (555) 123-4567"), "+15551234567")
            self.assertEqual(canonical_address("+44 (20) 1234-5678"), "+442012345678")
            config = root / "config.yaml"
            config.write_text("", encoding="utf-8")
            isolated = sqlite3.connect(root / "isolated.sqlite")
            target = sqlite3.connect(root / "target.sqlite")
            isolated.executescript(SCHEMA)
            target.executescript(SCHEMA)
            selected, reasons = classify(records, target, config)
            self.assertEqual(reasons["review_addition"], 1)
            self.assertEqual(append_isolated(isolated, selected), 1)
            self.assertEqual(append_isolated(isolated, selected), 0)
            self.assertEqual(merge_rows(isolated, target), 1)
            self.assertEqual(merge_rows(isolated, target), 0)
            for db_path in source.glob("*/sms.db"):
                db_path.unlink()
            self.assertEqual(target.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(target.execute(
                "SELECT source,name FROM conversations"
            ).fetchone(), ("legacy_sms_review", "Review legacy SMS: +15551234567"))
            isolated.close()
            target.close()

    def test_near_variant_and_short_code_remain_outside_review(self) -> None:
        """Keep a close body variant and a transactional short code held."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = sqlite3.connect(":memory:")
            db.executescript(SCHEMA)
            record = LegacySMS(1_288_000_000, "+15551234567", 2,
                               "A sample old message", frozenset(("+15551234567",)))
            _, timestamp = record.wall_time
            db.execute("INSERT INTO conversations(source,name,handle,is_group) "
                       "VALUES('imessage','fixture','+15551234567',0)")
            db.execute("INSERT INTO messages(hash,conversation_id,source,ts,ts_unix,"
                       "sender,body,is_system,seq) VALUES('fixture',1,'imessage','',?,"
                       "'+15551234567','A sample old message!',0,0)", (timestamp,))
            short = LegacySMS(1_288_000_000, "12345", 2, "Your code is 123456",
                              frozenset(("12345",)))
            config = root / "config.yaml"
            config.write_text("", encoding="utf-8")
            selected, reasons = classify([record, short], db, config)
            self.assertEqual(selected, [])
            self.assertEqual(reasons["possible_text_variant"], 1)
            self.assertEqual(reasons["short_code"], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
