"""
Region registry client for the Journey Service.

Reads CONFLICT_REGIONS env var: comma-separated "REGION_ID:URL" pairs.
Example: IE:http://conflict-service-ie:8000,NI:http://conflict-service-ni:8000

Also reads ROUTE_REGION_MAP env var: comma-separated "route_id:REGION_ID" pairs.
REGION_ID may contain "-" to denote multi-region routes.
Example: dublin-galway:IE,dublin-cork:IE,dublin-belfast:IE-NI

get_regions_for_route(route_id) -> list of (region_id, url) pairs
get_all_regions() -> list of (region_id, url) pairs
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# IE:http://conflict-service-ie:8000,NI:http://conflict-service-ni:8000
CONFLICT_REGIONS_ENV = os.getenv(
    "CONFLICT_REGIONS",
    "IE:http://conflict-service-ie:8000"
)

# dublin-galway:IE,dublin-cork:IE,dublin-limerick:IE,...,dublin-belfast:IE-NI
ROUTE_REGION_MAP_ENV = os.getenv(
    "ROUTE_REGION_MAP",
    "dublin-galway:IE,dublin-cork:IE,dublin-limerick:IE,"
    "galway-limerick:IE,limerick-cork:IE,dublin-belfast:IE-NI"
)


def _parse_regions() -> dict[str, str]:
    """Parse CONFLICT_REGIONS into {region_id: base_url} dict."""
    regions: dict[str, str] = {}
    for part in CONFLICT_REGIONS_ENV.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            logger.warning("Invalid CONFLICT_REGIONS entry (missing ':'): %s", part)
            continue
        # Region ID is the first segment; URL may contain colons (http://...)
        region_id, _, url = part.partition(":")
        # url captured everything after the first colon — reconstruct full URL
        # e.g. "IE" + ":" + "http://conflict-service-ie:8000"
        full_url = url.strip()
        regions[region_id.strip()] = full_url
    return regions


def _parse_route_map() -> dict[str, list[str]]:
    """Parse ROUTE_REGION_MAP into {route_id: [region_id, ...]} dict."""
    route_map: dict[str, list[str]] = {}
    for part in ROUTE_REGION_MAP_ENV.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        route_id, _, region_str = part.partition(":")
        # Region string may be "IE-NI" for multi-region routes
        region_ids = [r.strip() for r in region_str.split("-") if r.strip()]
        route_map[route_id.strip()] = region_ids
    return route_map


# Parsed once at import time; safe since env vars don't change at runtime.
_REGIONS: dict[str, str] = _parse_regions()
_ROUTE_MAP: dict[str, list[str]] = _parse_route_map()


def get_regions_for_route(route_id: Optional[str]) -> list[tuple[str, str]]:
    """
    Returns list of (region_id, base_url) for a given route.

    For single-region routes (e.g. dublin-galway: IE) returns [(IE, url)].
    For cross-region routes (e.g. dublin-belfast: IE-NI) returns [(IE, url), (NI, url)].
    If route_id is None or not in the map, falls back to the primary IE region.
    """
    if route_id and route_id in _ROUTE_MAP:
        region_ids = _ROUTE_MAP[route_id]
        result = []
        for rid in region_ids:
            if rid in _REGIONS:
                result.append((rid, _REGIONS[rid]))
            else:
                logger.warning(
                    "Route %s references region %s but no URL configured for it",
                    route_id, rid
                )
        if result:
            return result

    # Fallback: primary IE region (first region configured)
    if _REGIONS:
        primary_id = next(iter(_REGIONS))
        return [(primary_id, _REGIONS[primary_id])]

    # Last resort: hardcoded IE default
    return [("IE", "http://conflict-service-ie:8000")]


def get_all_regions() -> list[tuple[str, str]]:
    """Returns all configured regions as (region_id, url) pairs."""
    return list(_REGIONS.items())
