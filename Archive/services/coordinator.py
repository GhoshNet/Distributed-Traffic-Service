# ============================================================
# services/coordinator.py — SERVICE 4: 2PC Coordinator/Participant
# ============================================================
import json
import uuid
import threading
from datetime import datetime
from typing import Tuple, Optional

import requests

import config
from models.booking import Booking
from utils.logger import log


class CoordinatorService:
    """
    Implements Two-Phase Commit (2PC) for cross-region journey bookings.

    As COORDINATOR  — drives PREPARE → COMMIT/ABORT across participant nodes.
    As PARTICIPANT  — responds to PREPARE / COMMIT / ABORT from coordinators.
    """

    def __init__(self, node_state):
        self.state = node_state
        self._lock = threading.Lock()
        log("COORDINATOR", "2PC Coordinator/Participant service initialised")

    # ------------------------------------------------------------------
    # Coordinator role
    # ------------------------------------------------------------------
    def initiate_cross_region_booking(
        self,
        driver_id: str,
        origin: str,
        destination: str,
        departure_time: datetime,
    ) -> Tuple[bool, Optional[Booking], str]:

        txn_id = f"TXN-{str(uuid.uuid4())[:8].upper()}"
        db = self.state.db

        log("COORDINATOR", f"🔄 Starting 2PC  TXN={txn_id}", "INFO")
        log("COORDINATOR", f"   {origin} → {destination} | driver={driver_id}")

        dep_iso = (
            departure_time.isoformat()
            if isinstance(departure_time, datetime)
            else str(departure_time)
        )

        booking_data = {
            "driver_id": driver_id,
            "origin": origin,
            "destination": destination,
            "departure_time": dep_iso,
            "home_region": self.state.region_name,
            "transaction_id": txn_id,
            "is_cross_region": True,
        }

        # Find which region owns each city
        origin_region, origin_peer = self._find_region(origin)
        dest_region,   dest_peer   = self._find_region(destination)

        if not origin_region or not dest_region:
            msg = (f"Cannot resolve region for "
                   f"{'origin' if not origin_region else 'destination'} city")
            log("COORDINATOR", f"❌ {msg}", "ERROR")
            return False, None, msg

        log("COORDINATOR",
            f"   Regions: {origin} ∈ [{origin_region}]  {destination} ∈ [{dest_region}]")

        # Collect unique remote participants
        participants = {}
        for city, peer in [(origin, origin_peer), (destination, dest_peer)]:
            if peer and peer["region_name"] not in participants:
                participants[peer["region_name"]] = peer

        # ---- PHASE 1: PREPARE ----------------------------------------
        log("COORDINATOR",
            f"📨 PHASE 1 — PREPARE  remote_participants={list(participants.keys())}")

        votes = {}           # region_name -> (vote_ok, remote_booking_id)
        local_booking_id = None

        # Local prepare (if this region hosts origin or destination)
        if origin_region == self.state.region_name or dest_region == self.state.region_name:
            ok, b_id = self._local_prepare(txn_id, booking_data)
            local_booking_id = b_id
            votes[self.state.region_name] = (ok, b_id)
            log("COORDINATOR",
                f"   [{self.state.region_name}] vote: {'✅ YES' if ok else '❌ NO'}")

        # Remote prepares
        prepare_payload = {
            "transaction_id": txn_id,
            "booking_data": booking_data,
            "coordinator": self.state.region_name,
        }
        for rname, peer in participants.items():
            url = f"http://{peer['host']}:{peer['port']}/api/coordinator/prepare"
            try:
                resp = requests.post(url, json=prepare_payload,
                                     timeout=config.TWO_PC_TIMEOUT)
                data = resp.json()
                ok   = data.get("vote") == "YES"
                b_id = data.get("booking_id")
                votes[rname] = (ok, b_id)
                log("COORDINATOR",
                    f"   [{rname}] vote: {'✅ YES' if ok else '❌ NO'}")
            except Exception as e:
                votes[rname] = (False, None)
                log("COORDINATOR", f"   [{rname}] unreachable: {e}", "WARN")

        all_yes = bool(votes) and all(v[0] for v in votes.values())

        # ---- PHASE 2: COMMIT or ABORT --------------------------------
        if all_yes:
            log("COORDINATOR",
                f"✅ PHASE 2 — COMMIT  TXN={txn_id}", "SUCCESS")

            if local_booking_id:
                self.state.booking_service.confirm_held_booking(local_booking_id)

            for rname, peer in participants.items():
                url = f"http://{peer['host']}:{peer['port']}/api/coordinator/commit"
                try:
                    requests.post(url, json={"transaction_id": txn_id},
                                  timeout=config.TWO_PC_TIMEOUT)
                    log("COORDINATOR", f"   COMMIT sent → [{rname}]")
                except Exception as e:
                    log("COORDINATOR",
                        f"   COMMIT failed → [{rname}]: {e}", "WARN")

            db.log_event("2PC_COMMITTED", {"txn_id": txn_id})
            row = db.get_booking(local_booking_id) if local_booking_id else None
            booking = Booking.from_dict(row) if row else None
            return True, booking, f"Cross-region booking confirmed (TXN={txn_id})"

        else:
            failed = [r for r, (ok, _) in votes.items() if not ok]
            log("COORDINATOR",
                f"❌ PHASE 2 — ABORT  TXN={txn_id}  failed_regions={failed}", "ERROR")

            if local_booking_id:
                self.state.booking_service.abort_held_booking(local_booking_id)

            for rname, peer in participants.items():
                url = f"http://{peer['host']}:{peer['port']}/api/coordinator/abort"
                try:
                    requests.post(url, json={"transaction_id": txn_id},
                                  timeout=config.TWO_PC_TIMEOUT)
                except Exception:
                    pass

            db.log_event("2PC_ABORTED", {"txn_id": txn_id, "failed": failed})
            return False, None, (
                f"Cross-region booking failed (TXN={txn_id}). "
                f"Rejected by: {failed}"
            )

    # ------------------------------------------------------------------
    # Participant role  (called by API routes)
    # ------------------------------------------------------------------
    def handle_prepare(self, txn_id: str, booking_data: dict, coordinator: str) -> dict:
        log("COORDINATOR",
            f"📥 PREPARE received  TXN={txn_id}  from=[{coordinator}]")
        ok, booking_id = self._local_prepare(txn_id, booking_data)
        vote = "YES" if ok else "NO"
        log("COORDINATOR",
            f"   Responding: {vote}", "SUCCESS" if ok else "WARN")
        return {"vote": vote, "booking_id": booking_id,
                "region": self.state.region_name}

    def handle_commit(self, txn_id: str):
        log("COORDINATOR", f"📥 COMMIT received  TXN={txn_id}", "SUCCESS")
        txn = self.state.db.get_transaction(txn_id)
        if txn:
            self.state.booking_service.confirm_held_booking(txn["booking_id"])
            self.state.db.update_transaction_phase(txn_id, "COMMITTED")

    def handle_abort(self, txn_id: str):
        log("COORDINATOR", f"📥 ABORT received  TXN={txn_id}", "WARN")
        txn = self.state.db.get_transaction(txn_id)
        if txn:
            self.state.booking_service.abort_held_booking(txn["booking_id"])
            self.state.db.update_transaction_phase(txn_id, "ABORTED")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _find_region(self, city: str):
        """Return (region_name, peer_row_or_None) for the given city."""
        if city in self.state.road_network.cities:
            return self.state.region_name, None   # local

        peers = self.state.db.get_all_peers()
        for peer in peers:
            cities = json.loads(peer.get("cities", "[]"))
            if city in cities:
                return peer["region_name"], peer

        return None, None

    def _local_prepare(self, txn_id: str, booking_data: dict):
        """Create a HELD booking for the 2PC PREPARE phase."""
        dep = datetime.fromisoformat(booking_data["departure_time"])
        booking = self.state.booking_service.create_held_booking(
            driver_id=booking_data["driver_id"],
            origin=booking_data["origin"],
            destination=booking_data["destination"],
            departure_time=dep,
            transaction_id=txn_id,
            home_region=booking_data["home_region"],
        )
        if booking:
            self.state.db.create_transaction(
                txn_id, booking.booking_id,
                booking_data["home_region"], "PARTICIPANT",
                timeout_secs=config.TWO_PC_TIMEOUT + 2,
            )
            return True, booking.booking_id
        return False, None
