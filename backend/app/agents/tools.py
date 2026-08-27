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
