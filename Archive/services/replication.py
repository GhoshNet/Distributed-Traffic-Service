# ============================================================
# services/replication.py — SERVICE 6: Booking replication
# ============================================================
import threading
import time
import json
from datetime import datetime, timezone

import requests

import config
from models.booking import Booking
from utils.logger import log


class ReplicationService:
    """
    Periodically pushes confirmed bookings to all alive peers.
    Also handles pull-sync requests from recovering nodes.
    Provides eventual consistency across the cluster.
    """

    def __init__(self, node_state):
        self.state = node_state
        self._running = False
        self._last_push_ts: str = "1970-01-01T00:00:00"
        log("REPLICATION", "Replication service initialised")

    def start(self):
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True, name="replication")
        t.start()
        log("REPLICATION",
            f"📡 Replication started  interval={config.REPLICATION_INTERVAL}s")

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Periodic push
    # ------------------------------------------------------------------
    def _loop(self):
        while self._running:
            time.sleep(config.REPLICATION_INTERVAL)
            if not self.state.failure_simulated:
                self._push_to_peers()

    def _push_to_peers(self):
        db = self.state.db
        peers = db.get_all_peers(status="ALIVE")
        if not peers:
            return

        since = self._last_push_ts
        bookings = db.get_bookings_since(since)
        if not bookings:
            return

        now = datetime.utcnow().isoformat()
        log("REPLICATION",
            f"📤 Pushing {len(bookings)} booking(s) to {len(peers)} peer(s)  since={since}")

        payload = {
            "source_region": self.state.region_name,
            "bookings": bookings,
        }
        for peer in peers:
            url = f"http://{peer['host']}:{peer['port']}/api/replication/sync"
            try:
                requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT)
                log("REPLICATION", f"   ✅ Pushed to [{peer['region_name']}]")
            except Exception as e:
                log("REPLICATION",
                    f"   ⚠️  Push failed to [{peer['region_name']}]: {e}", "WARN")

        self._last_push_ts = now

    # ------------------------------------------------------------------
    # Pull-sync (called by health_monitor when peer recovers)
    # ------------------------------------------------------------------
    def sync_from_peer(self, peer: dict):
        log("REPLICATION",
            f"🔄 Pulling state from recovering peer [{peer['region_name']}]", "INFO")
        url = (f"http://{peer['host']}:{peer['port']}/api/replication"
               f"/bookings-since?since=1970-01-01T00:00:00")
        try:
            resp = requests.get(url, timeout=config.REQUEST_TIMEOUT)
            data = resp.json()
            bookings = data.get("bookings", [])
            applied = self._apply_incoming(bookings, peer["region_name"])
            log("REPLICATION",
                f"   Synced {applied} booking(s) from [{peer['region_name']}]",
                "SUCCESS")
        except Exception as e:
            log("REPLICATION",
                f"   Pull-sync failed from [{peer['region_name']}]: {e}", "WARN")

    # ------------------------------------------------------------------
    # Receive incoming replication push  (called by API route)
    # ------------------------------------------------------------------
    def receive_sync(self, source_region: str, bookings: list) -> int:
        applied = self._apply_incoming(bookings, source_region)
        if applied:
            log("REPLICATION",
                f"📥 Applied {applied} replicated booking(s) from [{source_region}]")
        return applied

    # ------------------------------------------------------------------
    def _apply_incoming(self, bookings: list, source_region: str) -> int:
        db = self.state.db
        applied = 0
        for b_dict in bookings:
            existing = db.get_booking(b_dict.get("booking_id"))
            if existing:
                # last-write-wins by version
                if int(b_dict.get("version", 0)) > int(existing.get("version", 0)):
                    db.update_booking_status(
                        b_dict["booking_id"], b_dict["status"]
                    )
                    applied += 1
            else:
                # New booking from remote — store it locally
                try:
                    booking = Booking.from_dict(b_dict)
                    db.insert_booking(booking)
                    applied += 1
                except Exception as e:
                    log("REPLICATION",
                        f"   Failed to apply booking {b_dict.get('booking_id')}: {e}",
                        "WARN")
        return applied
