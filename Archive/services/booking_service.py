# ============================================================
# services/booking_service.py — SERVICE 3: Journey booking
# ============================================================
import threading
import time
import json
from datetime import datetime
from typing import Tuple, Optional

import config
from models.booking import Booking
from utils.logger import log


class BookingService:
    """
    Handles local journey bookings and delegates cross-region bookings
    to the CoordinatorService via 2PC.
    """

    def __init__(self, node_state):
        self.state = node_state
        self._booking_lock = threading.Lock()
        log("BOOKING", "Booking service initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def book_journey(
        self,
        driver_id: str,
        origin: str,
        destination: str,
        departure_time: datetime,
        cross_region_override: bool = False,
    ) -> Tuple[bool, Optional[Booking], str]:
        """
        Returns (success, booking_or_None, message).
        cross_region_override=True skips the cross-region 2PC check
        (used by the coordinator after it has already done 2PC).
        """
        self._apply_delay()

        if self.state.failure_simulated:
            log("BOOKING", "❌ Node in failure mode — rejecting booking", "ERROR")
            return False, None, "Node is simulating failure"

        db = self.state.db
        local_cities = self.state.road_network.cities

        origin_local = origin in local_cities
        dest_local   = destination in local_cities
        is_cross     = not (origin_local and dest_local)

        log("BOOKING",
            f"📋 {driver_id} | {origin} → {destination} | "
            f"{'cross-region' if is_cross else 'local'}")

        # --- cross-region: hand off to 2PC coordinator ---
        if is_cross and not cross_region_override:
            if self.state.local_only_mode:
                log("BOOKING", "⚠️  LOCAL ONLY mode — cross-region booking queued", "WARN")
                return False, None, "Node in local-only mode; cross-region booking unavailable"
            log("BOOKING", "🔀 Delegating to Coordinator (2PC)", "INFO")
            return self.state.coordinator.initiate_cross_region_booking(
                driver_id, origin, destination, departure_time
            )

        # --- local booking ---
        with self._booking_lock:
            dep_iso = (
                departure_time.isoformat()
                if isinstance(departure_time, datetime)
                else departure_time
            )
            if db.check_conflict(origin, destination, dep_iso):
                log("BOOKING",
                    f"⚠️  Conflict: booking already exists for {origin}→{destination} ≈{dep_iso}",
                    "WARN")
                return False, None, f"Conflict: another booking exists for {origin}→{destination} around that time"

            route = self.state.road_network.find_route(origin, destination)
            if not route:
                log("BOOKING", f"❌ No route: {origin} → {destination}", "ERROR")
                return False, None, f"No route found between {origin} and {destination}"

            log("BOOKING", f"🗺️  Route: {' → '.join(route)}")

            if not self.state.road_network.check_road_capacity(route):
                log("BOOKING", f"🚗 Road at capacity on {' → '.join(route)}", "WARN")
                return False, None, "Road capacity exceeded on this route"

            booking = Booking(
                driver_id=driver_id,
                origin=origin,
                destination=destination,
                departure_time=departure_time
                if isinstance(departure_time, datetime)
                else datetime.fromisoformat(str(departure_time)),
                home_region=self.state.region_name,
                status="CONFIRMED",
                is_cross_region=is_cross,
                route_path=route,
            )

            self.state.road_network.reserve_road(route)
            db.insert_booking(booking)
            db.log_event("BOOKING_CONFIRMED", booking.to_dict())

            log("BOOKING", f"✅ Confirmed: {booking.booking_id}", "SUCCESS")
            return True, booking, "Booking confirmed"

    def cancel_booking(self, booking_id: str) -> Tuple[bool, str]:
        self._apply_delay()
        db = self.state.db
        row = db.get_booking(booking_id)

        if not row:
            log("BOOKING", f"❌ Booking {booking_id} not found", "ERROR")
            return False, "Booking not found"

        if row["status"] == "CANCELLED":
            return False, "Booking already cancelled"

        route = json.loads(row.get("route_path") or "[]")
        if route:
            self.state.road_network.release_road(route)

        db.update_booking_status(booking_id, "CANCELLED")
        db.log_event("BOOKING_CANCELLED", {"booking_id": booking_id})
        log("BOOKING", f"✅ Cancelled: {booking_id}", "SUCCESS")
        return True, "Booking cancelled"

    # ------------------------------------------------------------------
    # 2PC helpers — called by CoordinatorService
    # ------------------------------------------------------------------
    def create_held_booking(
        self, driver_id, origin, destination, departure_time, transaction_id, home_region
    ) -> Optional[Booking]:
        """Create a HELD booking (2PC PREPARE phase on participant node)."""
        db = self.state.db
        local_cities = self.state.road_network.cities

        dep_iso = (
            departure_time.isoformat()
            if isinstance(departure_time, datetime)
            else str(departure_time)
        )

        if db.check_conflict(origin, destination, dep_iso):
            log("BOOKING", f"   HELD PREPARE failed: conflict {origin}→{destination}", "WARN")
            return None

        # Build partial route (only the segment belonging to this region)
        route = []
        if origin in local_cities and destination in local_cities:
            route = self.state.road_network.find_route(origin, destination) or []
        elif origin in local_cities:
            route = [origin]
        elif destination in local_cities:
            route = [destination]

        if len(route) > 1 and not self.state.road_network.check_road_capacity(route):
            log("BOOKING", "   HELD PREPARE failed: road at capacity", "WARN")
            return None

        if len(route) > 1:
            self.state.road_network.reserve_road(route)

        dep_dt = (
            departure_time
            if isinstance(departure_time, datetime)
            else datetime.fromisoformat(str(departure_time))
        )

        booking = Booking(
            driver_id=driver_id,
            origin=origin,
            destination=destination,
            departure_time=dep_dt,
            home_region=home_region,
            status="HELD",
            is_cross_region=True,
            transaction_id=transaction_id,
            route_path=route,
        )
        db.insert_booking(booking)
        log("BOOKING", f"   HELD booking created: {booking.booking_id}")
        return booking

    def confirm_held_booking(self, booking_id: str) -> bool:
        db = self.state.db
        row = db.get_booking(booking_id)
        if row and row["status"] == "HELD":
            db.update_booking_status(booking_id, "CONFIRMED")
            db.log_event("2PC_COMMITTED", {"booking_id": booking_id})
            log("BOOKING", f"✅ HELD→CONFIRMED: {booking_id}", "SUCCESS")
            return True
        return False

    def abort_held_booking(self, booking_id: str) -> bool:
        db = self.state.db
        row = db.get_booking(booking_id)
        if row and row["status"] == "HELD":
            route = json.loads(row.get("route_path") or "[]")
            if len(route) > 1:
                self.state.road_network.release_road(route)
            db.update_booking_status(booking_id, "CANCELLED")
            db.log_event("2PC_ABORTED", {"booking_id": booking_id})
            log("BOOKING", f"🚫 HELD→CANCELLED (abort): {booking_id}", "WARN")
            return True
        return False

    # ------------------------------------------------------------------
    def _apply_delay(self):
        ms = self.state.network_delay_ms
        if ms > 0:
            log("BOOKING", f"⏳ Simulated network delay: {ms} ms", "WARN")
            time.sleep(ms / 1000.0)
