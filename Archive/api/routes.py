# ============================================================
# api/routes.py — All REST endpoints for a GDTS node
# ============================================================
import json
from datetime import datetime

from flask import Blueprint, jsonify, request, current_app

from utils.logger import log

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _state():
    return current_app.config["NODE_STATE"]


# ──────────────────────────────────────────────────────────────────────
# Health  (SERVICE 5 endpoint)
# ──────────────────────────────────────────────────────────────────────

@api_bp.route("/health/ping", methods=["GET"])
def health_ping():
    s = _state()
    if s.failure_simulated:
        return jsonify({"status": "FAILED"}), 503
    return jsonify({"status": "OK", "region": s.region_name}), 200


@api_bp.route("/health/status", methods=["GET"])
def health_status():
    s = _state()
    return jsonify(s.to_dict()), 200


# ──────────────────────────────────────────────────────────────────────
# Region  (SERVICE 2 endpoint)
# ──────────────────────────────────────────────────────────────────────

@api_bp.route("/region/info", methods=["GET"])
def region_info():
    s = _state()
    return jsonify(s.region_service.get_info()), 200


@api_bp.route("/region/graph", methods=["GET"])
def region_graph():
    s = _state()
    return jsonify(s.road_network.to_dict()), 200


# ──────────────────────────────────────────────────────────────────────
# Peers  (SERVICE 1 endpoint)
# ──────────────────────────────────────────────────────────────────────

@api_bp.route("/peer/list", methods=["GET"])
def peer_list():
    s = _state()
    peers = s.db.get_all_peers()
    return jsonify({"peers": peers}), 200


@api_bp.route("/peer/announce", methods=["POST"])
def peer_announce():
    """Manual peer announcement (seed-node fallback)."""
    data = request.get_json(force=True)
    s = _state()
    s.db.upsert_peer(
        data["region_name"], data["host"], data["api_port"],
        data.get("cities", []), data.get("gateway_city", ""),
    )
    # Add inter-region edge
    gw_city  = data.get("gateway_city")
    my_gw    = s.road_network.gateway_city()
    if gw_city:
        dist = s.road_network.add_inter_region_edge(my_gw, gw_city, data["region_name"])
        log("GATEWAY", f"Manual peer registered: [{data['region_name']}]  road {my_gw}↔{gw_city} ({dist}km)")
    return jsonify({"ok": True}), 200


# ──────────────────────────────────────────────────────────────────────
# Booking  (SERVICE 3 endpoint)
# ──────────────────────────────────────────────────────────────────────

@api_bp.route("/booking/create", methods=["POST"])
def booking_create():
    s = _state()
    data = request.get_json(force=True)

    driver_id   = data.get("driver_id", "DRIVER-UNKNOWN")
    origin      = data.get("origin", "")
    destination = data.get("destination", "")
    dep_raw     = data.get("departure_time")

    try:
        dep_dt = datetime.fromisoformat(dep_raw) if dep_raw else datetime.utcnow()
    except ValueError:
        dep_dt = datetime.utcnow()

    # Ask gateway first — returns None if we should handle locally
    gw_result = s.gateway.route_booking(driver_id, origin, destination,
                                        dep_dt.isoformat())
    if gw_result is not None:
        ok, booking, msg = gw_result
        return jsonify({"success": ok, "booking": booking, "message": msg}), (200 if ok else 400)

    ok, booking, msg = s.booking_service.book_journey(driver_id, origin, destination, dep_dt)
    return jsonify({
        "success": ok,
        "booking": booking.to_dict() if booking else None,
        "message": msg,
    }), (200 if ok else 400)


@api_bp.route("/booking/cancel/<booking_id>", methods=["POST", "DELETE"])
def booking_cancel(booking_id):
    s = _state()
    ok, msg = s.booking_service.cancel_booking(booking_id)
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)


@api_bp.route("/booking/list", methods=["GET"])
def booking_list():
    s = _state()
    status = request.args.get("status")
    bookings = s.db.get_all_bookings(status=status)
    return jsonify({"bookings": bookings, "count": len(bookings)}), 200


@api_bp.route("/booking/<booking_id>", methods=["GET"])
def booking_get(booking_id):
    s = _state()
    b = s.db.get_booking(booking_id)
    if not b:
        return jsonify({"error": "Not found"}), 404
    return jsonify(b), 200


# ──────────────────────────────────────────────────────────────────────
# 2PC Coordinator  (SERVICE 4 endpoints)
# ──────────────────────────────────────────────────────────────────────

@api_bp.route("/coordinator/prepare", methods=["POST"])
def coordinator_prepare():
    s = _state()
    data = request.get_json(force=True)
    result = s.coordinator.handle_prepare(
        data["transaction_id"],
        data["booking_data"],
        data.get("coordinator", "UNKNOWN"),
    )
    return jsonify(result), 200


@api_bp.route("/coordinator/commit", methods=["POST"])
def coordinator_commit():
    s = _state()
    data = request.get_json(force=True)
    s.coordinator.handle_commit(data["transaction_id"])
    return jsonify({"ok": True}), 200


@api_bp.route("/coordinator/abort", methods=["POST"])
def coordinator_abort():
    s = _state()
    data = request.get_json(force=True)
    s.coordinator.handle_abort(data["transaction_id"])
    return jsonify({"ok": True}), 200


# ──────────────────────────────────────────────────────────────────────
# Replication  (SERVICE 6 endpoints)
# ──────────────────────────────────────────────────────────────────────

@api_bp.route("/replication/sync", methods=["POST"])
def replication_sync():
    s = _state()
    data = request.get_json(force=True)
    applied = s.replication_service.receive_sync(
        data.get("source_region", "UNKNOWN"),
        data.get("bookings", []),
    )
    return jsonify({"applied": applied}), 200


@api_bp.route("/replication/bookings-since", methods=["GET"])
def replication_bookings_since():
    s = _state()
    since = request.args.get("since", "1970-01-01T00:00:00")
    bookings = s.db.get_bookings_since(since)
    return jsonify({"bookings": bookings, "count": len(bookings)}), 200
