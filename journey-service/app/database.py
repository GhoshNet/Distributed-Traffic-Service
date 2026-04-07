"""
Journey Service - Database models.

Uses READ COMMITTED isolation level (PostgreSQL default) for the primary engine.
This is sufficient because:
- Journey creation uses idempotency keys (unique constraint) to prevent duplicates
- Points operations use SELECT FOR UPDATE to serialize concurrent modifications
- The outbox pattern guarantees at-least-once event delivery

A read replica engine is configured for read-heavy queries (list journeys,
enforcement lookups) to offload the primary. The replica uses streaming
replication from the primary with eventual consistency (typical lag < 100ms).
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
DATABASE_READ_URL = os.getenv("DATABASE_READ_URL", "")

# Primary engine — all writes go here
# Isolation level: READ COMMITTED (PostgreSQL default, explicitly set for clarity)
engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=20, max_overflow=10,
    execution_options={"isolation_level": "READ COMMITTED"},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Read replica engine — read-heavy queries are routed here to reduce primary load
# Falls back to primary if no replica URL is configured
_read_url = DATABASE_READ_URL if DATABASE_READ_URL else DATABASE_URL
read_engine = create_async_engine(
    _read_url, echo=False, pool_size=20, max_overflow=10,
    execution_options={"isolation_level": "READ COMMITTED"},
)
read_session = async_sessionmaker(read_engine, class_=AsyncSession, expire_on_commit=False)


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


class DriverPoints(Base):
    """
    Tracks driver points/credits. Drivers earn points for completing journeys
    and lose points for late cancellations. Uses optimistic locking (version column)
    and SELECT FOR UPDATE to prevent double-spending.
    """
    __tablename__ = "driver_points"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_spent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PointsTransaction(Base):
    """Immutable ledger of all points changes for auditability."""
    __tablename__ = "points_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    journey_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_points_user_created", "user_id", "created_at"),
    )


class OutboxEvent(Base):
    """
    Transactional outbox table — events are written in the same DB transaction
    as the journey status update, then published to RabbitMQ by a background task.
    This guarantees at-least-once delivery even if RabbitMQ is temporarily down.
    """
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    routing_key: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    """Primary DB session — used for writes and consistency-critical reads."""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_read_db() -> AsyncSession:
    """Read replica DB session — used for read-heavy, non-critical queries.
    Routes to the streaming replica if configured, otherwise falls back to primary."""
    async with read_session() as session:
        try:
            yield session
        finally:
            await session.close()
