"""
Analytics & Monitoring Service - Database models.

Stores aggregated event data for system-wide analytics and monitoring.
"""

import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, DateTime, Integer, Float, Text, func, Index
from datetime import datetime


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://analytics_user:analytics_pass@localhost:5432/analytics_db",
)

# Isolation level: READ COMMITTED — analytics is append-only (event logging).
# No concurrent modifications to the same row, so READ COMMITTED is sufficient.
# The HMAC audit chain provides integrity guarantees at the application level.
engine = create_async_engine(
    DATABASE_URL, echo=False, pool_size=20, max_overflow=10,
    execution_options={"isolation_level": "READ COMMITTED"},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class EventLog(Base):
    """Stores all system events for analytics."""
    __tablename__ = "event_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    journey_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=True, index=True)
    origin: Mapped[str] = mapped_column(String(500), nullable=True)
    destination: Mapped[str] = mapped_column(String(500), nullable=True)
    region: Mapped[str] = mapped_column(String(100), nullable=True, index=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_event_type_date", "event_type", "created_at"),
    )


class HourlyStats(Base):
    """Pre-aggregated hourly statistics for dashboard queries."""
    __tablename__ = "hourly_stats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    total_bookings: Mapped[int] = mapped_column(Integer, default=0)
    confirmed: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)
    avg_duration_minutes: Mapped[float] = mapped_column(Float, nullable=True)
    region: Mapped[str] = mapped_column(String(100), nullable=True, index=True)

    __table_args__ = (
        Index("idx_hourly_region", "hour", "region"),
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
