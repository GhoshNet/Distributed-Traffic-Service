"""
Journey Service - API routes.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .service import JourneyService
from shared.auth import get_current_user
from shared.schemas import (
    JourneyCreateRequest,
    JourneyResponse,
    JourneyListResponse,
    ErrorResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/journeys", tags=["Journeys"])


@router.post(
    "/",
    response_model=JourneyResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def create_journey(
    request: JourneyCreateRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Book a new journey. The system will check for conflicts before confirming."""
    try:
        return await JourneyService.create_journey(
            db, current_user["user_id"], request
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/",
    response_model=JourneyListResponse,
)
async def list_journeys(
    status: Optional[str] = Query(None, description="Filter by status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all journeys for the current user."""
    return await JourneyService.list_journeys(
        db, current_user["user_id"], status, page, page_size
    )


@router.get(
    "/{journey_id}",
    response_model=JourneyResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_journey(
    journey_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get details of a specific journey."""
    try:
        return await JourneyService.get_journey(db, journey_id, current_user["user_id"])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete(
    "/{journey_id}",
    response_model=JourneyResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def cancel_journey(
    journey_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a booked journey."""
    try:
        return await JourneyService.cancel_journey(
            db, journey_id, current_user["user_id"]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/vehicle/{vehicle_registration}/active",
    response_model=list[JourneyResponse],
)
async def get_active_vehicle_journeys(
    vehicle_registration: str,
    db: AsyncSession = Depends(get_db),
):
    """Get active journeys for a vehicle (used by enforcement service)."""
    return await JourneyService.get_active_journeys_for_vehicle(
        db, vehicle_registration
    )


@router.get(
    "/user/{user_id}/active",
    response_model=list[JourneyResponse],
)
async def get_active_user_journeys(
    user_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get active journeys for a user (used by enforcement service for license lookup)."""
    return await JourneyService.get_active_journeys_for_user(
        db, user_id
    )
