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
        if "event_id" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
            con.execute("UPDATE events SET event_id = 'SENT-LEGACY-' || id WHERE event_id IS NULL")
        if "evidence_hash" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN evidence_hash TEXT")
        if "blockchain_tx" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN blockchain_tx TEXT")
        if "blockchain_status" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN blockchain_status TEXT")
        if "blockchain_event_id" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN blockchain_event_id TEXT")
        if "verification_status" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN verification_status TEXT")
        if "blockchain_registered_at" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN blockchain_registered_at TEXT")
        if "registered_by" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN registered_by TEXT")
        if "block_number" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN block_number INTEGER")
        if "contract_address" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN contract_address TEXT")
        if "blockchain_network" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN blockchain_network TEXT")
        if "blockchain_error" not in columns:
            con.execute("ALTER TABLE events ADD COLUMN blockchain_error TEXT")
        con.execute("""CREATE TABLE IF NOT EXISTS source_zones (
            source_key TEXT PRIMARY KEY, zone_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        # Face encodings are deliberately kept server-side.  A person may have
        # multiple enrolment photos, so encodings are normalized instead of
        # adding columns to the existing events table.
        con.execute("""CREATE TABLE IF NOT EXISTS authorized_persons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS authorized_face_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            encoding_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(person_id) REFERENCES authorized_persons(id) ON DELETE CASCADE
        )""")


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_authorized_persons(include_inactive: bool = True):
    query = """SELECT p.id, p.name, p.active, p.created_at, p.updated_at,
                      COUNT(e.id) AS image_count
               FROM authorized_persons p
               LEFT JOIN authorized_face_embeddings e ON e.person_id = p.id"""
    if not include_inactive:
        query += " WHERE p.active = 1"
    query += " GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
    with connection() as con:
        return [dict(row) for row in con.execute(query).fetchall()]


def get_authorized_faces():
    """Return active encodings for the in-memory recognizer only."""
    with connection() as con:
        rows = con.execute("""SELECT e.id, p.id AS person_id, p.name, e.encoding_json
                            FROM authorized_face_embeddings e
                            JOIN authorized_persons p ON p.id = e.person_id
                            WHERE p.active = 1""").fetchall()
    return [
        {"id": row["id"], "person_id": row["person_id"], "name": row["name"],
         "encoding": json.loads(row["encoding_json"])}
        for row in rows
    ]


def add_authorized_person(name: str, encodings: list[list[float]]):
    now = _now()
    with connection() as con:
        cursor = con.execute(
            "INSERT INTO authorized_persons (name, active, created_at, updated_at) VALUES (?, 1, ?, ?)",
            (name, now, now),
        )
        person_id = cursor.lastrowid
        con.executemany(
            "INSERT INTO authorized_face_embeddings (person_id, encoding_json, created_at) VALUES (?, ?, ?)",
            [(person_id, json.dumps(encoding, separators=(",", ":")), now) for encoding in encodings],
        )
    return person_id


def add_authorized_embeddings(person_id: int, encodings: list[list[float]]):
    now = _now()
    with connection() as con:
        exists = con.execute("SELECT 1 FROM authorized_persons WHERE id = ?", (person_id,)).fetchone()
        if not exists:
            return False
        con.executemany(
            "INSERT INTO authorized_face_embeddings (person_id, encoding_json, created_at) VALUES (?, ?, ?)",
            [(person_id, json.dumps(encoding, separators=(",", ":")), now) for encoding in encodings],
        )
        con.execute("UPDATE authorized_persons SET updated_at = ? WHERE id = ?", (now, person_id))
    return True


def update_authorized_person(person_id: int, name: str | None = None, active: bool | None = None):
    changes, values = [], []
    if name is not None:
        changes.append("name = ?")
        values.append(name)
    if active is not None:
        changes.append("active = ?")
        values.append(int(active))
    if not changes:
        return False
    changes.append("updated_at = ?")
    values.append(_now())
    values.append(person_id)
    with connection() as con:
        result = con.execute(f"UPDATE authorized_persons SET {', '.join(changes)} WHERE id = ?", values)
    return result.rowcount == 1


def delete_authorized_face(person_id: int):
    with connection() as con:
        con.execute("DELETE FROM authorized_face_embeddings WHERE person_id = ?", (person_id,))
        result = con.execute("DELETE FROM authorized_persons WHERE id = ?", (person_id,))
    return result.rowcount == 1


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


def add_event(event_type: str, camera: str, details: str, severity: str = "warning", image_path: str | None = None, body_image_path: str | None = None, event_id: str | None = None, evidence_hash: str | None = None, created_at: str | None = None):
    now = created_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with connection() as con:
        con.execute(
            "INSERT INTO events (created_at, event_type, camera, details, severity, image_path, body_image_path, event_id, evidence_hash, blockchain_status, verification_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, event_type, camera, details, severity, image_path, body_image_path, event_id, evidence_hash, "PENDING", "PENDING"),
        )
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_event(event_id: str):
    with connection() as con:
        row = con.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def confirm_blockchain_event(event_id: str, transaction_hash: str, blockchain_event_id: str, registered_at: str, registered_by: str, block_number: int | None = None, contract_address: str | None = None, blockchain_network: str | None = None):
    with connection() as con:
        con.execute(
            "UPDATE events SET blockchain_tx = ?, blockchain_event_id = ?, blockchain_status = 'CONFIRMED', verification_status = 'VERIFIED', blockchain_registered_at = ?, registered_by = ?, block_number = ?, contract_address = ?, blockchain_network = ?, blockchain_error = NULL WHERE event_id = ?",
            (transaction_hash, blockchain_event_id, registered_at, registered_by, block_number, contract_address, blockchain_network, event_id),
        )


def mark_blockchain_submitted(event_id: str, transaction_hash: str):
    """Persist a user-reported tx only as pending; it is never treated as proof."""
    with connection() as con:
        con.execute(
            "UPDATE events SET blockchain_tx = ?, blockchain_status = 'PENDING', verification_status = 'PENDING' WHERE event_id = ?",
            (transaction_hash, event_id),
        )


def claim_blockchain_registration(event_id: str) -> bool:
    """Atomically claim an unsent event so duplicate worker jobs cannot sign twice."""
    with connection() as con:
        result = con.execute(
            "UPDATE events SET blockchain_status = 'REGISTERING', blockchain_error = NULL WHERE event_id = ? AND blockchain_tx IS NULL AND blockchain_status IN ('PENDING', 'FAILED')",
            (event_id,),
        )
    return result.rowcount == 1


def mark_blockchain_failed(event_id: str, message: str):
    with connection() as con:
        con.execute(
            "UPDATE events SET blockchain_status = 'FAILED', verification_status = 'PENDING', blockchain_error = ? WHERE event_id = ? AND verification_status != 'VERIFIED'",
            (message[:300], event_id),
        )


def pending_blockchain_events(limit: int = 200):
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM events WHERE verification_status != 'VERIFIED' AND blockchain_status IN ('PENDING', 'FAILED', 'REGISTERING') ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def recover_interrupted_registrations():
    with connection() as con:
        con.execute("UPDATE events SET blockchain_status = 'PENDING' WHERE blockchain_status = 'REGISTERING' AND blockchain_tx IS NULL")


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
