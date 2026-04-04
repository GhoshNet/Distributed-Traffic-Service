"""
User Service - API routes.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .service import UserService
from shared.auth import get_current_user
from shared.schemas import (
    UserRegisterRequest,
    UserLoginRequest,
    UserResponse,
    TokenResponse,
    ErrorResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["Users"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def register(request: UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new driver account."""
    try:
        from shared.schemas import UserRole
        # Ensure default registrations are only DRIVERs
        if request.role != UserRole.DRIVER:
            request.role = UserRole.DRIVER
        return await UserService.register(db, request)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post(
    "/register/agent",
    response_model=UserResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def register_agent(request: UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new enforcement agent account."""
    try:
        from shared.schemas import UserRole
        request.role = UserRole.ENFORCEMENT_AGENT
        return await UserService.register(db, request)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}},
)
async def login(request: UserLoginRequest, db: AsyncSession = Depends(get_db)):
    """Login and receive a JWT access token."""
    try:
        return await UserService.login(db, request)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get(
    "/me",
    response_model=UserResponse,
    responses={401: {"model": ErrorResponse}},
)
async def get_my_profile(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current user's profile."""
    try:
        return await UserService.get_profile(db, current_user["user_id"])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get(
    "/license/{license_number}",
    response_model=UserResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_user_by_license(
    license_number: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a user by license number (used by enforcement service)."""
    try:
        return await UserService.get_user_by_license(db, license_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
