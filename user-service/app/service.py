"""
User Service - Business logic layer.
"""

import uuid
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from passlib.context import CryptContext

from .database import User
from shared.auth import create_access_token
from shared.schemas import (
    UserRegisterRequest,
    UserLoginRequest,
    UserResponse,
    TokenResponse,
)

logger = logging.getLogger(__name__)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserService:
    """Handles user registration, authentication, and profile management."""

    @staticmethod
    async def register(db: AsyncSession, request: UserRegisterRequest) -> UserResponse:
        """Register a new user."""
        # Check if email already exists
        existing = await db.execute(
            select(User).where(User.email == request.email)
        )
        if existing.scalar_one_or_none():
            raise ValueError("Email already registered")

        # Check if license already exists
        existing_license = await db.execute(
            select(User).where(User.license_number == request.license_number)
        )
        if existing_license.scalar_one_or_none():
            raise ValueError("License number already registered")

        # Create user
        user = User(
            id=str(uuid.uuid4()),
            email=request.email,
            password_hash=pwd_context.hash(request.password),
            full_name=request.full_name,
            license_number=request.license_number,
            role=request.role.value,
        )

        db.add(user)
        await db.commit()
        await db.refresh(user)

        logger.info(f"User registered: {user.id} ({user.email})")

        return UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            license_number=user.license_number,
            role=user.role,
            created_at=user.created_at,
        )

    @staticmethod
    async def login(db: AsyncSession, request: UserLoginRequest) -> TokenResponse:
        """Authenticate a user and return a JWT token."""
        result = await db.execute(
            select(User).where(User.email == request.email)
        )
        user = result.scalar_one_or_none()

        if not user or not pwd_context.verify(request.password, user.password_hash):
            raise ValueError("Invalid email or password")

        if not user.is_active:
            raise ValueError("Account is deactivated")

        token, expires_in = create_access_token(
            user_id=user.id,
            email=user.email,
            license_number=user.license_number,
            role=user.role,
        )

        logger.info(f"User logged in: {user.id}")

        return TokenResponse(
            access_token=token,
            token_type="bearer",
            expires_in=expires_in,
        )

    @staticmethod
    async def get_profile(db: AsyncSession, user_id: str) -> UserResponse:
        """Get a user's profile by ID."""
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        return UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            license_number=user.license_number,
            role=user.role,
            created_at=user.created_at,
        )

    @staticmethod
    async def get_user_by_license(db: AsyncSession, license_number: str) -> UserResponse:
        """Get a user by their license number (used by enforcement)."""
        result = await db.execute(
            select(User).where(User.license_number == license_number)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise ValueError("User not found")

        return UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            license_number=user.license_number,
            role=user.role,
            created_at=user.created_at,
        )

    # ==========================================
    # Vehicle Management
    # ==========================================

    @staticmethod
    async def register_vehicle(
        db: AsyncSession, user_id: str, registration: str, vehicle_type: str
    ):
        """Register a vehicle to a user."""
        from .database import Vehicle

        # Check if registration already taken
        existing = await db.execute(
            select(Vehicle).where(Vehicle.registration == registration.upper())
        )
        if existing.scalar_one_or_none():
            raise ValueError("Vehicle registration already registered to a user")

        vehicle = Vehicle(
            id=str(uuid.uuid4()),
            user_id=user_id,
            registration=registration.upper(),
            vehicle_type=vehicle_type,
        )
        db.add(vehicle)
        await db.commit()
        await db.refresh(vehicle)

        logger.info(f"Vehicle {registration} registered to user {user_id}")
        return vehicle

    @staticmethod
    async def list_vehicles(db: AsyncSession, user_id: str):
        """List all vehicles belonging to a user."""
        from .database import Vehicle

        result = await db.execute(
            select(Vehicle).where(Vehicle.user_id == user_id).order_by(Vehicle.created_at.desc())
        )
        return result.scalars().all()

    @staticmethod
    async def delete_vehicle(db: AsyncSession, user_id: str, vehicle_id: str):
        """Remove a vehicle from a user's account."""
        from .database import Vehicle

        result = await db.execute(
            select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.user_id == user_id)
        )
        vehicle = result.scalar_one_or_none()
        if not vehicle:
            raise ValueError("Vehicle not found")

        await db.delete(vehicle)
        await db.commit()
        logger.info(f"Vehicle {vehicle.registration} removed from user {user_id}")

    @staticmethod
    async def verify_vehicle_ownership(
        db: AsyncSession, user_id: str, registration: str
    ) -> bool:
        """Check if a vehicle registration belongs to a specific user."""
        from .database import Vehicle

        result = await db.execute(
            select(Vehicle).where(
                Vehicle.registration == registration.upper(),
                Vehicle.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None
