"""
User Service - API routes.
"""

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .database import get_db, User as UserModel
from .service import UserService
from .replication import (
    acquire_distributed_lock, release_distributed_lock,
    replicate_user, replicate_vehicle, shard_for_email,
)
from shared.auth import get_current_user
from shared.messaging import get_broker
from shared.schemas import (
    UserRegisterRequest,
    UserLoginRequest,
    UserResponse,
    TokenResponse,
    ErrorResponse,
    VehicleRegisterRequest,
    VehicleResponse,
    VehicleListResponse,
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
    """
    Register a new driver account.

    DS flow:
      1. Compute shard assignment (consistent hash on email)
      2. Acquire distributed lock (local Redis SETNX + peer coordination)
      3. Register locally inside a serializable transaction
      4. Release distributed lock
      5. Replicate user to all peers (async, non-blocking)
    """
    from shared.schemas import UserRole
    if request.role != UserRole.DRIVER:
        request.role = UserRole.DRIVER

    # Step 1 — shard assignment (logged for demo visibility)
    shard_id, home = shard_for_email(request.email)
    logger.info(
        f"[shard] register email={request.email} → shard={shard_id} home={home}"
    )

    # Step 2 — distributed lock
    acquired, redis_conn = await acquire_distributed_lock(request.email)
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail="Email registration is currently in progress on another node, or the email is already taken. Please try again.",
        )

    try:
        # Step 3 — local registration
        user = await UserService.register(db, request)

        # Publish event (best-effort)
        try:
            broker = await get_broker()
            await broker.publish(
                routing_key="user.registered",
                data={
                    "user_id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                    "license_number": user.license_number,
                    "registered_at": datetime.utcnow().isoformat(),
                },
            )
        except Exception as e:
            logger.warning(f"Could not publish user.registered event: {e}")

        # Step 4 — release lock
        await release_distributed_lock(request.email, redis_conn)
        redis_conn = None  # prevent double-release in finally

        # Step 5 — fetch ORM object for password_hash and replicate async
        user_orm = (await db.execute(
            select(UserModel).where(UserModel.id == user.id)
        )).scalar_one()
        asyncio.create_task(replicate_user({
            "id": user_orm.id,
            "email": user_orm.email,
            "password_hash": user_orm.password_hash,
            "full_name": user_orm.full_name,
            "license_number": user_orm.license_number,
            "role": user_orm.role,
            "is_active": user_orm.is_active,
        }))

        return user
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        # Ensure lock is always released on any exception
        if redis_conn is not None:
            await release_distributed_lock(request.email, redis_conn)


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


# ==========================================
# Vehicle Management
# ==========================================

@router.post(
    "/vehicles",
    response_model=VehicleResponse,
    status_code=201,
    responses={409: {"model": ErrorResponse}},
)
async def register_vehicle(
    request: VehicleRegisterRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a vehicle to the current user."""
    try:
        vehicle = await UserService.register_vehicle(
            db, current_user["user_id"], request.registration, request.vehicle_type.value
        )
        # Replicate vehicle to all peers async
        asyncio.create_task(replicate_vehicle({
            "id": vehicle.id,
            "user_id": vehicle.user_id,
            "registration": vehicle.registration,
            "vehicle_type": vehicle.vehicle_type,
        }))
        return VehicleResponse(
            id=vehicle.id,
            user_id=vehicle.user_id,
            registration=vehicle.registration,
            vehicle_type=vehicle.vehicle_type,
            created_at=vehicle.created_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get(
    "/vehicles",
    response_model=VehicleListResponse,
)
async def list_vehicles(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all vehicles belonging to the current user."""
    vehicles = await UserService.list_vehicles(db, current_user["user_id"])
    return VehicleListResponse(
        vehicles=[
            VehicleResponse(
                id=v.id, user_id=v.user_id, registration=v.registration,
                vehicle_type=v.vehicle_type, created_at=v.created_at,
            )
            for v in vehicles
        ]
    )


@router.delete(
    "/vehicles/{vehicle_id}",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
)
async def delete_vehicle(
    vehicle_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a vehicle from the current user's account."""
    try:
        await UserService.delete_vehicle(db, current_user["user_id"], vehicle_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get(
    "/vehicles/verify/{registration}",
)
async def verify_vehicle_ownership(
    registration: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Internal endpoint: verify that a vehicle registration belongs to a user."""
    is_owner = await UserService.verify_vehicle_ownership(db, user_id, registration)
    return {"is_owner": is_owner, "registration": registration.upper(), "user_id": user_id}

