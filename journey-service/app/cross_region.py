"""
Cross-Region Two-Phase Commit Coordinator

When a booking's origin and destination belong to different regions,
this coordinator drives 2PC across the participating region nodes.

Each region's journey-service exposes participant endpoints:
  POST /api/journeys/2pc/prepare  — Phase 1: hold/reserve, respond YES/NO
  POST /api/journeys/2pc/commit   — Phase 2a: confirm reservation
  POST /api/journeys/2pc/abort    — Phase 2b: release reservation

The initiating region is the Coordinator; all other involved regions are Participants.

Per the plan: PREPARE timeout = 5 s; if no YES within 5s → ABORT.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

PREPARE_TIMEOUT = 5.0   # plan specifies 5s timeout
COMMIT_TIMEOUT  = 10.0


class CrossRegionCoordinator:
    """
    Drive 2PC across multiple region nodes.

    Usage::

        ok, reason = await CrossRegionCoordinator.execute(
            txn_id="TXN-ABCD1234",
            participant_urls=["http://192.168.1.42:8002", "http://192.168.1.43:8002"],
            journey_data={...},
        )
    """

    @staticmethod
    async def execute(
        txn_id: str,
        participant_urls: list[str],
        journey_data: dict,
    ) -> tuple[bool, str]:
        """
        Execute 2PC across participant region nodes.
        Returns (committed, reason).
        """
        logger.info(f"[CrossRegion 2PC] TXN={txn_id} starting, participants={participant_urls}")

        votes = await CrossRegionCoordinator._phase1_prepare(txn_id, participant_urls, journey_data)

        if all(votes.values()):
            logger.info(f"[CrossRegion 2PC] TXN={txn_id} all YES → COMMIT")
            await CrossRegionCoordinator._phase2(txn_id, participant_urls, "commit")
            return True, "Cross-region booking committed via 2PC"
        else:
            no_voters = [u for u, v in votes.items() if not v]
            logger.warning(f"[CrossRegion 2PC] TXN={txn_id} NO from {no_voters} → ABORT")
            await CrossRegionCoordinator._phase2(txn_id, participant_urls, "abort")
            return False, "Cross-region booking aborted — a participant region rejected"

    @staticmethod
    async def _phase1_prepare(
        txn_id: str,
        participant_urls: list[str],
        journey_data: dict,
    ) -> dict[str, bool]:
        payload = {"txn_id": txn_id, "journey": journey_data, "phase": "PREPARE"}

        votes: dict[str, bool] = {}

        async def prepare_one(url: str):
            try:
                async with httpx.AsyncClient(timeout=PREPARE_TIMEOUT) as client:
                    resp = await client.post(
                        f"{url}/api/journeys/2pc/prepare",
                        json=payload,
                        headers={"X-2PC-Transaction-ID": txn_id},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        vote = data.get("vote") == "YES"
                        votes[url] = vote
                        logger.info(f"[CrossRegion 2PC] TXN={txn_id} PREPARE {url} → {data.get('vote')}")
                    else:
                        votes[url] = False
                        logger.warning(
                            f"[CrossRegion 2PC] TXN={txn_id} PREPARE {url} → HTTP {resp.status_code}"
                        )
            except httpx.TimeoutException:
                votes[url] = False
                logger.error(f"[CrossRegion 2PC] TXN={txn_id} PREPARE {url} timed out (>{PREPARE_TIMEOUT}s) → NO")
            except Exception as exc:
                votes[url] = False
                logger.error(f"[CrossRegion 2PC] TXN={txn_id} PREPARE {url} error: {exc}")

        await asyncio.gather(*[prepare_one(url) for url in participant_urls])
        return votes

    @staticmethod
    async def _phase2(txn_id: str, participant_urls: list[str], phase: str):
        payload = {"txn_id": txn_id, "phase": phase.upper()}

        async def send_one(url: str):
            try:
                async with httpx.AsyncClient(timeout=COMMIT_TIMEOUT) as client:
                    resp = await client.post(
                        f"{url}/api/journeys/2pc/{phase}",
                        json=payload,
                        headers={"X-2PC-Transaction-ID": txn_id},
                    )
                    logger.info(
                        f"[CrossRegion 2PC] TXN={txn_id} {phase.upper()} → {url}: {resp.status_code}"
                    )
            except Exception as exc:
                # Best-effort; log but do not raise (coordinator must proceed)
                logger.error(
                    f"[CrossRegion 2PC] TXN={txn_id} {phase.upper()} failed for {url}: {exc}"
                )

        await asyncio.gather(*[send_one(url) for url in participant_urls])
