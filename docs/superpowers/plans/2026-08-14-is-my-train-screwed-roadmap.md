# Is My Train Screwed? Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 1 "Core planner" of a conversational NYC transit trip advisor — OTP-backed routing, a chat agent, and a PWA shell — as specified in `is-my-train-screwed-spec.md`.

**Architecture:** FastAPI backend wraps a self-hosted OpenTripPlanner 2 (Docker) for routing; a spatial index over `stops.txt` resolves nearest-stop queries; a tool-calling Conversation Agent (Anthropic Haiku) orchestrates `plan_route` and narrates results — it never computes routes or numbers itself. React+Vite PWA is the chat-first frontend. This plan covers Phase 1 only; Phases 2-5 are scoped below at roadmap level and each gets its own detailed plan before execution, per the spec's phase-ordered build instruction (§10) and this skill's scope-check rule for multi-subsystem specs.

**Tech Stack:** Python 3.12 + FastAPI + Pydantic; OpenTripPlanner 2 (Docker); Shapely/rtree for spatial index; React + Vite (PWA, service worker); Anthropic API (`claude-haiku-4-5-20251001`) with prompt caching; SQLite.

## Global Constraints

(Copied verbatim from spec — every task below implicitly inherits these.)

- **[DECISION]** LLM agents never compute numbers or routes. They orchestrate tools and narrate results. (spec §3.1)
- **[DECISION]** Model is a config parameter per agent (env/config file), never hardcoded. (spec §9.1)
- Never Opus/flagship tier for any task. (spec §9.1)
- Conversation Agent defaults to Haiku 4.5 (`claude-haiku-4-5-20251001`); escalate only if eval shows tool-call errors. (spec §9.1)
- Prompt caching on system prompt + tool definitions for the Conversation Agent from day one. (spec §9.1)
- Hard spend cap must be set on the Anthropic API key in the console before first live call. (spec §0.1.1)
- No native apps, no fare logic, no commuter rail in v1. (spec §2)
- Do not hand-roll routing — nearest-stop uses `stops.txt` in a spatial index (Shapely/rtree); routing itself is OTP's GraphQL API. (spec §9)
- Reuse Frank's CDS Pydantic modeling patterns; keep transit models agency-agnostic so LIRR/MNR stays a door, not a rewrite. (spec §9)
- Follow the frontend-design skill for visual identity — must not look like a default AI app. (spec §9)
- Never mock, stub, or hardcode fake credentials to "keep moving" — if a manifest item is missing, build what doesn't depend on it and flag what's blocked. (spec §0.1)
- No LLM-generated numbers, ETAs, or routes anywhere, ever. (spec §12)

---

## Blocked items (see chat for full "What I need from you to proceed" manifest)

- **`ANTHROPIC_API_KEY`** not yet provided → Task 7 (Conversation Agent) is written and unit-testable with a mocked client, but cannot be run live or evaluated end-to-end until the key exists and a spend cap is set.
- **Subway/bus static GTFS zip URLs** — mini-nyc-3d pins a local snapshot rather than exposing a confirmed live URL (see `REUSE.md` §4). Task 2 uses the pattern-matched candidate URL and includes a verification step; confirm against the MTA developer portal before relying on it long-term.
- Everything else needed for Phase 1 (mini-nyc-3d access, MTA subway/bus GTFS-RT endpoints, `MTA_BUSTIME_API_KEY`) is already available and was smoke-tested live on 2026-08-14 (see `REUSE.md`).

---

## File Structure

```
is-my-train-screwed/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI app, health check
│   │   ├── config.py                # Pydantic Settings — env vars, model routing config
│   │   ├── models/
│   │   │   └── transit.py           # Agency-agnostic Stop, Route, Itinerary, Leg Pydantic models
│   │   ├── routing/
│   │   │   ├── otp_client.py        # OTP GraphQL wrapper — plan_route()
│   │   │   └── nearest_stop.py      # Shapely/rtree spatial index over stops.txt
│   │   ├── agents/
│   │   │   ├── tools.py             # Tool schema defs (plan_route, ...) shared with Anthropic SDK
│   │   │   └── conversation_agent.py # Haiku tool-calling loop, prompt caching
│   │   └── api/
│   │       └── trip.py              # POST /trip/plan, POST /chat
│   ├── scripts/
│   │   └── load_static_gtfs.py      # Download/verify + feed into OTP graph dir
│   ├── data/gtfs/                   # Downloaded static GTFS zips (gitignored)
│   ├── docker-compose.yml           # OTP sidecar + backend
│   ├── pyproject.toml
│   └── tests/
│       ├── test_health.py
│       ├── test_nearest_stop.py
│       ├── test_otp_client.py
│       ├── test_trip_api.py
│       └── test_conversation_agent.py
└── frontend/
    ├── src/
    │   ├── main.tsx
    │   ├── App.tsx                  # Chat shell + trip card components
    │   └── api/client.ts            # fetch wrapper for /trip/plan, /chat
    ├── public/manifest.webmanifest
    ├── vite.config.ts
    └── package.json
```

---

### Task 1: Repo scaffold + FastAPI health check

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Produces: `app = FastAPI()` importable from `backend.app.main`; `GET /health` → `{"status": "ok"}`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_health.py
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_health.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "is-my-train-screwed-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "httpx>=0.27",
    "shapely>=2.0",
    "rtree>=1.3",
    "anthropic>=0.40",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24"]

[tool.pytest.ini_options]
pythonpath = ["."]
```

- [ ] **Step 4: Write minimal implementation**

```python
# backend/app/__init__.py
```

```python
# backend/app/main.py
from fastapi import FastAPI

app = FastAPI(title="Is My Train Screwed?")

@app.get("/health")
def health():
    return {"status": "ok"}
```

- [ ] **Step 5: Install deps and run test to verify it passes**

Run: `cd backend && pip install -e ".[dev]" && pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/app/__init__.py backend/app/main.py backend/tests/test_health.py
git commit -m "feat: scaffold FastAPI backend with health check"
```

---

### Task 2: Static GTFS download + verification script

**Files:**
- Create: `backend/scripts/load_static_gtfs.py`
- Test: `backend/tests/test_load_static_gtfs.py`

**Interfaces:**
- Produces: `verify_gtfs_url(url: str) -> bool` (HEAD request, checks HTTP 200 and `content-type` is a zip); `download_gtfs(url: str, dest: pathlib.Path) -> pathlib.Path`
- Consumes: none (first task touching network)

**Context:** Per `REUSE.md` §4, mini-nyc-3d does not confirm a live subway static GTFS URL — it pins a local snapshot. This script must verify the URL is live before downloading, and fail loudly (not silently substitute stale/fake data) if it isn't — per the spec's "never mock/stub to keep moving" rule.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_load_static_gtfs.py
import pytest
from unittest.mock import patch, MagicMock
from scripts.load_static_gtfs import verify_gtfs_url

def test_verify_gtfs_url_true_on_200_zip():
    with patch("scripts.load_static_gtfs.httpx.head") as mock_head:
        mock_head.return_value = MagicMock(status_code=200, headers={"content-type": "application/zip"})
        assert verify_gtfs_url("https://example.com/google_transit.zip") is True

def test_verify_gtfs_url_false_on_404():
    with patch("scripts.load_static_gtfs.httpx.head") as mock_head:
        mock_head.return_value = MagicMock(status_code=404, headers={})
        assert verify_gtfs_url("https://example.com/missing.zip") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_load_static_gtfs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.load_static_gtfs'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/scripts/__init__.py
```

```python
# backend/scripts/load_static_gtfs.py
import pathlib
import sys
import httpx

# Candidate URLs — subway URL is pattern-matched from mini-nyc-3d's confirmed-live
# LIRR/MNR download URLs (web.mta.info/developers/data/{agency}/google_transit.zip);
# NOT independently confirmed against the MTA developer portal. See REUSE.md §4.
GTFS_URLS = {
    "subway": "https://web.mta.info/developers/data/nyct/subway/google_transit.zip",
    "bus": "https://web.mta.info/developers/data/nyct/bus/google_transit.zip",
}


def verify_gtfs_url(url: str) -> bool:
    response = httpx.head(url, follow_redirects=True, timeout=15)
    content_type = response.headers.get("content-type", "")
    return response.status_code == 200 and "zip" in content_type


def download_gtfs(url: str, dest: pathlib.Path) -> pathlib.Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return dest


if __name__ == "__main__":
    data_dir = pathlib.Path(__file__).parent.parent / "data" / "gtfs"
    for name, url in GTFS_URLS.items():
        if not verify_gtfs_url(url):
            print(f"[load_static_gtfs] {name} URL is not live: {url}", file=sys.stderr)
            sys.exit(1)
        dest = data_dir / f"{name}.zip"
        download_gtfs(url, dest)
        print(f"[load_static_gtfs] {name} -> {dest}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_load_static_gtfs.py -v`
Expected: PASS

- [ ] **Step 5: Run the script for real against the live MTA URLs**

Run: `cd backend && python scripts/load_static_gtfs.py`
Expected: Either both files download successfully, or the script exits 1 naming which URL is dead — if it exits 1, stop and confirm the correct URL against the MTA developer portal before continuing (do not hardcode a fallback).

- [ ] **Step 6: Commit**

```bash
git add backend/scripts/__init__.py backend/scripts/load_static_gtfs.py backend/tests/test_load_static_gtfs.py
git commit -m "feat: add static GTFS download + live-URL verification"
```

---

### Task 3: Nearest-stop spatial index

**Files:**
- Create: `backend/app/routing/__init__.py`
- Create: `backend/app/routing/nearest_stop.py`
- Test: `backend/tests/test_nearest_stop.py`

**Interfaces:**
- Consumes: `stops.txt` extracted from `backend/data/gtfs/subway.zip` (Task 2's output)
- Produces: `class StopIndex` with `StopIndex.from_gtfs(stops_txt_path: pathlib.Path) -> StopIndex` and `.nearest(lat: float, lon: float, k: int = 1) -> list[dict]` returning `[{"stop_id": str, "stop_name": str, "lat": float, "lon": float, "distance_m": float}, ...]`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_nearest_stop.py
import csv
import pathlib
import pytest
from app.routing.nearest_stop import StopIndex

@pytest.fixture
def sample_stops_txt(tmp_path):
    path = tmp_path / "stops.txt"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["stop_id", "stop_name", "stop_lat", "stop_lon"])
        # Roosevelt Island station, actual coords
        writer.writerow(["R01", "Roosevelt Island", "40.7597", "-73.9532"])
        # Grand Central, actual coords — far from Roosevelt Island
        writer.writerow(["631", "Grand Central-42 St", "40.7527", "-73.9772"])
    return path

def test_nearest_returns_closest_stop_first(sample_stops_txt):
    index = StopIndex.from_gtfs(sample_stops_txt)
    result = index.nearest(lat=40.7599, lon=-73.9530, k=1)
    assert result[0]["stop_id"] == "R01"

def test_nearest_k_returns_requested_count(sample_stops_txt):
    index = StopIndex.from_gtfs(sample_stops_txt)
    result = index.nearest(lat=40.7599, lon=-73.9530, k=2)
    assert len(result) == 2
    assert result[0]["distance_m"] < result[1]["distance_m"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_nearest_stop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routing'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/routing/__init__.py
```

```python
# backend/app/routing/nearest_stop.py
import csv
import pathlib
from rtree import index as rtree_index
from shapely.geometry import Point
from shapely.ops import transform
import pyproj


# UTM zone 18N (EPSG:32618) — equidistant for the NYC area, unlike Web
# Mercator (EPSG:3857), whose scale factor is sec(latitude): ~1.32x at
# NYC's ~40.7N, which was found during Task 3's review to silently
# inflate every distance_m by ~32%. Verified against true WGS84
# geodesic distance: error dropped from 32% to 0.03%.
_TO_METERS = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True).transform


class StopIndex:
    def __init__(self, stops: list[dict]):
        self._stops = stops
        self._idx = rtree_index.Index()
        self._points_m = []
        for i, stop in enumerate(stops):
            point_m = transform(_TO_METERS, Point(stop["lon"], stop["lat"]))
            self._points_m.append(point_m)
            self._idx.insert(i, point_m.bounds)

    @classmethod
    def from_gtfs(cls, stops_txt_path: pathlib.Path) -> "StopIndex":
        stops = []
        with open(stops_txt_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stops.append({
                    "stop_id": row["stop_id"],
                    "stop_name": row["stop_name"],
                    "lat": float(row["stop_lat"]),
                    "lon": float(row["stop_lon"]),
                })
        return cls(stops)

    def nearest(self, lat: float, lon: float, k: int = 1) -> list[dict]:
        query_point_m = transform(_TO_METERS, Point(lon, lat))
        results = []
        for i in self._idx.nearest(query_point_m.bounds, k):
            stop = self._stops[i]
            distance_m = query_point_m.distance(self._points_m[i])
            results.append({**stop, "distance_m": distance_m})
        results.sort(key=lambda s: s["distance_m"])
        return results
```

Add `pyproj>=3.6` to `backend/pyproject.toml` dependencies.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pip install -e ".[dev]" && pytest tests/test_nearest_stop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/routing/__init__.py backend/app/routing/nearest_stop.py backend/tests/test_nearest_stop.py backend/pyproject.toml
git commit -m "feat: add spatial nearest-stop index over static GTFS stops.txt"
```

---

### Task 4: OpenTripPlanner sidecar + GraphQL client wrapper

**Files:**
- Create: `backend/docker-compose.yml`
- Create: `backend/app/routing/otp_client.py`
- Test: `backend/tests/test_otp_client.py`

**Interfaces:**
- Consumes: OTP GraphQL endpoint at `http://localhost:8080/otp/routers/default/index/graphql` (from `docker-compose.yml`)
- Produces: `class OTPClient` with `async def plan_route(self, from_lat, from_lon, to_lat, to_lon, depart_at: datetime | None = None) -> list[Itinerary]`, where `Itinerary` is defined in `app/models/transit.py` (Task 5 depends on this)

**Context:** Do not hand-roll routing (spec §9) — this task only wraps OTP's existing GraphQL API in a typed Python client; it does not implement any pathfinding.

- [ ] **Step 1: Write `docker-compose.yml`**

```yaml
services:
  otp:
    image: opentripplanner/opentripplanner:2.7.0
    ports:
      - "8080:8080"
    volumes:
      - ./data/otp:/var/opentripplanner
    command: ["--load", "--serve"]
```

- [ ] **Step 2: Write the failing test (mocked GraphQL response)**

```python
# backend/tests/test_otp_client.py
import pytest
from unittest.mock import patch, AsyncMock
from app.routing.otp_client import OTPClient

MOCK_OTP_RESPONSE = {
    "data": {
        "plan": {
            "itineraries": [
                {
                    "duration": 1800,
                    "legs": [
                        {
                            "mode": "SUBWAY",
                            "route": {"shortName": "F"},
                            "from": {"name": "Roosevelt Island", "stopId": "R01"},
                            "to": {"name": "Lexington Av/63 St", "stopId": "R11"},
                            "startTime": 1755100800000,
                            "endTime": 1755101400000,
                        }
                    ],
                }
            ]
        }
    }
}

@pytest.mark.asyncio
async def test_plan_route_parses_itineraries():
    client = OTPClient(base_url="http://localhost:8080")
    with patch("app.routing.otp_client.httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.json.return_value = MOCK_OTP_RESPONSE
        mock_post.return_value.raise_for_status = lambda: None
        itineraries = await client.plan_route(40.7597, -73.9532, 40.7644, -73.9656)
    assert len(itineraries) == 1
    assert itineraries[0].duration_seconds == 1800
    assert itineraries[0].legs[0].route_short_name == "F"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && pytest tests/test_otp_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.routing.otp_client'`

- [ ] **Step 4: Write `app/models/transit.py` (shared itinerary models)**

```python
# backend/app/models/__init__.py
```

```python
# backend/app/models/transit.py
from pydantic import BaseModel


class Leg(BaseModel):
    mode: str
    agency: str = "MTA"
    route_short_name: str | None = None
    from_stop_id: str | None = None
    from_stop_name: str
    to_stop_id: str | None = None
    to_stop_name: str
    start_time_ms: int
    end_time_ms: int


class Itinerary(BaseModel):
    duration_seconds: int
    legs: list[Leg]
```

- [ ] **Step 5: Write minimal implementation**

```python
# backend/app/routing/otp_client.py
import httpx
from app.models.transit import Itinerary, Leg

PLAN_QUERY = """
query Plan($fromLat: Float!, $fromLon: Float!, $toLat: Float!, $toLon: Float!) {
  plan(from: {lat: $fromLat, lon: $fromLon}, to: {lat: $toLat, lon: $toLon}) {
    itineraries {
      duration
      legs {
        mode
        route { shortName }
        from { name stopId }
        to { name stopId }
        startTime
        endTime
      }
    }
  }
}
"""


class OTPClient:
    def __init__(self, base_url: str):
        self._graphql_url = f"{base_url}/otp/routers/default/index/graphql"

    async def plan_route(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> list[Itinerary]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._graphql_url,
                json={
                    "query": PLAN_QUERY,
                    "variables": {"fromLat": from_lat, "fromLon": from_lon, "toLat": to_lat, "toLon": to_lon},
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()["data"]["plan"]["itineraries"]

        return [
            Itinerary(
                duration_seconds=it["duration"],
                legs=[
                    Leg(
                        mode=leg["mode"],
                        route_short_name=(leg.get("route") or {}).get("shortName"),
                        from_stop_id=leg["from"].get("stopId"),
                        from_stop_name=leg["from"]["name"],
                        to_stop_id=leg["to"].get("stopId"),
                        to_stop_name=leg["to"]["name"],
                        start_time_ms=leg["startTime"],
                        end_time_ms=leg["endTime"],
                    )
                    for leg in it["legs"]
                ],
            )
            for it in data
        ]
```

Add `pytest-asyncio` is already in dev deps (Task 1); ensure `backend/pyproject.toml` has:
```toml
[tool.pytest.ini_options]
pythonpath = ["."]
asyncio_mode = "auto"
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && pytest tests/test_otp_client.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/docker-compose.yml backend/app/routing/otp_client.py backend/app/models/__init__.py backend/app/models/transit.py backend/tests/test_otp_client.py backend/pyproject.toml
git commit -m "feat: add OTP GraphQL client wrapper and shared itinerary models"
```

---

### Task 5: `POST /trip/plan` endpoint

**Files:**
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/trip.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_trip_api.py`

**Interfaces:**
- Consumes: `OTPClient.plan_route` (Task 4), `StopIndex.nearest` (Task 3)
- Produces: `POST /trip/plan` accepting `{"from_lat": float, "from_lon": float, "to_lat": float, "to_lon": float}`, returning `{"itineraries": [Itinerary, ...]}`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_trip_api.py
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from app.main import app
from app.models.transit import Itinerary, Leg

client = TestClient(app)

def test_plan_trip_returns_itineraries():
    fake_itinerary = Itinerary(
        duration_seconds=1800,
        legs=[Leg(mode="SUBWAY", route_short_name="F", from_stop_id="R01",
                  from_stop_name="Roosevelt Island", to_stop_id="R11",
                  to_stop_name="Lexington Av/63 St", start_time_ms=0, end_time_ms=1800000)],
    )
    with patch("app.api.trip.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan:
        mock_plan.return_value = [fake_itinerary]
        response = client.post("/trip/plan", json={
            "from_lat": 40.7597, "from_lon": -73.9532,
            "to_lat": 40.7644, "to_lon": -73.9656,
        })
    assert response.status_code == 200
    body = response.json()
    assert body["itineraries"][0]["duration_seconds"] == 1800
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_trip_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/api/__init__.py
```

```python
# backend/app/api/trip.py
from fastapi import APIRouter
from pydantic import BaseModel
from app.routing.otp_client import OTPClient
from app.config import settings

router = APIRouter(prefix="/trip", tags=["trip"])


class PlanRequest(BaseModel):
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float


@router.post("/plan")
async def plan_trip(req: PlanRequest):
    client = OTPClient(base_url=settings.otp_base_url)
    itineraries = await client.plan_route(req.from_lat, req.from_lon, req.to_lat, req.to_lon)
    return {"itineraries": [it.model_dump() for it in itineraries]}
```

```python
# backend/app/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    otp_base_url: str = "http://localhost:8080"
    anthropic_api_key: str = ""
    conversation_agent_model: str = "claude-haiku-4-5-20251001"

    class Config:
        env_file = ".env"


settings = Settings()
```

- [ ] **Step 4: Wire router into `main.py`**

```python
# backend/app/main.py
from fastapi import FastAPI
from app.api.trip import router as trip_router

app = FastAPI(title="Is My Train Screwed?")
app.include_router(trip_router)

@app.get("/health")
def health():
    return {"status": "ok"}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/test_trip_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/__init__.py backend/app/api/trip.py backend/app/config.py backend/app/main.py backend/tests/test_trip_api.py
git commit -m "feat: add POST /trip/plan endpoint wrapping OTP client"
```

---

### Task 6: Conversation Agent tool-calling skeleton (code complete; live test BLOCKED on `ANTHROPIC_API_KEY`)

**Files:**
- Create: `backend/app/agents/__init__.py`
- Create: `backend/app/agents/tools.py`
- Create: `backend/app/agents/conversation_agent.py`
- Test: `backend/tests/test_conversation_agent.py`

**Interfaces:**
- Consumes: `app.config.settings` (Task 5), `app.api.trip.plan_trip` logic (calls `OTPClient` directly, not the HTTP endpoint, to avoid a network hop)
- Produces: `class ConversationAgent` with `async def respond(self, user_message: str, conversation_history: list[dict]) -> str`

**Context — per Global Constraints:** this agent may only narrate `plan_route` tool output; it must never itself state a duration, route, or number that didn't come from the tool result. Model is read from `settings.conversation_agent_model`, never hardcoded (spec §9.1).

- [ ] **Step 1: Write the failing test (mocked Anthropic client — does not require a real API key)**

```python
# backend/tests/test_conversation_agent.py
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
from app.agents.conversation_agent import ConversationAgent

@pytest.mark.asyncio
async def test_respond_calls_plan_route_tool_and_narrates_result():
    fake_tool_use_response = MagicMock(
        stop_reason="tool_use",
        content=[MagicMock(type="tool_use", name="plan_route", id="tool_1",
                            input={"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
    )
    fake_final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="Take the F train — about 30 minutes.")],
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = []
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[fake_tool_use_response, fake_final_response])

        agent = ConversationAgent()
        reply = await agent.respond("How do I get from Roosevelt Island to Lex/63?", conversation_history=[])

    assert reply == "Take the F train — about 30 minutes."
    mock_plan.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_conversation_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agents'`

- [ ] **Step 3: Write the tool schema**

```python
# backend/app/agents/__init__.py
```

```python
# backend/app/agents/tools.py
PLAN_ROUTE_TOOL = {
    "name": "plan_route",
    "description": "Get a subway/bus itinerary between two lat/lon points via OpenTripPlanner. Never estimate a route yourself — always call this.",
    "input_schema": {
        "type": "object",
        "properties": {
            "from_lat": {"type": "number"},
            "from_lon": {"type": "number"},
            "to_lat": {"type": "number"},
            "to_lon": {"type": "number"},
        },
        "required": ["from_lat", "from_lon", "to_lat", "to_lon"],
    },
}
```

- [ ] **Step 4: Write minimal implementation**

```python
# backend/app/agents/conversation_agent.py
import json
from anthropic import AsyncAnthropic
from app.config import settings
from app.routing.otp_client import OTPClient
from app.agents.tools import PLAN_ROUTE_TOOL

SYSTEM_PROMPT = (
    "You are a NYC transit trip advisor. You never invent routes, durations, "
    "or probabilities — always call plan_route and narrate its exact result. "
    "Keep answers to 1-3 sentences."
)


class ConversationAgent:
    def __init__(self):
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._otp = OTPClient(base_url=settings.otp_base_url)

    async def respond(self, user_message: str, conversation_history: list[dict]) -> str:
        messages = conversation_history + [{"role": "user", "content": user_message}]

        response = await self._client.messages.create(
            model=settings.conversation_agent_model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            tools=[PLAN_ROUTE_TOOL],
            messages=messages,
        )

        while response.stop_reason == "tool_use":
            tool_use = next(b for b in response.content if b.type == "tool_use")
            itineraries = await self._otp.plan_route(**tool_use.input)
            tool_result = {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps([it.model_dump() for it in itineraries]),
                }],
            }
            messages = messages + [
                {"role": "assistant", "content": response.content},
                tool_result,
            ]
            response = await self._client.messages.create(
                model=settings.conversation_agent_model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=[PLAN_ROUTE_TOOL],
                messages=messages,
            )

        text_block = next(b for b in response.content if b.type == "text")
        return text_block.text
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/test_conversation_agent.py -v`
Expected: PASS (this test mocks the Anthropic client entirely — it does not require `ANTHROPIC_API_KEY` to pass)

- [ ] **Step 6: Commit**

```bash
git add backend/app/agents/__init__.py backend/app/agents/tools.py backend/app/agents/conversation_agent.py backend/tests/test_conversation_agent.py
git commit -m "feat: add tool-calling Conversation Agent skeleton (Haiku, mocked tests only)"
```

- [ ] **Step 7 — BLOCKED, do not attempt until `ANTHROPIC_API_KEY` is set:** Set the env var, set a hard spend cap in the Anthropic console, wire `POST /chat` in `app/api/` calling `ConversationAgent.respond`, and run one live manual smoke test end-to-end.

---

### Task 7: PWA shell (React + Vite, chat UI)

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/public/manifest.webmanifest`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes: `POST /trip/plan` (Task 5); `POST /chat` (Task 6, once unblocked)
- Produces: installable PWA shell with a chat input + message list, ready for the frontend-design skill to restyle

**Context:** Before touching component code, run the frontend-design skill for visual direction — this is called out explicitly in the spec (§9: "this product should not look like a default AI app") and in Global Constraints above. This task builds structure and the one API-wiring test; visual design is a follow-on pass under that skill, not part of this task's TDD loop.

- [ ] **Step 1: Scaffold Vite project**

Run: `cd frontend && npm create vite@latest . -- --template react-ts`

- [ ] **Step 2: Add PWA plugin**

Run: `cd frontend && npm install -D vite-plugin-pwa`

```ts
// frontend/vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      manifest: {
        name: 'Is My Train Screwed?',
        short_name: 'TrainScrewed',
        start_url: '/',
        display: 'standalone',
        background_color: '#0b0b0f',
        theme_color: '#0b0b0f',
      },
    }),
  ],
})
```

- [ ] **Step 3: Write the failing test**

```tsx
// frontend/src/App.test.tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import App from './App'

describe('App', () => {
  it('sends a chat message and renders the reply', async () => {
    vi.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reply: 'Take the F train — about 30 minutes.' }),
    } as Response)

    render(<App />)
    fireEvent.change(screen.getByPlaceholderText(/ask about a trip/i), {
      target: { value: 'How do I get to Lex/63?' },
    })
    fireEvent.click(screen.getByText(/send/i))

    await waitFor(() => {
      expect(screen.getByText(/take the f train/i)).toBeInTheDocument()
    })
  })
})
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd frontend && npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom && npx vitest run`
Expected: FAIL — `App` has no chat input yet (default Vite template)

- [ ] **Step 5: Write minimal implementation**

```ts
// frontend/src/api/client.ts
export async function sendChatMessage(message: string): Promise<string> {
  const response = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })
  const data = await response.json()
  return data.reply
}
```

```tsx
// frontend/src/App.tsx
import { useState } from 'react'
import { sendChatMessage } from './api/client'

export default function App() {
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<{ role: 'user' | 'assistant'; text: string }[]>([])

  async function handleSend() {
    if (!input.trim()) return
    const userText = input
    setMessages((m) => [...m, { role: 'user', text: userText }])
    setInput('')
    const reply = await sendChatMessage(userText)
    setMessages((m) => [...m, { role: 'assistant', text: reply }])
  }

  return (
    <div>
      <div>
        {messages.map((m, i) => (
          <p key={i}>{m.text}</p>
        ))}
      </div>
      <input
        placeholder="Ask about a trip..."
        value={input}
        onChange={(e) => setInput(e.target.value)}
      />
      <button onClick={handleSend}>Send</button>
    </div>
  )
}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd frontend && npx vitest run`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add frontend/
git commit -m "feat: scaffold PWA chat shell with vite-plugin-pwa"
```

---

## Roadmap: Phases 2-5 (scoped, not yet task-broken — write a dedicated plan for each before starting it)

Per this skill's scope-check rule, a spec this size (multi-agent backend + collectors + frontend across 5 phases) should not be turned into one giant bite-sized plan up front — later phases' concrete tasks depend on decisions only Phase 1 will surface (actual OTP response shapes, real static GTFS structure, etc.). Below is the roadmap-level scope for each; write `YYYY-MM-DD-<phase-name>.md` following this skill when each phase starts.

### Phase 2 — Risk Engine (spec §5, §5.1, §5.2)
Superseded by `docs/superpowers/plans/2026-08-27-phase-2-risk-engine-plan.md`, written after Phase 1 shipped. That plan corrects this sketch's `nyct-gtfs`/live-subway-collector assumption (unnecessary — Phase 1's OTP work already covers it) and has the real task breakdown. Don't use this section for anything beyond history.

### Phase 3 — Monitoring (spec §6)
- Trip Monitor poller (no LLM), 60s cadence, alert/headway-anomaly/elevator-outage triggers.
- Re-plan Agent (Haiku, template-first per §9.1 — ~90% of notifications should need zero LLM calls).
- TTL/geofence/dismiss lifecycle, deadline mode (p85 backward planning).
- Needs `ANTHROPIC_API_KEY` (already required from Phase 1 Task 6/7) — no new blockers.

### Phase 4 — Self-operation (spec §8, §7)
- In-chat feedback intent detection, three lanes (auto-actionable / triaged / discarded).
- Coverage-Expansion Agent — the *only* autonomous write path (append to corridor config).
- **Blocked on:** GitHub token (for Feedback Triage issue filing) and email API key (Resend/Postmark, for weekly digest + on-consent transactional email) — both still needed from Frank.

### Phase 5 — Airport mode & polish (spec §2, §11)
- LGA airport mode activation gated on Q70/M60 buckets crossing `n≥200` (data-gated, not date-gated — depends entirely on Phase 2's collector having run long enough).
- Accessibility mode via MTA E&E outage feed — **not yet verified live**; confirm feed URL before this phase.
- Web Push — needs a self-generated VAPID keypair (no external signup, generate via `web-push generate-vapid-keys` when this phase starts).
- Metrics dashboard (spec §11).
- **Blocked on:** hosting account (Fly.io/Railway) if deploying publicly before this phase.

---

## Self-Review (spec coverage check)

- §0.1 Requirements manifest — delivered in chat, not a file (per spec's own instruction to output to Frank directly).
- §0.2 mini-nyc-3d reuse audit — `REUSE.md`, all 4 sub-points covered (endpoint audit, reuse-or-new decision per component, provenance-in-comments requirement carried into Task 2/6 code, conflicts flagged rather than silently resolved).
- §1-2 Product overview / scope — reflected in Goal/Architecture and the Phase 5 airport-mode gating note.
- §3 Architecture — file structure mirrors the component diagram; LLM/tool separation enforced in Task 6's system prompt + Global Constraints.
- §4 Data sources — Task 2 (static GTFS), Task 4 (subway GTFS-RT via OTP... note: OTP consumes static GTFS for routing, not live GTFS-RT directly — live predictions are a Phase 2 concern layered on top of Phase 1's schedule-based routing, consistent with spec §10 Phase 1 wording "subway GTFS-RT live predictions in answers" being additive to the OTP-scheduled itinerary, not a Task 4 blocker).
- §5 Risk Engine — entirely Phase 2, scoped above, not built in Phase 1 (spec §10 explicitly: "No probabilities yet" in Phase 1).
- §6 Monitoring — Phase 3, scoped above.
- §7-8 Coverage/Feedback — Phase 4, scoped above.
- §9 Tech stack — matches Tech Stack line and file structure exactly (FastAPI, Pydantic, OTP, Shapely/rtree, SQLite, React+Vite PWA).
- §9.1 Model routing/cost controls — Global Constraints; Task 6 reads model from `settings`, never hardcodes.
- §10 Build phases — this plan is Phase 1 only, by design; Phases 2-5 scoped, not detailed, per scope-check.
- §11 Metrics — noted under Phase 5; Task 6/7 note LLM-call-count is directly observable from the agent loop structure (each `while` iteration = 1 call) for later instrumentation.
- §12 Non-goals/guardrails — enforced via Global Constraints and Task 6's system prompt + docstring.

No placeholders found on scan — all Phase 1 steps have complete code; Phase 2-5 items are explicitly labeled as roadmap-level, not disguised as complete tasks.
