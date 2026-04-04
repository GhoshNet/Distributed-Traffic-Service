"""
Journey Service - Database models.
"""

import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, DateTime, Float, Integer, Boolean, Text, func, Index
from datetime import datetime


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://journeys_user:journeys_pass@localhost:5432/journeys_db",
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=20, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Journey(Base):
    __tablename__ = "journeys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(500), nullable=False)
    destination: Mapped[str] = mapped_column(String(500), nullable=False)
    origin_lat: Mapped[float] = mapped_column(Float, nullable=False)
    origin_lng: Mapped[float] = mapped_column(Float, nullable=False)
    destination_lat: Mapped[float] = mapped_column(Float, nullable=False)
    destination_lng: Mapped[float] = mapped_column(Float, nullable=False)
    departure_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_arrival_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    vehicle_registration: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    vehicle_type: Mapped[str] = mapped_column(String(20), default="CAR", nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Composite indexes for common queries
    __table_args__ = (
        Index("idx_user_status", "user_id", "status"),
        Index("idx_user_departure", "user_id", "departure_time"),
        Index("idx_vehicle_departure", "vehicle_registration", "departure_time"),
    )


class IdempotencyRecord(Base):
    """Tracks processed idempotency keys to prevent duplicate bookings."""
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    journey_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
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
