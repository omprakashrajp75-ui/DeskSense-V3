"""
SQLite storage layer.

Per the project spec, we deliberately do NOT store a row per second - that
would be ~28,800 rows for an 8 hour day for no real benefit. Instead the
scoring engine keeps a rolling in-memory window and this module persists one
summarized row per rollup period (ROLLUP_PERIOD_SECONDS in config.py).
"""
import sqlite3
import os
import logging
from contextlib import contextmanager

logger = logging.getLogger("workspace_ai.storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    avg_posture_score REAL,
    avg_engagement_score REAL,
    engaged_seconds INTEGER,
    distracted_seconds INTEGER,
    away_seconds INTEGER,
    looking_away_seconds INTEGER,
    phone_seconds INTEGER,
    posture_good_seconds INTEGER,
    posture_fair_seconds INTEGER,
    posture_poor_seconds INTEGER,
    sample_count INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_hourly_period_start ON hourly_summary(period_start);

-- One row per pomodoro phase that ended, completed or not. Additive: the
-- hourly_summary table above is untouched, and CREATE TABLE IF NOT EXISTS
-- means an existing database just gains this table on next start.
-- focused_seconds is how many seconds of the phase the engagement tracker
-- classified as "engaged", so focused_seconds/planned_seconds is the
-- session's focus quality.
CREATE TABLE IF NOT EXISTS pomodoro_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    phase TEXT NOT NULL,
    planned_seconds INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    focused_seconds INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pomodoro_start ON pomodoro_sessions(start);
"""


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        logger.info("Database ready at %s", self.db_path)

    def insert_hourly_summary(self, row: dict):
        columns = (
            "period_start", "period_end", "avg_posture_score", "avg_engagement_score",
            "engaged_seconds", "distracted_seconds", "away_seconds",
            "looking_away_seconds", "phone_seconds",
            "posture_good_seconds", "posture_fair_seconds", "posture_poor_seconds",
            "sample_count",
        )
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO hourly_summary ({', '.join(columns)}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(sql, tuple(row.get(c) for c in columns))
        logger.info("Stored hourly summary for period starting %s", row.get("period_start"))

    def get_recent(self, limit: int = 24):
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM hourly_summary ORDER BY period_start DESC LIMIT ?",
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()  # chronological order for charting
        return rows

    def get_today(self):
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM hourly_summary WHERE date(period_start) = date('now', 'localtime') "
                "ORDER BY period_start ASC"
            )
            return [dict(r) for r in cur.fetchall()]

    # ---------- pomodoro ----------

    def insert_pomodoro_session(self, row: dict):
        columns = ("start", "end", "phase", "planned_seconds", "completed", "focused_seconds")
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO pomodoro_sessions ({', '.join(columns)}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(sql, tuple(row.get(c) for c in columns))
        logger.info("Stored pomodoro session: %s (%s)", row.get("phase"),
                    "completed" if row.get("completed") else "ended early")

    def count_pomodoro_completed_today(self) -> int:
        """Completed focus phases on the local calendar day. Breaks are
        excluded - "sessions today" means focus sessions."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) AS n FROM pomodoro_sessions "
                "WHERE phase = 'focus' AND completed = 1 "
                "AND date(start) = date('now', 'localtime')"
            )
            row = cur.fetchone()
        return int(row["n"]) if row else 0

    def get_last_completed_focus_session(self):
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM pomodoro_sessions WHERE phase = 'focus' AND completed = 1 "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_daily_summary(self, days: int = 7, tz: str = "Asia/Kolkata"):
        """Groups every row by its local calendar day and returns the most
        recent `days` calendar days that actually have a row - a day with
        zero rows is simply absent from the result, never zero-filled, so a
        gap in usage doesn't get plotted as a bad day."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tzinfo = ZoneInfo(tz)
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT period_start, avg_posture_score, avg_engagement_score "
                "FROM hourly_summary ORDER BY period_start ASC"
            )
            rows = cur.fetchall()

        by_day = {}
        for r in rows:
            local_date = datetime.fromisoformat(r["period_start"]).astimezone(tzinfo).date().isoformat()
            bucket = by_day.setdefault(local_date, {"p_sum": 0.0, "p_n": 0, "e_sum": 0.0, "e_n": 0})
            if r["avg_posture_score"] is not None:
                bucket["p_sum"] += r["avg_posture_score"]
                bucket["p_n"] += 1
            if r["avg_engagement_score"] is not None:
                bucket["e_sum"] += r["avg_engagement_score"]
                bucket["e_n"] += 1

        all_dates = sorted(by_day)
        date_range = {"first": all_dates[0], "last": all_dates[-1]} if all_dates else None

        daily = []
        for d in all_dates[-days:]:
            b = by_day[d]
            daily.append({
                "date": d,
                "avg_posture_score": (b["p_sum"] / b["p_n"]) if b["p_n"] else None,
                "avg_engagement_score": (b["e_sum"] / b["e_n"]) if b["e_n"] else None,
            })

        return {
            "days": daily,
            "total_days_recorded": len(all_dates),
            "date_range": date_range,
        }
