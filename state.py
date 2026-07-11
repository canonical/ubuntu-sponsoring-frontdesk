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
            # Rule B (design_journal.md #66): the aggregated feedback posted
            # when an item was bounced, so the sweep can later ask the LLM
            # whether a contributor's response addresses it.
            if "bounce_reason" not in existing:
                cursor.execute("ALTER TABLE requests ADD COLUMN bounce_reason TEXT")
            conn.commit()

    def update_status(self, url, status, details="", facts=None, bounce_reason=None):
        """
        Upsert the record for url. When facts is None the stored facts snapshot
        is left untouched (so callers that only update status don't wipe it);
        same contract for bounce_reason.
        """
        facts_json = json.dumps(facts, sort_keys=True) if facts is not None else None
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            assignments = {
                "status": status,
                "last_checked": now,
                "details": details,
            }
            if facts_json is not None:
                assignments["facts"] = facts_json
            if bounce_reason is not None:
                assignments["bounce_reason"] = bounce_reason
            columns = ["url"] + list(assignments)
            updates = ", ".join(f"{col}=excluded.{col}" for col in assignments)
            cursor.execute(
                f"""
                INSERT INTO requests ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                ON CONFLICT(url) DO UPDATE SET {updates}
                """,
                [url] + list(assignments.values()),
            )
            conn.commit()

    def bounced_bugs(self):
        """(url, bounce_reason) for every bug this bot bounced and is still
        waiting on -- Rule B's sweep population (design_journal.md #66).
        MPs are excluded: their lifecycle is a new push changing the facts
        fingerprint, not a task-status round trip."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT url, bounce_reason FROM requests "
                "WHERE status = 'WAITING_ON_CONTRIBUTOR'"
            ).fetchall()
        return [(url, reason) for url, reason in rows if "+merge/" not in url]

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
