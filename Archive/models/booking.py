# ============================================================
# models/booking.py — Journey / Booking data model
# ============================================================
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Booking:
    driver_id: str
    origin: str
    destination: str
    departure_time: datetime
    home_region: str
    booking_id: str = field(
        default_factory=lambda: str(uuid.uuid4())[:8].upper()
    )
    status: str = "CONFIRMED"          # PENDING | CONFIRMED | HELD | CANCELLED
    version: int = 1
    is_cross_region: bool = False
    transaction_id: Optional[str] = None
    route_path: Optional[List[str]] = None

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "booking_id": self.booking_id,
            "driver_id": self.driver_id,
            "origin": self.origin,
            "destination": self.destination,
            "departure_time": (
                self.departure_time.isoformat()
                if isinstance(self.departure_time, datetime)
                else self.departure_time
            ),
            "home_region": self.home_region,
            "status": self.status,
            "version": self.version,
            "is_cross_region": self.is_cross_region,
            "transaction_id": self.transaction_id,
            "route_path": self.route_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Booking":
        dep = d.get("departure_time")
        if isinstance(dep, str):
            try:
                dep = datetime.fromisoformat(dep)
            except ValueError:
                dep = datetime.utcnow()
        return cls(
            booking_id=d.get("booking_id", str(uuid.uuid4())[:8].upper()),
            driver_id=d.get("driver_id", "UNKNOWN"),
            origin=d.get("origin", ""),
            destination=d.get("destination", ""),
            departure_time=dep or datetime.utcnow(),
            home_region=d.get("home_region", ""),
            status=d.get("status", "CONFIRMED"),
            version=int(d.get("version", 1)),
            is_cross_region=bool(d.get("is_cross_region", False)),
            transaction_id=d.get("transaction_id"),
            route_path=d.get("route_path"),
        )
