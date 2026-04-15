# ============================================================
# services/gateway.py — SERVICE 7: Request routing gateway
# ============================================================
import json
import time
from typing import Optional, Tuple

import requests

import config
from utils.logger import log


class GatewayService:
    """
    Routes incoming booking requests to the correct home region.
    The 'home region' for a booking is determined by the origin city.
    If the origin city is local → handle locally.
    If the origin city belongs to a peer → forward the request.
    If origin is unknown → try local anyway (may fail with 'no route').
    """

    def __init__(self, node_state):
        self.state = node_state
        log("GATEWAY", "Request routing gateway initialised")

    # ------------------------------------------------------------------
    def route_booking(
        self, driver_id: str, origin: str, destination: str, departure_time_iso: str
    ) -> Tuple[bool, Optional[dict], str]:
        """
        Decide whether to handle locally or forward to the owning region.
        Returns (success, booking_dict_or_None, message).
        """
        self._apply_delay()

        local_cities = self.state.road_network.cities
        if origin in local_cities:
            log("GATEWAY", f"✅ Local routing: {origin} → {destination}")
            return None   # signal caller to handle locally

        # Find peer that owns origin
        peer = self._find_peer_for_city(origin)
        if not peer:
            log("GATEWAY",
                f"⚠️  Unknown origin city '{origin}' — attempting local handling",
                "WARN")
            return None   # fallback to local

        if peer["status"] != "ALIVE":
            if self.state.local_only_mode:
                log("GATEWAY",
                    f"🔴 Peer [{peer['region_name']}] is {peer['status']} and node is LOCAL ONLY — rejecting",
                    "ERROR")
                return False, None, f"Home region [{peer['region_name']}] unavailable"
            # degrade gracefully: try anyway
            log("GATEWAY",
                f"⚠️  Peer [{peer['region_name']}] is {peer['status']} — attempting forward",
                "WARN")

        log("GATEWAY",
            f"🔀 Forwarding booking to [{peer['region_name']}] @ {peer['host']}:{peer['port']}")

        url = f"http://{peer['host']}:{peer['port']}/api/booking/create"
        payload = {
            "driver_id": driver_id,
            "origin": origin,
            "destination": destination,
            "departure_time": departure_time_iso,
        }
        try:
            resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT)
            data = resp.json()
            ok   = data.get("success", False)
            msg  = data.get("message", "")
            booking = data.get("booking")
            log("GATEWAY",
                f"   Forward result: {'✅' if ok else '❌'} {msg}",
                "SUCCESS" if ok else "WARN")
            return ok, booking, msg
        except Exception as e:
            log("GATEWAY", f"   Forward failed: {e}", "ERROR")
            return False, None, f"Could not reach home region [{peer['region_name']}]: {e}"

    # ------------------------------------------------------------------
    def _find_peer_for_city(self, city: str) -> Optional[dict]:
        peers = self.state.db.get_all_peers()
        for peer in peers:
            cities = json.loads(peer.get("cities", "[]"))
            if city in cities:
                return peer
        return None

    def _apply_delay(self):
        ms = self.state.network_delay_ms
        if ms > 0:
            log("GATEWAY", f"⏳ Simulated routing delay: {ms} ms", "WARN")
            time.sleep(ms / 1000.0)
