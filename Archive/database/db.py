# ============================================================
# database/db.py — SQLite database manager (thread-safe)
# ============================================================
import sqlite3
import threading
import json
import os
from datetime import datetime, timedelta
from utils.logger import log


class Database:
    """
    Thread-safe SQLite wrapper.
    Uses WAL mode and a per-call connection to avoid threading issues.
    A module-level lock serialises all write operations.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_schema()
        log("REGION", f"Database initialised at {db_path}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS bookings (
                    booking_id      TEXT PRIMARY KEY,
                    driver_id       TEXT NOT NULL,
                    origin          TEXT NOT NULL,
                    destination     TEXT NOT NULL,
                    departure_time  TEXT NOT NULL,
                    home_region     TEXT NOT NULL,
                    status          TEXT DEFAULT 'CONFIRMED',
                    version         INTEGER DEFAULT 1,
                    is_cross_region INTEGER DEFAULT 0,
                    transaction_id  TEXT,
                    route_path      TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS peers (
                    region_name          TEXT PRIMARY KEY,
                    host                 TEXT NOT NULL,
                    port                 INTEGER NOT NULL,
                    status               TEXT DEFAULT 'ALIVE',
                    cities               TEXT DEFAULT '[]',
                    gateway_city         TEXT,
                    consecutive_failures INTEGER DEFAULT 0,
                    last_seen            TEXT
                );

                CREATE TABLE IF NOT EXISTS held_transactions (
                    transaction_id    TEXT PRIMARY KEY,
                    booking_id        TEXT,
                    coordinator_region TEXT,
                    role              TEXT,
                    phase             TEXT DEFAULT 'PREPARE',
                    created_at        TEXT NOT NULL,
                    expires_at        TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    details    TEXT,
                    created_at TEXT NOT NULL
                );
            """)

    # ------------------------------------------------------------------
    # Booking operations
    # ------------------------------------------------------------------
    def insert_booking(self, booking):
        now = datetime.utcnow().isoformat()
        with self._write_lock:
            with self._conn() as c:
                c.execute("""
                    INSERT INTO bookings
                        (booking_id, driver_id, origin, destination, departure_time,
                         home_region, status, version, is_cross_region,
                         transaction_id, route_path, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    booking.booking_id, booking.driver_id,
                    booking.origin, booking.destination,
                    booking.departure_time.isoformat()
                    if hasattr(booking.departure_time, "isoformat")
                    else booking.departure_time,
                    booking.home_region, booking.status, booking.version,
                    1 if booking.is_cross_region else 0,
                    booking.transaction_id,
                    json.dumps(booking.route_path) if booking.route_path else None,
                    now, now,
                ))

    def update_booking_status(self, booking_id: str, status: str):
        now = datetime.utcnow().isoformat()
        with self._write_lock:
            with self._conn() as c:
                c.execute("""
                    UPDATE bookings
                    SET status=?, version=version+1, updated_at=?
                    WHERE booking_id=?
                """, (status, now, booking_id))

    def get_booking(self, booking_id: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM bookings WHERE booking_id=?", (booking_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_bookings(self, status: str = None):
        with self._conn() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM bookings WHERE status=? ORDER BY created_at DESC", (status,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM bookings ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def check_conflict(self, origin: str, destination: str,
                       departure_time_iso: str, exclude_id: str = None) -> bool:
        """Return True if there's already a CONFIRMED/HELD booking for same route ±30 min."""
        with self._conn() as c:
            rows = c.execute("""
                SELECT 1 FROM bookings
                WHERE origin=? AND destination=?
                  AND status IN ('CONFIRMED','HELD')
                  AND ABS(julianday(departure_time) - julianday(?)) < 0.0208
                  AND (? IS NULL OR booking_id != ?)
                LIMIT 1
            """, (origin, destination, departure_time_iso, exclude_id, exclude_id)).fetchall()
            return len(rows) > 0

    def get_bookings_since(self, since_iso: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM bookings WHERE updated_at > ? ORDER BY updated_at",
                (since_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Peer operations
    # ------------------------------------------------------------------
    def upsert_peer(self, region_name: str, host: str, port: int,
                    cities: list, gateway_city: str, status: str = "ALIVE"):
        now = datetime.utcnow().isoformat()
        with self._write_lock:
            with self._conn() as c:
                c.execute("""
                    INSERT INTO peers
                        (region_name, host, port, cities, gateway_city, status, last_seen)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(region_name) DO UPDATE SET
                        host=excluded.host, port=excluded.port,
                        cities=excluded.cities, gateway_city=excluded.gateway_city,
                        status=excluded.status, last_seen=excluded.last_seen,
                        consecutive_failures=0
                """, (region_name, host, port, json.dumps(cities), gateway_city, status, now))

    def get_peer(self, region_name: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM peers WHERE region_name=?", (region_name,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_peers(self, status: str = None):
        with self._conn() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM peers WHERE status=?", (status,)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM peers").fetchall()
            return [dict(r) for r in rows]

    def update_peer_status(self, region_name: str, status: str, increment_failures: bool = False):
        now = datetime.utcnow().isoformat()
        with self._write_lock:
            with self._conn() as c:
                if increment_failures:
                    c.execute("""
                        UPDATE peers
                        SET status=?, consecutive_failures=consecutive_failures+1, last_seen=?
                        WHERE region_name=?
                    """, (status, now, region_name))
                else:
                    c.execute("""
                        UPDATE peers
                        SET status=?, consecutive_failures=0, last_seen=?
                        WHERE region_name=?
                    """, (status, now, region_name))

    def update_peer_last_seen(self, region_name: str):
        now = datetime.utcnow().isoformat()
        with self._write_lock:
            with self._conn() as c:
                c.execute("""
                    UPDATE peers
                    SET last_seen=?, consecutive_failures=0, status='ALIVE'
                    WHERE region_name=?
                """, (now, region_name))

    # ------------------------------------------------------------------
    # 2PC Transaction operations
    # ------------------------------------------------------------------
    def create_transaction(self, transaction_id: str, booking_id: str,
                           coordinator_region: str, role: str, timeout_secs: int = 10):
        now = datetime.utcnow().isoformat()
        expires = (datetime.utcnow() + timedelta(seconds=timeout_secs)).isoformat()
        with self._write_lock:
            with self._conn() as c:
                c.execute("""
                    INSERT OR REPLACE INTO held_transactions
                        (transaction_id, booking_id, coordinator_region,
                         role, phase, created_at, expires_at)
                    VALUES (?,?,?,?,'PREPARE',?,?)
                """, (transaction_id, booking_id, coordinator_region, role, now, expires))

    def update_transaction_phase(self, transaction_id: str, phase: str):
        with self._write_lock:
            with self._conn() as c:
                c.execute(
                    "UPDATE held_transactions SET phase=? WHERE transaction_id=?",
                    (phase, transaction_id),
                )

    def get_transaction(self, transaction_id: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM held_transactions WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------
    def log_event(self, event_type: str, details):
        now = datetime.utcnow().isoformat()
        payload = json.dumps(details) if isinstance(details, dict) else str(details)
        with self._write_lock:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO event_log (event_type, details, created_at) VALUES (?,?,?)",
                    (event_type, payload, now),
                )
