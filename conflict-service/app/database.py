"""
Conflict Detection Service - Database models.

Stores road segment capacity data and booked journey time slots
for efficient overlap detection.
"""

import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, DateTime, Float, Integer, func, Index
from datetime import datetime


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://conflicts_user:conflicts_pass@localhost:5432/conflicts_db",
)

# Isolation level: SERIALIZABLE — the Conflict Service is the single authority
# for booking slot allocation. SERIALIZABLE prevents phantom reads where two
# concurrent conflict checks could both see "no overlap" and both approve,
# creating a double-booking. This is the critical path for correctness.
engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=20, max_overflow=10,
    execution_options={"isolation_level": "SERIALIZABLE"},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class BookedSlot(Base):
    """Represents a time slot reserved by a confirmed journey for a specific user/vehicle."""
    __tablename__ = "booked_slots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    journey_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    vehicle_registration: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    departure_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    arrival_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    origin_lat: Mapped[float] = mapped_column(Float, nullable=False)
    origin_lng: Mapped[float] = mapped_column(Float, nullable=False)
    destination_lat: Mapped[float] = mapped_column(Float, nullable=False)
    destination_lng: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_slot_user_time", "user_id", "departure_time", "arrival_time"),
        Index("idx_slot_vehicle_time", "vehicle_registration", "departure_time", "arrival_time"),
    )


class RoadSegmentCapacity(Base):
    """Tracks capacity for geographic regions (grid cells) at time intervals."""
    __tablename__ = "road_segment_capacity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Grid cell identified by rounded lat/lng
    grid_lat: Mapped[float] = mapped_column(Float, nullable=False)
    grid_lng: Mapped[float] = mapped_column(Float, nullable=False)
    time_slot_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    time_slot_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    current_bookings: Mapped[float] = mapped_column(Float, default=0.0)
    max_capacity: Mapped[int] = mapped_column(Integer, default=100)

    __table_args__ = (
        Index("idx_grid_time", "grid_lat", "grid_lng", "time_slot_start"),
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
