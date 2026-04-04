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
