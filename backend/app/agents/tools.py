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

CREATE_MONITORED_TRIP_TOOL = {
    "name": "create_monitored_trip",
    "description": "Start monitoring the itinerary you just planned for disruptions or deadline risk. Only call this after the user has agreed to be monitored — never proactively without asking first.",
    "input_schema": {
        "type": "object",
        "properties": {
            "itinerary_index": {
                "type": "integer",
                "description": "Which itinerary from the most recent plan_route result to monitor (0 = first/default).",
            },
            "deadline_ts": {
                "type": "integer",
                "description": "If the user gave a specific deadline (flight, interview, etc.), the epoch-millisecond timestamp for that deadline. Omit entirely if no deadline was given — never guess one.",
            },
        },
        "required": [],
    },
}

CANCEL_MONITORED_TRIP_TOOL = {
    "name": "cancel_monitored_trip",
    "description": "Stop monitoring a trip — call this when the user says they've arrived, no longer need monitoring, or explicitly asks to cancel. If the user has more than one active monitored trip and it's unclear which they mean, call this tool with no trip_id first — it will tell you the active trips so you can ask the user to pick one, then call again with the specific trip_id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "trip_id": {
                "type": "integer",
                "description": "The specific trip to cancel, if known. Omit if you don't have a specific id yet.",
            },
        },
        "required": [],
    },
}
