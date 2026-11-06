# -*- coding: utf-8 -*-
"""SVG 活动任务去重和旧数据库迁移测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
import server


class SVGQueueTests(unittest.TestCase):
    def test_duplicate_active_relay_is_not_inserted_twice(self):
        con = sqlite3.connect(":memory:")
        con.execute(
            """CREATE TABLE svg_generations(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   relay TEXT NOT NULL, relay_label TEXT, model_id TEXT,
                   status TEXT NOT NULL, trigger TEXT NOT NULL, requested_at TEXT NOT NULL)"""
        )
        con.execute(
            """CREATE UNIQUE INDEX ux_svg_generations_active_relay
               ON svg_generations(relay) WHERE status IN ('queued','generating')"""
        )
        relay = {"name": "relay-a", "label": "渠道 A", "model": "model-a"}

        first_id = server._insert_svg_generation(con, relay, "manual", "2026-10-22T10:00:00")
        duplicate_id = server._insert_svg_generation(con, relay, "manual", "2026-10-22T10:00:01")

        self.assertEqual(first_id, 1)
        self.assertIsNone(duplicate_id)
        self.assertEqual(
            con.execute("SELECT COUNT(*) FROM svg_generations WHERE status='queued'").fetchone()[0],
            1,
        )
        con.close()

    def test_database_migration_closes_legacy_duplicate_active_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            db_path = data_dir / "history.db"
            con = sqlite3.connect(db_path)
            con.execute(
                """CREATE TABLE svg_generations(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       relay TEXT NOT NULL, relay_label TEXT, model_id TEXT,
                       status TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT 'manual',
                       svg_content TEXT, error_detail TEXT, requested_at TEXT NOT NULL,
                       started_at TEXT, completed_at TEXT)"""
            )
            con.executemany(
                """INSERT INTO svg_generations(relay, relay_label, model_id, status, requested_at)
                   VALUES('relay-a', '渠道 A', 'model-a', 'queued', ?)""",
                [("2026-10-22T10:00:00",), ("2026-10-22T10:00:01",)],
            )
            con.commit()
            con.close()

            with patch.object(monitor, "DATA_DIR", data_dir), patch.object(monitor, "DB_PATH", db_path):
                con = monitor.init_db()
                rows = con.execute(
                    "SELECT id, status FROM svg_generations ORDER BY id"
                ).fetchall()
                index = con.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='index' AND name='ux_svg_generations_active_relay'"""
                ).fetchone()
                con.close()

        self.assertEqual(rows, [(1, "failed"), (2, "queued")])
        self.assertIsNotNone(index)


if __name__ == "__main__":
    unittest.main()
