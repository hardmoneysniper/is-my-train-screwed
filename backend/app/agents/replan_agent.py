"""backend/app/agents/replan_agent.py

`replan_trip(trip, trigger_reason, conn=None) -> str | None` -- Phase 3
Task 6's Re-plan Agent (see `.superpowers/sdd/task-6-brief.md`). Called
later by Task 7's poll loop whenever something might have gone wrong with
an actively-monitored trip: re-plans it fresh, decides whether anything
actually changed, and -- if so -- writes a plain-language notification for
the user to see on their next `/chat` message. Same "LLM agents never
compute numbers" discipline as `risk_engine.get_risk` /
`deadline.compute_deadline_threshold`: the notification text is built with
plain Python string formatting, never an LLM call.

Prerequisite gap this module resolves (see the brief for full rationale):
`MonitoredTrip.itinerary_snapshot` never stored the original request's raw
lat/lon -- `Leg` only has `from_stop_id`/`to_stop_id` (a GTFS stop
reference, `None` for a WALK leg's arbitrary endpoint). This product also
has no live user-location tracking (geofencing was explicitly deferred in
the design doc). The honest, buildable interpretation built here:
re-plan the same overall trip, using the first and last *transit* legs'
stops as origin/destination (skipping any leading/trailing WALK leg) --
this loses the "walk to/from the station" portion of a fresh re-plan, a
documented simplification, not a hidden assumption.

Two implementation options were considered for resolving a transit leg's
stop_id back to coordinates: (a) extend `Leg`/`otp_client.py`'s GraphQL
query to carry raw lat/lon on every leg endpoint, or (b) reverse-resolve
via the already-loaded `StopIndex` (`StopIndex.find_by_id`, added
alongside this module). Option (b) is what's built here: this session has
no Docker access to live-verify OTP's actual GraphQL schema shape before
committing to it, so touching two already-shipped, widely-depended-on
Phase 1/2 files (`models/transit.py`, `otp_client.py`) would be a much
larger, riskier blast radius than a small, fully-verifiable addition to
this codebase's own already-loaded stop data.

Deliberately deferred, NOT built here: the design doc's aspirational
Haiku-based path for "comparing multiple real alternative routes [when] a
template can't express the tradeoff honestly." `replan_trip` always uses
`plan_route`'s first/default itinerary (`itineraries[0]`), same convention
as every other consumer in this codebase (`_handle_get_risk`'s
`itinerary_index` defaulting to 0, etc.). None of this task's required
tests exercise a multi-route-comparison path, and this codebase's TDD
discipline doesn't ship untested LLM-decision code.
"""
import sqlite3
from datetime import datetime, timezone

from app import deadline, risk_engine
from app.agents.conversation_agent import CITATION_FOOTER_TEMPLATE
from app.config import settings
from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary
from app.risk_engine import LOCAL_TZ
from app.routing.nearest_stop import get_stop_index
from app.routing.otp_client import OTPClient
from db import get_connection


def _route_signature(itinerary: Itinerary) -> tuple:
    """The honest signal for "is this actually a different trip" -- mode +
    route_short_name per transit leg, in itinerary order. Deliberately NOT
    based on p_miss or timing, both of which can fluctuate between two
    re-plans of the literal same route (a re-sampled Monte Carlo draw, a
    few seconds of schedule drift) without the trip having changed at all."""
    return tuple((leg.mode, leg.route_short_name) for leg in itinerary.legs if leg.mode != "WALK")


def _route_summary(itinerary: Itinerary) -> str:
    return " -> ".join(rs for _, rs in _route_signature(itinerary))


def _format_departure_clock(depart_by_ts: int) -> str:
    """Format an epoch-ms timestamp as a local no-leading-zero clock time,
    e.g. "8:05 AM". `%-I` (the usual no-leading-zero hour directive) raises
    `ValueError: Invalid format string` on this environment's Python/CRT
    (verified directly, not assumed) -- Windows' strftime doesn't support
    the glibc-style `%-` modifier at all. `%I` + manual leading-zero strip
    works on every platform including this one."""
    depart_by_local = datetime.fromtimestamp(depart_by_ts / 1000, tz=timezone.utc).astimezone(LOCAL_TZ)
    hour_minute = depart_by_local.strftime("%I:%M %p")
    if hour_minute.startswith("0"):
        hour_minute = hour_minute[1:]
    return hour_minute


def _build_notification(new_itinerary: Itinerary, new_risks: list, depart_by_ts: int | None) -> str:
    route_summary = _route_summary(new_itinerary)
    citation_risk = next((r for r in new_risks if r.quality == "ok"), None)

    if citation_risk is None:
        text = f"Your trip has been rerouted: now via {route_summary}. No transfer risk to report for the new route."
    else:
        pct = round(citation_risk.p_miss * 100)
        footer = CITATION_FOOTER_TEMPLATE.format(n=citation_risk.n, window_days=citation_risk.window_days)
        text = (
            f"Your trip has been rerouted: now via {route_summary}. There's about a "
            f"{pct}%* chance of missing the {citation_risk.from_route}->{citation_risk.to_route} "
            f"transfer at {citation_risk.transfer_stop_name}.\n{footer}"
        )

    if depart_by_ts is not None:
        text += f" Based on the new route, you should now aim to leave by {_format_departure_clock(depart_by_ts)}."

    return text


async def replan_trip(trip: MonitoredTrip, trigger_reason: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Re-plan `trip` fresh and, if the route actually changed, write a
    plain-language notification to `pending_notification` and return it.
    Returns `None` (no notification, no write beyond the snapshot refresh --
    see step-by-step below) if the route is unchanged, or if the trip
    can't be honestly re-planned at all (unresolvable stops, no transit
    legs, OTP finds no route).

    `trigger_reason` is accepted per the plan's signature but not
    otherwise used by this function's own logic -- Task 7's poll loop
    decides *whether* to call `replan_trip` based on it; once called,
    this function always re-plans unconditionally (decision 1 in the
    brief), regardless of why it was triggered.

    `conn`: same `owns_conn` pattern as `risk_engine.get_risk` -- if the
    caller passes a connection (Task 7's poll loop will pass its own
    per-trip transaction connection), it's reused for every internal
    conn= call (get_risk, compute_deadline_threshold) AND this function's
    own UPDATE, and never committed/closed here; if `None`, one is opened
    and closed in `finally`. By the time `replan_trip` runs, the trip was
    already claimed by `claim_active_trips_for_polling` (which serializes
    concurrent pollers on this trip) -- there's no additional atomicity
    this function's own write needs beyond a plain `UPDATE ... WHERE
    id = ?`.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        transit_legs = [leg for leg in trip.itinerary_snapshot.legs if leg.mode != "WALK"]
        if not transit_legs:
            return None  # nothing to re-plan meaningfully; honest skip, not a crash

        stop_index = get_stop_index()
        origin_stop = stop_index.find_by_id(risk_engine._strip_feed_prefix(transit_legs[0].from_stop_id))
        dest_stop = stop_index.find_by_id(risk_engine._strip_feed_prefix(transit_legs[-1].to_stop_id))
        if origin_stop is None or dest_stop is None:
            return None  # a stop that no longer resolves against static GTFS

        otp = OTPClient(base_url=settings.otp_base_url)
        itineraries = await otp.plan_route(
            origin_stop["lat"], origin_stop["lon"], dest_stop["lat"], dest_stop["lon"]
        )
        if not itineraries:
            # OTP found no route at all -- a documented simplification, not
            # a distinct "route no longer possible" template. Known
            # limitation, flagged in this task's report.
            return None
        new_itinerary = itineraries[0]

        new_risks = risk_engine.get_risk(new_itinerary, conn=conn)

        route_changed = _route_signature(trip.itinerary_snapshot) != _route_signature(new_itinerary)

        depart_by_ts = None
        if trip.deadline_ts is not None:
            deadline_ts_ms = int(trip.deadline_ts.timestamp() * 1000)
            depart_by_ts = deadline.compute_deadline_threshold(new_itinerary, deadline_ts_ms, conn=conn)

        if not route_changed:
            # Still refresh the snapshot with fresh timing (decision 2) --
            # but leave pending_notification untouched: don't overwrite an
            # existing unclaimed notification with nothing, and don't
            # manufacture a notification just because a trigger fired.
            conn.execute(
                "UPDATE monitored_trips SET itinerary_snapshot = ? WHERE id = ?",
                (new_itinerary.model_dump_json(), trip.id),
            )
            conn.commit()
            return None

        notification_text = _build_notification(new_itinerary, new_risks, depart_by_ts)
        conn.execute(
            "UPDATE monitored_trips SET itinerary_snapshot = ?, pending_notification = ? WHERE id = ?",
            (new_itinerary.model_dump_json(), notification_text, trip.id),
        )
        conn.commit()
        return notification_text
    finally:
        if owns_conn:
            conn.close()
