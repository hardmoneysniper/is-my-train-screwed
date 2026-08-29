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

FIND_STOP_TOOL = {
    "name": "find_stop",
    "description": "Look up a subway or bus stop by name or partial name (e.g. 'Roosevelt Island', '86 St') to get its coordinates. Call this when the user names a place instead of giving exact coordinates, then use the returned stop's lat/lon with plan_route. Never guess coordinates yourself.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The place name or partial name to search for."},
        },
        "required": ["query"],
    },
}

GET_RISK_TOOL = {
    "name": "get_risk",
    "description": "Check transfer-miss probability for the itinerary you just planned. Never estimate this yourself — always call this tool and narrate its exact result.",
    "input_schema": {
        "type": "object",
        "properties": {
            "itinerary_index": {
                "type": "integer",
                "description": "Which itinerary from the most recent plan_route result to check (0 = first/default).",
            },
        },
        "required": [],
    },
}
