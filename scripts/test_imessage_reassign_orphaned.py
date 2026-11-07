"""Synthetic regression tests for metadata-only orphan reassignment."""

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from scripts.imessage_reassign_orphaned import run


def make_fixture(root: Path) -> tuple[Path, Path]:
    """Create source and archive databases without real message content."""

    archive_path = root / "archive.sqlite"
    source_path = root / "chat.db"
    with closing(sqlite3.connect(archive_path)) as db:
        db.executescript(
            """
            CREATE TABLE conversations(
                id INTEGER PRIMARY KEY, source TEXT, name TEXT,
                is_group INTEGER DEFAULT 0, handle TEXT DEFAULT ''
            );
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY, hash TEXT, conversation_id INTEGER,
                source TEXT, source_guid TEXT
            );
            CREATE TABLE attachments(
                id INTEGER PRIMARY KEY, message_id INTEGER,
                conversation_id INTEGER, rel_path TEXT
            );
            CREATE TABLE links(
                id INTEGER PRIMARY KEY, message_id INTEGER,
                conversation_id INTEGER, url TEXT
            );
            CREATE TABLE reactions(
                id INTEGER PRIMARY KEY, message_hash TEXT,
                conversation_id INTEGER, source TEXT
            );
            INSERT INTO conversations(id,source,name) VALUES
                (1,'imessage','orphaned'),(2,'imessage','Known'),
                (3,'imessage','Exact');
            INSERT INTO messages(id,hash,conversation_id,source,source_guid) VALUES
                (10,'h-known',2,'imessage','g-known'),
                (11,'h-one',1,'imessage','g-one'),
                (12,'h-new',1,'imessage','g-new'),
                (13,'h-ambiguous',1,'imessage','g-ambiguous'),
                (14,'h-exact',1,'imessage','g-exact'),
                (15,'h-missing',1,'imessage','g-missing');
            INSERT INTO attachments VALUES (1,11,1,'permanent/file.jpg');
            INSERT INTO links VALUES (1,12,1,'https://example.invalid');
            INSERT INTO reactions VALUES (1,'h-exact',1,'imessage');
            """
        )
        db.commit()
    with closing(sqlite3.connect(source_path)) as db:
        db.executescript(
            """
            CREATE TABLE message(ROWID INTEGER PRIMARY KEY, guid TEXT);
            CREATE TABLE chat(
                ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, room_name TEXT
            );
            CREATE TABLE chat_message_join(chat_id INTEGER, message_id INTEGER);
            CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
            INSERT INTO chat VALUES
                (10,'Known',NULL),(20,'+15551234567',NULL),
                (30,'chat-30',NULL),(31,'chat-31',NULL),
                (40,'Exact',NULL);
            INSERT INTO message VALUES
                (1,'g-known'),(2,'g-one'),(3,'g-new'),
                (4,'g-ambiguous'),(5,'g-exact');
            INSERT INTO chat_message_join VALUES
                (10,1),(10,2),(20,3),(30,4),(31,4),(40,5);
            """
        )
        db.commit()
    return source_path, archive_path


class ReassignOrphanedTests(unittest.TestCase):
    """Verify dry-run, checked move, related rows, and idempotency."""

    def test_reassign_preserves_rows_and_skips_ambiguous_source(self) -> None:
        """Move only GUIDs with a unique source chat and verified destination."""

        with tempfile.TemporaryDirectory() as folder:
            source, archive = make_fixture(Path(folder))
            preview = run(source, archive, apply_changes=False)
            self.assertEqual(len(preview.moves), 3)
            self.assertEqual(
                preview.skipped["missing_or_ambiguous_source_chat"], 2
            )
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM messages WHERE conversation_id=1"
                    ).fetchone()[0],
                    5,
                )

            with self.assertRaisesRegex(ValueError, "expected 4"):
                run(source, archive, apply_changes=True, expected_moves=4)
            applied = run(source, archive, apply_changes=True, expected_moves=3)
            self.assertEqual(len(applied.moves), 3)
            with closing(sqlite3.connect(archive)) as db:
                moved = dict(
                    db.execute(
                        "SELECT source_guid,conversation_id FROM messages"
                    ).fetchall()
                )
                self.assertEqual(moved["g-new"], 4)
                self.assertEqual(moved["g-one"], 2)
                self.assertEqual(moved["g-exact"], 3)
                self.assertEqual(moved["g-ambiguous"], 1)
                self.assertEqual(
                    db.execute("SELECT conversation_id FROM links").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    db.execute("SELECT rel_path FROM attachments").fetchone()[0],
                    "permanent/file.jpg",
                )
                self.assertEqual(
                    db.execute("SELECT conversation_id FROM attachments").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    db.execute("SELECT conversation_id FROM reactions").fetchone()[0],
                    3,
                )
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 6)
            self.assertEqual(len(run(source, archive, apply_changes=True).moves), 0)

    def test_related_row_mismatch_rolls_back(self) -> None:
        """Reject a move if denormalized attachment metadata is already wrong."""

        with tempfile.TemporaryDirectory() as folder:
            source, archive = make_fixture(Path(folder))
            with closing(sqlite3.connect(archive)) as db:
                db.execute("UPDATE attachments SET conversation_id=2 WHERE id=1")
                db.commit()
            with self.assertRaisesRegex(ValueError, "attachments has"):
                run(source, archive, apply_changes=True)
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT conversation_id FROM messages WHERE id=12"
                    ).fetchone()[0],
                    1,
                )

    def test_shared_identifier_uses_only_unique_archived_chat(self) -> None:
        """A shared chat label cannot mask a unique GUID-linked chat."""

        with tempfile.TemporaryDirectory() as folder:
            source, archive = make_fixture(Path(folder))
            with closing(sqlite3.connect(source)) as db:
                db.execute(
                    "INSERT INTO chat(ROWID,chat_identifier) VALUES(11,'Known')"
                )
                db.execute("INSERT INTO message VALUES(6,'g-unproven')")
                db.execute("INSERT INTO chat_message_join VALUES(11,6)")
                db.commit()
            with closing(sqlite3.connect(archive)) as db:
                db.execute(
                    "INSERT INTO messages VALUES"
                    "(16,'h-unproven',1,'imessage','g-unproven')"
                )
                db.commit()

            preview = run(source, archive, apply_changes=False)
            self.assertIn("g-one", {move.guid for move in preview.moves})
            self.assertNotIn("g-unproven", {move.guid for move in preview.moves})
            run(source, archive, apply_changes=True, expected_moves=3)
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(
                    dict(
                        db.execute(
                            "SELECT source_guid,conversation_id FROM messages"
                        )
                    )["g-one"],
                    2,
                )
                self.assertEqual(
                    dict(
                        db.execute(
                            "SELECT source_guid,conversation_id FROM messages"
                        )
                    )["g-unproven"],
                    1,
                )

    def test_multiple_sources_skip_conflicting_chat_links(self) -> None:
        """Keep a GUID orphaned when source snapshots disagree on its chat."""

        with tempfile.TemporaryDirectory() as folder:
            source, archive = make_fixture(Path(folder))
            second = Path(folder) / "second.db"
            shutil.copyfile(source, second)
            with closing(sqlite3.connect(second)) as db:
                db.execute(
                    "UPDATE chat_message_join SET chat_id=40 WHERE message_id=2"
                )
                db.commit()
            preview = run([source, second], archive, apply_changes=False)
            self.assertEqual(len(preview.moves), 2)
            self.assertEqual(preview.skipped["conflicting_source_destinations"], 1)
            run([source, second], archive, apply_changes=True, expected_moves=2)
            with closing(sqlite3.connect(archive)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT conversation_id FROM messages WHERE source_guid='g-one'"
                    ).fetchone()[0],
                    1,
                )

    def test_hidden_source_conflict_blocks_other_snapshot_move(self) -> None:
        """A skipped source plan still vetoes a contradictory destination."""

        with tempfile.TemporaryDirectory() as folder:
            source, archive = make_fixture(Path(folder))
            second = Path(folder) / "second.db"
            shutil.copyfile(source, second)
            with closing(sqlite3.connect(archive)) as db:
                db.execute(
                    "INSERT INTO messages VALUES"
                    "(16,'h-second',3,'imessage','g-second')"
                )
                db.commit()
            with closing(sqlite3.connect(second)) as db:
                db.execute(
                    "UPDATE chat_message_join SET chat_id=40 WHERE message_id=2"
                )
                db.execute("UPDATE chat SET chat_identifier='Known' WHERE ROWID=40")
                db.execute("INSERT INTO message VALUES(6,'g-second')")
                db.execute("INSERT INTO chat_message_join VALUES(40,6)")
                db.commit()

            preview = run([source, second], archive, apply_changes=False)
            self.assertNotIn("g-one", {move.guid for move in preview.moves})
            self.assertEqual(preview.skipped["conflicting_source_destinations"], 1)


if __name__ == "__main__":
    unittest.main()
