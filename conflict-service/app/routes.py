"""
Conflict Detection Service - API routes.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .service import ConflictDetectionService
from shared.schemas import (
    ConflictCheckRequest,
    ConflictCheckResponse,
    ErrorResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conflicts", tags=["Conflicts"])


@router.post(
    "/check",
    response_model=ConflictCheckResponse,
    responses={400: {"model": ErrorResponse}},
)
async def check_conflicts(
    request: ConflictCheckRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Check if a journey booking request conflicts with existing bookings.
    Called by the Journey Service during the booking saga.
    """
    try:
        return await ConflictDetectionService.check_conflicts(db, request)
    except Exception as e:
        logger.error(f"Conflict check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Conflict check failed: {str(e)}")


@router.post(
    "/cancel/{journey_id}",
    status_code=204,
)
async def cancel_booking_slot(
    journey_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a booking slot when a journey is cancelled."""
    await ConflictDetectionService.cancel_booking_slot(db, journey_id)
