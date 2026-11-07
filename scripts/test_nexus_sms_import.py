"""Synthetic retention checks for the isolated Nexus SMS importer."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET

from scripts.nexus_sms_import import SMS, run, wall_time


class NexusSMSImportTests(unittest.TestCase):
    """Exercise overlap, exclusion, repeat, and omitted-source behavior."""

    def setUp(self) -> None:
        """Create an isolated archive-shaped database and small XML sources."""
        self.temp = tempfile.TemporaryDirectory(prefix="reconstruction-nexus-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "msgbrowse.sqlite"
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text('imessage_exclusions:\n  - name: blocked\n    conversation: "+15550000008"\n')
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
                CREATE VIRTUAL TABLE messages_fts USING fts5(body,content='messages',content_rowid='id');
                CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid,body) VALUES(new.id,new.body);
                END;
                INSERT INTO conversations(id,source,name) VALUES(1,'imessage','+15550000001');
                """
            )
            overlap = SMS(self._date_ms(), "+15550000001", 1, "already archived")
            display, timestamp = wall_time(overlap)
            db.execute(
                "INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,sender,body) "
                "VALUES('apple-hash',1,'imessage','apple-guid',?,?,?,?)",
                (display, timestamp, "+15550000001", overlap.body),
            )
            db.commit()

    @staticmethod
    def _date_ms() -> int:
        """Return a fixed timestamp for synthetic XML messages."""
        return int(datetime(2015, 5, 4, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def _write_xml(self, filename: str, records: list[SMS]) -> Path:
        """Write one SMS Backup & Restore-shaped source file."""
        root = ET.Element("smses", count=str(len(records)))
        for record in records:
            ET.SubElement(
                root,
                "sms",
                date=str(record.date_ms),
                address=record.address,
                type=str(record.kind),
                body=record.body,
            )
        path = self.root / filename
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path

    def test_repeat_and_source_omission_preserve_rows(self) -> None:
        """An overlap is held, rules exclude, and repeat/omission add nothing."""
        date = self._date_ms()
        first = self._write_xml(
            "first.xml",
            [
                SMS(date, "+15550000001", 1, "already archived"),
                SMS(date + 1000, "+15550000002", 1, "retain unique message"),
                SMS(date + 2000, "75243", 1, "transactional code"),
                SMS(date + 3000, "+15550000003", 1, "marketing"),
                SMS(date + 5000, "+15550000008", 1, "blocked"),
                SMS(date + 6000, "+15550000004", 1, "first variant"),
                SMS(date + 7000, "+15550000005", 1, "possible timestamp duplicate"),
            ],
        )
        self._write_xml(
            "second.xml",
            [
                SMS(date + 1000, "5550000002", 1, "retain unique message"),
                SMS(date + 4000, "+15550000003", 2, "STOP"),
                SMS(date + 6000, "+15550000004", 1, "second variant"),
                SMS(date + 37000, "5550000005", 1, "possible timestamp duplicate"),
            ],
        )

        result = run(self.root, self.db_path, self.config_path, apply=True)
        self.assertEqual(result["distinct_sms"], 10)
        self.assertEqual(result["provisional_new"], 1)
        self.assertEqual(result["near_source_duplicate_candidate"], 2)
        self.assertEqual(result["added"], 1)
        self.assertEqual(run(self.root, self.db_path, self.config_path, apply=True)["added"], 0)
        self.assertEqual(run(first, self.db_path, self.config_path, apply=True)["added"], 0)

        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE source='nexus' AND source_guid IS NULL").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'unique'").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_production_path_is_rejected(self) -> None:
        """The import command cannot write outside isolated reconstruction."""
        source = self._write_xml("single.xml", [SMS(self._date_ms(), "+15550000002", 1, "message")])
        with self.assertRaisesRegex(ValueError, "isolated reconstruction"):
            run(source, Path("/Users/example/Documents/Messages/archive/msgbrowse.sqlite"), self.config_path, apply=True)


if __name__ == "__main__":
    unittest.main()
