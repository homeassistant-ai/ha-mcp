"""Shared HA container readiness checks for BAT and story tests.

Mirrors the stabilization used by E2E tests (conftest.py):
1. API reachable + components loaded (GET /api/config shows 50+ components)
2. Entities registered (GET /api/states shows 50+ entities)
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger("uat.ha_wait")

MIN_COMPONENTS = 50
MIN_ENTITIES = 50

API_TIMEOUT = 120
ENTITY_TIMEOUT = 30


def wait_for_ha_ready(url: str, token: str) -> None:
    """Wait until HA is fully ready: components loaded, entities registered.

    Raises TimeoutError if any gate is not reached within its timeout.
    """
    headers = {"Authorization": f"Bearer {token}"}

    # Gate 1: API reachable and components loaded
    logger.info(f"Waiting for HA at {url} ...")
    api_responded = False
    last_component_count = 0
    for attempt in range(API_TIMEOUT):
        try:
            r = requests.get(f"{url}/api/config", timeout=5, headers=headers)
            if r.status_code == 200:
                api_responded = True
                data = r.json()
                component_count = len(data.get("components", []))
                if component_count >= MIN_COMPONENTS:
                    version = data.get("version", "unknown")
                    logger.info(
                        f"HA stabilized: {component_count} components, "
                        f"version {version} ({attempt + 1}s)"
                    )
                    break
                if component_count != last_component_count:
                    logger.info(f"  {component_count} components loaded, waiting for {MIN_COMPONENTS}+...")
                    last_component_count = component_count
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Readiness check failed (retrying): %s", exc)
        time.sleep(1)
    else:
        if not api_responded:
            raise TimeoutError(f"HA API at {url} not reachable after {API_TIMEOUT}s")
        raise TimeoutError(
            f"HA component stabilization timed out after {API_TIMEOUT}s. "
            f"Only {last_component_count} components loaded (minimum: {MIN_COMPONENTS})."
        )

    # Gate 2: Entities registered
    logger.info("Waiting for HA entities to register...")
    last_entity_count = 0
    for attempt in range(ENTITY_TIMEOUT):
        try:
            r = requests.get(f"{url}/api/states", timeout=5, headers=headers)
            if r.status_code == 200:
                entity_count = len(r.json())
                if entity_count >= MIN_ENTITIES:
                    logger.info(f"HA ready: {entity_count} entities registered ({attempt + 1}s)")
                    break
                if entity_count != last_entity_count:
                    logger.info(f"  {entity_count} entities registered, waiting for {MIN_ENTITIES}+...")
                    last_entity_count = entity_count
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Readiness check failed (retrying): %s", exc)
        time.sleep(1)
    else:
        raise TimeoutError(
            f"Entity registration timed out after {ENTITY_TIMEOUT}s. "
            f"Only {last_entity_count} entities registered (minimum: {MIN_ENTITIES})."
        )
