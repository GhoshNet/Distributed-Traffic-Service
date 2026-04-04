"""
Shared Pydantic schemas for inter-service communication.
These models define the data contracts between microservices.
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
import uuid


# ==========================================
# Enums
# ==========================================

class JourneyStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class UserRole(str, Enum):
    DRIVER = "DRIVER"
    ENFORCEMENT_AGENT = "ENFORCEMENT_AGENT"
    ADMIN = "ADMIN"


class VehicleType(str, Enum):
    MOTORCYCLE = "MOTORCYCLE"
    CAR = "CAR"
    VAN = "VAN"
    TRUCK = "TRUCK"
    BUS = "BUS"


class ConflictType(str, Enum):
    TIME_OVERLAP = "TIME_OVERLAP"
    ROAD_CAPACITY = "ROAD_CAPACITY"


class EventType(str, Enum):
    JOURNEY_BOOKED = "journey.booked"
    JOURNEY_CONFIRMED = "journey.confirmed"
    JOURNEY_REJECTED = "journey.rejected"
    JOURNEY_CANCELLED = "journey.cancelled"
    JOURNEY_STARTED = "journey.started"
    JOURNEY_COMPLETED = "journey.completed"
    USER_REGISTERED = "user.registered"
    CONFLICT_CHECK_REQUESTED = "conflict.check.requested"
    CONFLICT_CHECK_COMPLETED = "conflict.check.completed"


# ==========================================
# User Schemas
# ==========================================

class UserRegisterRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=8, description="Password (min 8 chars)")
    full_name: str = Field(..., description="Full name of the driver")
    license_number: str = Field(..., description="Driving license number")
    role: UserRole = Field(default=UserRole.DRIVER, description="User role")


class UserLoginRequest(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    license_number: str
    role: UserRole
    created_at: datetime

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ==========================================
# Journey Schemas
# ==========================================

class JourneyCreateRequest(BaseModel):
    origin: str = Field(..., description="Starting location (city/address)")
    destination: str = Field(..., description="Ending location (city/address)")
    origin_lat: float = Field(..., ge=-90, le=90, description="Origin latitude")
    origin_lng: float = Field(..., ge=-180, le=180, description="Origin longitude")
    destination_lat: float = Field(..., ge=-90, le=90, description="Destination latitude")
    destination_lng: float = Field(..., ge=-180, le=180, description="Destination longitude")
    departure_time: datetime = Field(..., description="Planned departure time (UTC)")
    estimated_duration_minutes: int = Field(..., gt=0, le=1440, description="Estimated duration in minutes")
    vehicle_registration: str = Field(..., description="Vehicle registration plate")
    vehicle_type: VehicleType = Field(default=VehicleType.CAR, description="Type of vehicle")
    idempotency_key: Optional[str] = Field(
        default=None,
        description="Client-generated idempotency key for safe retries"
    )


class JourneyResponse(BaseModel):
    id: str
    user_id: str
    origin: str
    destination: str
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    departure_time: datetime
    estimated_duration_minutes: int
    estimated_arrival_time: datetime
    vehicle_registration: str
    vehicle_type: VehicleType
    status: JourneyStatus
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class JourneyListResponse(BaseModel):
    journeys: list[JourneyResponse]
    total: int
    page: int
    page_size: int


# ==========================================
# Conflict Detection Schemas
# ==========================================

class ConflictCheckRequest(BaseModel):
    journey_id: str
    user_id: str
    origin_lat: float
    origin_lng: float
    destination_lat: float
    destination_lng: float
    departure_time: datetime
    estimated_duration_minutes: int
    vehicle_registration: str
    vehicle_type: VehicleType = Field(default=VehicleType.CAR)


class ConflictCheckResponse(BaseModel):
    journey_id: str
    is_conflict: bool
    conflict_type: Optional[ConflictType] = None
    conflict_details: Optional[str] = None
    checked_at: datetime


# ==========================================
# Enforcement Schemas
# ==========================================

class VerificationRequest(BaseModel):
    driver_license: Optional[str] = None
    vehicle_registration: Optional[str] = None


class VerificationResponse(BaseModel):
    is_valid: bool
    driver_id: Optional[str] = None
    journey_id: Optional[str] = None
    journey_status: Optional[JourneyStatus] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    departure_time: Optional[datetime] = None
    estimated_arrival_time: Optional[datetime] = None
    checked_at: datetime


# ==========================================
# Notification Schemas
# ==========================================

class NotificationPayload(BaseModel):
    user_id: str
    event_type: EventType
    title: str
    message: str
    journey_id: Optional[str] = None
    metadata: Optional[dict] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ==========================================
# Analytics Schemas
# ==========================================

class AnalyticsEvent(BaseModel):
    event_type: EventType
    journey_id: Optional[str] = None
    user_id: Optional[str] = None
    region: Optional[str] = None
    metadata: Optional[dict] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SystemStatsResponse(BaseModel):
    total_users: int
    total_journeys: int
    active_journeys: int
    confirmed_today: int
    rejected_today: int
    cancelled_today: int
    avg_booking_time_ms: Optional[float] = None


# ==========================================
# Common / Health
# ==========================================

class HealthResponse(BaseModel):
    status: str = "healthy"
    service: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    version: str = "1.0.0"
    dependencies: Optional[dict] = None


class ErrorResponse(BaseModel):
    error: str
    message: str
    request_id: Optional[str] = None
