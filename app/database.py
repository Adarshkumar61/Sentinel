import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "surveillance.db"


def connection():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def initialise():
    with connection() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                camera TEXT NOT NULL,
                details TEXT,
                severity TEXT NOT NULL DEFAULT 'warning',
                image_path TEXT,
                body_image_path TEXT
            )"""
        )
        # Safe migration for databases created before event-photo support.
        columns = {row[1] for row in con.execute("PRAGMA table_info(events)")}
        if "image_path" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN image_path TEXT")
        if "body_image_path" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN body_image_path TEXT")
        con.execute("""CREATE TABLE IF NOT EXISTS source_zones (
            source_key TEXT PRIMARY KEY, zone_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")


def load_zone(source_key: str):
    with connection() as con:
        row = con.execute("SELECT zone_json FROM source_zones WHERE source_key = ?", (source_key,)).fetchone()
    return json.loads(row[0]) if row else []


def save_zone(source_key: str, zone: list):
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with connection() as con:
        con.execute("""INSERT INTO source_zones (source_key, zone_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET zone_json=excluded.zone_json, updated_at=excluded.updated_at""",
            (source_key, json.dumps(zone), now))


def clear_zone(source_key: str):
    with connection() as con:
        con.execute("DELETE FROM source_zones WHERE source_key = ?", (source_key,))


def add_event(event_type: str, camera: str, details: str, severity: str = "warning", image_path: str | None = None, body_image_path: str | None = None):
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with connection() as con:
        con.execute(
            "INSERT INTO events (created_at, event_type, camera, details, severity, image_path, body_image_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, event_type, camera, details, severity, image_path, body_image_path),
        )


def recent_events(limit: int = 30):
    with connection() as con:
        rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def total_events():
    with connection() as con:
        return con.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def clear_events():
    """Delete alert records and return their optional evidence image paths."""
    with connection() as con:
        image_paths = [path for row in con.execute("SELECT image_path, body_image_path FROM events") for path in row if path]
        con.execute("DELETE FROM events")
    return image_paths
