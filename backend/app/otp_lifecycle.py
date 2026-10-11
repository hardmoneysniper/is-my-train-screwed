"""backend/app/otp_lifecycle.py

Wake/sleep control for the otp Railway service, on demand (2026-10-11,
user: "the otp should be sleeping if no requests are being made, and
only up and run if a routing request is made"). Not Railway's own
sleepApplication feature -- that wakes on traffic through a service's
PUBLIC domain, and otp has none (it's reachable only over private
networking from this service), so native sleep could never wake it.
This is a small, explicit replacement: call ensure_otp_awake() before
any OTP call, mark_otp_used() after a successful one, and run
stop_otp_if_idle() periodically from main.py's lifespan.

Measured cold-start timing (2026-10-11, real test against this exact
service): ~21-31s from triggering a restart to OTP serving a real,
graph-loaded routing query. ENSURE_AWAKE_MAX_WAIT_SECONDS is kept under
the ~30s a browser/proxy commonly allows before timing out a request, so
a caller that waits inline never risks hitting that ceiling itself --
if OTP isn't ready within the budget, callers get an honest "still
starting" signal instead of hanging past it.
"""
import asyncio
import time

import httpx

from app.config import settings

RAILWAY_GRAPHQL_URL = "https://backboard.railway.app/graphql/v2"

ENSURE_AWAKE_MAX_WAIT_SECONDS = 25
ENSURE_AWAKE_POLL_INTERVAL_SECONDS = 3
IDLE_STOP_AFTER_SECONDS = 10 * 60  # 2026-10-11 (user): sleep when unused

_last_used_at: float | None = None
_wake_triggered_at: float | None = None
_WAKE_RETRIGGER_COOLDOWN_SECONDS = 15  # don't re-trigger a deploy on every
# concurrent/retried call while one wake is already in flight


def _otp_graphql_url() -> str:
    return f"{settings.otp_base_url}/otp/routers/default/index/graphql"


async def _railway_graphql(query: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            RAILWAY_GRAPHQL_URL,
            headers={"Authorization": f"Bearer {settings.railway_api_token}"},
            json={"query": query},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()


async def is_otp_reachable() -> bool:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(_otp_graphql_url(), json={"query": "{ __typename }"}, timeout=4)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def _current_otp_deployment_id() -> str | None:
    result = await _railway_graphql(
        f'{{ service(id: "{settings.railway_otp_service_id}") '
        '{ deployments(first: 1) { edges { node { id status } } } } }'
    )
    edges = result.get("data", {}).get("service", {}).get("deployments", {}).get("edges", [])
    return edges[0]["node"]["id"] if edges else None


async def wake_otp() -> None:
    """Fire-and-forget restart trigger. Safe to call repeatedly while a
    wake is already in flight -- the cooldown just avoids spamming
    Railway's API with duplicate deploy triggers from concurrent callers,
    it doesn't change correctness if it fires twice."""
    global _wake_triggered_at
    now = time.monotonic()
    if _wake_triggered_at is not None and now - _wake_triggered_at < _WAKE_RETRIGGER_COOLDOWN_SECONDS:
        return
    _wake_triggered_at = now
    await _railway_graphql(
        f'mutation {{ serviceInstanceDeploy(serviceId: "{settings.railway_otp_service_id}", '
        f'environmentId: "{settings.railway_environment_id}", latestCommit: true) }}'
    )


def mark_otp_used() -> None:
    global _last_used_at
    _last_used_at = time.monotonic()


async def ensure_otp_awake() -> bool:
    """Returns True once otp is confirmed reachable (either it already
    was, or a triggered wake completed within budget), False if it's
    still starting after ENSURE_AWAKE_MAX_WAIT_SECONDS -- callers must
    treat False as an honest "still starting up" state, never proceed to
    call OTP anyway or fabricate a result."""
    if await is_otp_reachable():
        mark_otp_used()
        return True

    await wake_otp()
    deadline = time.monotonic() + ENSURE_AWAKE_MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(ENSURE_AWAKE_POLL_INTERVAL_SECONDS)
        if await is_otp_reachable():
            mark_otp_used()
            return True
    return False


async def stop_otp_if_idle() -> None:
    """Called periodically (main.py's lifespan). Only stops otp if it's
    both idle AND actually currently running -- querying its real
    deployment id fresh each time since a prior wake/stop changes it, and
    skipping entirely (no Railway call at all) when nothing has used OTP
    yet this process lifetime, matching "only up and run if a routing
    request is made" for a cold start too."""
    if _last_used_at is None:
        return
    if time.monotonic() - _last_used_at < IDLE_STOP_AFTER_SECONDS:
        return
    if not await is_otp_reachable():
        return  # already stopped -- nothing to do
    deployment_id = await _current_otp_deployment_id()
    if deployment_id is None:
        return
    await _railway_graphql(f'mutation {{ deploymentStop(id: "{deployment_id}") }}')


def reset_for_tests() -> None:
    global _last_used_at, _wake_triggered_at
    _last_used_at = None
    _wake_triggered_at = None
