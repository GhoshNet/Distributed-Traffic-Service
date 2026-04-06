"""
Driver Points Service — Manages driver credits/points.

Points are earned by completing journeys and deducted for late cancellations.
Uses SELECT FOR UPDATE (pessimistic locking) to prevent double-spending,
and an immutable transaction ledger for full auditability.

Isolation level: READ COMMITTED with row-level locks (SELECT FOR UPDATE)
ensures that concurrent point operations are serialized per-user.
"""

import uuid
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from .database import DriverPoints, PointsTransaction

logger = logging.getLogger(__name__)

# Points configuration
POINTS_PER_COMPLETED_JOURNEY = 10
POINTS_PER_BOOKING = 2
POINTS_DEDUCTED_LATE_CANCEL = 5
POINTS_WELCOME_BONUS = 20


class PointsService:
    """Manages driver points with double-spend prevention."""

    @staticmethod
    async def get_or_create_wallet(db: AsyncSession, user_id: str) -> DriverPoints:
        """Get or create a points wallet for a user."""
        result = await db.execute(
            select(DriverPoints).where(DriverPoints.user_id == user_id)
        )
        wallet = result.scalar_one_or_none()
        if not wallet:
            wallet = DriverPoints(
                user_id=user_id,
                balance=POINTS_WELCOME_BONUS,
                total_earned=POINTS_WELCOME_BONUS,
                total_spent=0,
                version=1,
            )
            db.add(wallet)
            # Record the welcome bonus transaction
            db.add(PointsTransaction(
                id=str(uuid.uuid4()),
                user_id=user_id,
                journey_id=None,
                amount=POINTS_WELCOME_BONUS,
                reason="WELCOME_BONUS",
                balance_after=POINTS_WELCOME_BONUS,
            ))
            await db.commit()
            await db.refresh(wallet)
        return wallet

    @staticmethod
    async def get_balance(db: AsyncSession, user_id: str) -> dict:
        """Get current points balance for a user."""
        wallet = await PointsService.get_or_create_wallet(db, user_id)
        return {
            "user_id": wallet.user_id,
            "balance": wallet.balance,
            "total_earned": wallet.total_earned,
            "total_spent": wallet.total_spent,
        }

    @staticmethod
    async def earn_points(
        db: AsyncSession, user_id: str, amount: int, reason: str, journey_id: str = None
    ) -> dict:
        """
        Award points to a driver. Uses SELECT FOR UPDATE to prevent
        concurrent modifications from producing an inconsistent balance.
        """
        # Lock the row for this user — prevents double-earning from
        # duplicate event delivery (at-least-once semantics)
        result = await db.execute(
            select(DriverPoints)
            .where(DriverPoints.user_id == user_id)
            .with_for_update()
        )
        wallet = result.scalar_one_or_none()

        if not wallet:
            wallet = DriverPoints(
                user_id=user_id, balance=POINTS_WELCOME_BONUS,
                total_earned=POINTS_WELCOME_BONUS, total_spent=0, version=1,
            )
            db.add(wallet)
            await db.flush()
            # Re-lock after insert
            result = await db.execute(
                select(DriverPoints)
                .where(DriverPoints.user_id == user_id)
                .with_for_update()
            )
            wallet = result.scalar_one()

        # Check idempotency: if we already recorded this journey's points, skip
        if journey_id:
            existing = await db.execute(
                select(PointsTransaction).where(
                    PointsTransaction.journey_id == journey_id,
                    PointsTransaction.reason == reason,
                )
            )
            if existing.scalar_one_or_none():
                logger.info(f"Points already awarded for journey {journey_id} ({reason}), skipping")
                return {"balance": wallet.balance, "awarded": 0}

        wallet.balance += amount
        wallet.total_earned += amount
        wallet.version += 1

        txn = PointsTransaction(
            id=str(uuid.uuid4()),
            user_id=user_id,
            journey_id=journey_id,
            amount=amount,
            reason=reason,
            balance_after=wallet.balance,
        )
        db.add(txn)
        await db.commit()

        logger.info(f"Awarded {amount} points to user {user_id} ({reason}). Balance: {wallet.balance}")
        return {"balance": wallet.balance, "awarded": amount}

    @staticmethod
    async def spend_points(
        db: AsyncSession, user_id: str, amount: int, reason: str, journey_id: str = None
    ) -> dict:
        """
        Deduct points from a driver. Uses SELECT FOR UPDATE to prevent
        double-spending — two concurrent spend requests will be serialized
        at the row lock level.

        Raises ValueError if insufficient balance.
        """
        result = await db.execute(
            select(DriverPoints)
            .where(DriverPoints.user_id == user_id)
            .with_for_update()
        )
        wallet = result.scalar_one_or_none()

        if not wallet:
            raise ValueError("Points wallet not found")

        if wallet.balance < amount:
            raise ValueError(
                f"Insufficient points: have {wallet.balance}, need {amount}"
            )

        wallet.balance -= amount
        wallet.total_spent += amount
        wallet.version += 1

        txn = PointsTransaction(
            id=str(uuid.uuid4()),
            user_id=user_id,
            journey_id=journey_id,
            amount=-amount,
            reason=reason,
            balance_after=wallet.balance,
        )
        db.add(txn)
        await db.commit()

        logger.info(f"Deducted {amount} points from user {user_id} ({reason}). Balance: {wallet.balance}")
        return {"balance": wallet.balance, "deducted": amount}

    @staticmethod
    async def get_transaction_history(
        db: AsyncSession, user_id: str, limit: int = 20
    ) -> list[dict]:
        """Get points transaction history for a user."""
        result = await db.execute(
            select(PointsTransaction)
            .where(PointsTransaction.user_id == user_id)
            .order_by(PointsTransaction.created_at.desc())
            .limit(limit)
        )
        txns = result.scalars().all()
        return [
            {
                "id": t.id,
                "amount": t.amount,
                "reason": t.reason,
                "journey_id": t.journey_id,
                "balance_after": t.balance_after,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in txns
        ]
