import json
import sqlite3
import datetime


class StateManager:
    def __init__(self, db_path="state.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS requests (
                    url TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_checked TIMESTAMP,
                    details TEXT,
                    facts TEXT
                )
            """)
            # Migrate older DBs that predate the facts column.
            existing = [
                row[1]
                for row in cursor.execute("PRAGMA table_info(requests)").fetchall()
            ]
            if "facts" not in existing:
                cursor.execute("ALTER TABLE requests ADD COLUMN facts TEXT")
            conn.commit()

    def update_status(self, url, status, details="", facts=None):
        """
        Upsert the record for url. When facts is None the stored facts snapshot
        is left untouched (so callers that only update status don't wipe it).
        """
        facts_json = json.dumps(facts, sort_keys=True) if facts is not None else None
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if facts_json is None:
                cursor.execute(
                    """
                    INSERT INTO requests (url, status, last_checked, details)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        status=excluded.status,
                        last_checked=excluded.last_checked,
                        details=excluded.details
                """,
                    (url, status, now, details),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO requests (url, status, last_checked, details, facts)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        status=excluded.status,
                        last_checked=excluded.last_checked,
                        details=excluded.details,
                        facts=excluded.facts
                """,
                    (url, status, now, details, facts_json),
                )
            conn.commit()

    def get_status(self, url):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, details FROM requests WHERE url = ?", (url,))
            return cursor.fetchone()

    def get_facts(self, url):
        """Return the stored facts snapshot for url as a dict, or None."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT facts FROM requests WHERE url = ?", (url,)
            ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None
