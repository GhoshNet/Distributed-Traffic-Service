"""
Journey Service - API routes.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from .database import get_db, get_read_db, Journey
from .service import JourneyService
from shared.auth import get_current_user, require_role
from shared.schemas import (
    JourneyCreateRequest,
    JourneyResponse,
    JourneyListResponse,
    ErrorResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/journeys", tags=["Journeys"])


def _check_node_not_failed():
    """Raise 503 if this node is in simulated failure mode."""
    from . import main as _main  # noqa: PLC0415
    if getattr(_main, "_node_failed", False):
        raise HTTPException(status_code=503, detail="Node is in simulated failure — bookings rejected")


@router.post(
    "/",
    response_model=JourneyResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def create_journey(
    request: JourneyCreateRequest,
    mode: Optional[str] = Query(
        default=None,
        description="Booking protocol: 'saga' (default) or '2pc' (Two-Phase Commit)",
    ),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Book a new journey.

    Use **?mode=2pc** to use the Two-Phase Commit coordinator (stronger consistency)
    instead of the default Saga pattern. Both paths check road capacity via the
    Conflict Service; 2PC adds explicit compensating cancellation on commit failure.
    """
    _check_node_not_failed()
    try:
        return await JourneyService.create_journey(
            db, current_user["user_id"], request, use_2pc=(mode == "2pc")
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
    db: AsyncSession = Depends(get_read_db),
):
    """List all journeys for the current user."""
    return await JourneyService.list_journeys(
        db, current_user["user_id"], status, page, page_size
    )

@router.get(
    "/all",
    response_model=JourneyListResponse,
    dependencies=[Depends(require_role("ADMIN"))],
)
async def list_all_journeys(
    status: Optional[str] = Query(None, description="Filter by status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_read_db),
):
    """Admin-only: list all journeys from all users."""
    query = select(Journey)
    if status:
        query = query.where(Journey.status == status)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    query = query.order_by(Journey.departure_time.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    journeys = result.scalars().all()

    return JourneyListResponse(
        journeys=[JourneyService._to_response(j) for j in journeys],
        total=total,
        page=page,
        page_size=page_size,
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


@router.get("/points/balance")
async def get_points_balance(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current driver's points balance."""
    from .points import PointsService
    return await PointsService.get_balance(db, current_user["user_id"])


@router.get("/points/history")
async def get_points_history(
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current driver's points transaction history."""
    from .points import PointsService
    transactions = await PointsService.get_transaction_history(
        db, current_user["user_id"], limit
    )
    return {"transactions": transactions, "count": len(transactions)}


@router.post("/points/spend")
async def spend_points(
    amount: int = Query(..., gt=0, description="Points to spend"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Spend points (e.g. for priority booking).
    Uses SELECT FOR UPDATE to prevent double-spending.
    """
    from .points import PointsService
    try:
        return await PointsService.spend_points(
            db, current_user["user_id"], amount, "MANUAL_SPEND"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/vehicle/{vehicle_registration}/active",
    response_model=list[JourneyResponse],
)
async def get_active_vehicle_journeys(
    vehicle_registration: str,
    db: AsyncSession = Depends(get_read_db),
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
    db: AsyncSession = Depends(get_read_db),
):
    """Get active journeys for a user (used by enforcement service for license lookup)."""
    return await JourneyService.get_active_journeys_for_user(
        db, user_id
    )
