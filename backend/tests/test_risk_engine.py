import json
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import risk_engine
from app.models.transit import Itinerary, Leg
from app.route_index import RouteIndex
from app.risk_engine import get_risk
from db import get_connection

LOCAL_TZ = ZoneInfo("America/New_York")


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(str(tmp_path / "risk.sqlite3"))
    yield connection
    connection.close()


def _routes_zip(tmp_path, name: str, rows: list[tuple[str, str]]):
    """A minimal synthetic GTFS zip containing just routes.txt, so the
    route index doesn't depend on the real ~150MB static GTFS files
    (task-6-brief.md's Tests section)."""
    path = tmp_path / name
    lines = ["route_id,route_short_name,route_long_name,route_type"]
    for route_id, short_name in rows:
        lines.append(f"{route_id},{short_name},,1")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", "\n".join(lines))
    return path


@pytest.fixture
def route_index(tmp_path):
    subway_zip = _routes_zip(
        tmp_path,
        "subway.zip",
        [
            ("F", "F"),
            ("Q", "Q"),
            # The three real subway shuttles: a genuine 3-way ambiguity on
            # short_name "S" (task-6-brief.md Gap 2), not a typo.
            ("GS", "S"),
            ("FS", "S"),
            ("H", "S"),
        ],
    )
    bus_zip = _routes_zip(
        tmp_path,
        "bus.zip",
        [
            ("Q102", "Q102"),
            ("Q70+", "Q70-SBS"),
        ],
    )
    return RouteIndex.from_gtfs([subway_zip, bus_zip])


def _local_ms(y, mo, d, h, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=LOCAL_TZ).timestamp() * 1000)


def _insert_bucket(conn, **overrides):
    fields = dict(
        agency="subway",
        route_id="F",
        stop_id="127N",
        direction="0",
        day_type="weekday",
        hour_bucket=8,
        stat_type="prediction_error",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": [0.0] * 101}),
        n_observations=250,
        n_ambiguous=0,
        window_start="2026-07-01",
        last_updated=datetime(2026, 8, 1).isoformat(),
    )
    fields.update(overrides)
    conn.execute(
        """
        INSERT INTO reliability_buckets (
            agency, route_id, stop_id, direction, day_type, hour_bucket,
            stat_type, histogram, n_observations, n_ambiguous, window_start,
            last_updated
        ) VALUES (
            :agency, :route_id, :stop_id, :direction, :day_type, :hour_bucket,
            :stat_type, :histogram, :n_observations, :n_ambiguous, :window_start,
            :last_updated
        )
        """,
        fields,
    )
    conn.commit()


# --- zero-transfer itineraries -----------------------------------------


def test_single_leg_itinerary_returns_empty_list(conn, route_index):
    itinerary = Itinerary(
        duration_seconds=600,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:B08N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            )
        ],
    )
    assert get_risk(itinerary, conn=conn, route_index=route_index) == []


def test_all_walk_itinerary_returns_empty_list(conn, route_index):
    itinerary = Itinerary(
        duration_seconds=300,
        legs=[
            Leg(
                mode="WALK",
                from_stop_name="A",
                to_stop_name="B",
                start_time_ms=0,
                end_time_ms=300000,
            )
        ],
    )
    assert get_risk(itinerary, conn=conn, route_index=route_index) == []


# --- happy path: one transfer, both buckets sufficient ------------------


def test_one_transfer_with_sufficient_data_returns_ok_quality(conn, route_index):
    # Feed-prefix-stripped stop_ids ("MTA_NYCT_Subway:127N" -> "127N",
    # task-6-brief.md Gap 1) must match the bare stop_id buckets are keyed by.
    # Incoming leg's agency is subway -> stat_type is "delay", not
    # "prediction_error" (final whole-branch review Critical fix: subway
    # never populates prediction_error, only delay -- see
    # risk_engine._incoming_stat_type). This is the previously-impossible
    # scenario: before the fix, a subway-incoming transfer could never
    # reach quality="ok" no matter what data existed, because the lookup
    # always asked for "prediction_error".
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",
        histogram=json.dumps(
            {"bin_width_s": 30, "min_s": -600, "counts": [200.0 if i == 20 else 0.0 for i in range(101)]}
        ),
        n_observations=200,
    )
    _insert_bucket(
        conn,
        agency="subway",
        route_id="Q",
        stop_id="127S",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps(
            {"bin_width_s": 30, "min_s": 0, "counts": [200.0 if i == 4 else 0.0 for i in range(81)]}
        ),
        n_observations=200,
        window_start="2026-07-01",
    )

    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:127N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            # Walk leg between the two transit legs -- its duration is
            # already embedded in the buffer, so it must be skipped when
            # finding transfer points (not treated as its own transfer).
            Leg(
                mode="WALK",
                from_stop_name="Lexington Av/63 St",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 12, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="Q",
                from_stop_id="MTA_NYCT_Subway:127S",
                from_stop_name="Lexington Av/63 St",
                to_stop_id="MTA_NYCT_Subway:201N",
                to_stop_name="Union Sq",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 25, 0),
            ),
        ],
    )

    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    r = results[0]
    assert r.from_route == "F"
    assert r.to_route == "Q"
    assert r.transfer_stop_name == "Lexington Av/63 St"
    assert r.quality == "ok"
    assert r.p_miss is not None
    assert 0.0 <= r.p_miss <= 1.0
    assert r.n == 200
    assert r.window_days == 20  # window_start is far enough in the past to hit the 20-day cap
    # Outgoing headway histogram's one weighted bin has midpoint 135s
    # (min_s=0, bin_width=30, i=4 -> 0+30*4.5=135); well under the
    # weekday-hour-8 rush threshold (450s), so no reroute offer.
    assert r.typical_wait_seconds == 135.0
    assert r.should_offer_reroute is False


def test_subway_incoming_leg_with_only_prediction_error_bucket_stays_insufficient(conn, route_index):
    """Directionality check for the Critical fix: proves the lookup
    genuinely changed, not just that a "delay" bucket happens to also be
    picked up. If only a "prediction_error" bucket exists for the subway
    incoming leg (the pre-fix expectation, and the one real bus-incoming
    transfers still use), the transfer must stay "insufficient" -- a
    regression back to always reading "prediction_error" would make this
    test start passing "ok" with a fabricated-looking histogram."""
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="prediction_error",
        histogram=json.dumps(
            {"bin_width_s": 30, "min_s": -600, "counts": [500.0 if i == 20 else 0.0 for i in range(101)]}
        ),
        n_observations=500,
    )
    _insert_bucket(
        conn,
        agency="subway",
        route_id="Q",
        stop_id="127S",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps(
            {"bin_width_s": 30, "min_s": 0, "counts": [200.0 if i == 4 else 0.0 for i in range(81)]}
        ),
        n_observations=200,
    )

    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:127N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="Q",
                from_stop_id="MTA_NYCT_Subway:127S",
                from_stop_name="Lexington Av/63 St",
                to_stop_id="MTA_NYCT_Subway:201N",
                to_stop_name="Union Sq",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 25, 0),
            ),
        ],
    )

    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    r = results[0]
    assert r.quality == "insufficient"
    assert r.p_miss is None
    # 2026-10-12 redesign: the outgoing bucket (real, sufficient data) is
    # independently gated from the incoming one -- typical_wait_seconds
    # must still populate even though the miss percentage can't.
    assert r.typical_wait_seconds == 135.0
    assert r.should_offer_reroute is False


def test_bus_incoming_leg_with_only_delay_bucket_stays_insufficient(conn, route_index):
    """Confirms the Critical fix left bus behavior unchanged in the other
    direction: bus's incoming leg must still read "prediction_error", not
    "delay". Real bus data can never actually populate a "delay" bucket
    (Task 4 leaves delay_seconds NULL for every bus event), but this test
    plants one anyway to prove it is NOT picked up -- if it were, that
    would mean the agency check in `_incoming_stat_type` is backwards."""
    _insert_bucket(
        conn,
        agency="bus",
        route_id="Q102",
        stop_id="450154",
        direction="0",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",  # bus never actually has this -- must be ignored
        histogram=json.dumps(
            {"bin_width_s": 30, "min_s": -600, "counts": [500.0 if i == 20 else 0.0 for i in range(101)]}
        ),
        n_observations=500,
    )
    _insert_bucket(
        conn,
        agency="subway",
        route_id="Q",
        stop_id="127S",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps(
            {"bin_width_s": 30, "min_s": 0, "counts": [200.0 if i == 4 else 0.0 for i in range(81)]}
        ),
        n_observations=200,
    )

    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="BUS",
                route_short_name="Q102",
                from_stop_id="MTABC:400000",
                from_stop_name="Start",
                to_stop_id="MTABC:450154",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="Q",
                from_stop_id="MTA_NYCT_Subway:127S",
                from_stop_name="Lexington Av/63 St",
                to_stop_id="MTA_NYCT_Subway:201N",
                to_stop_name="Union Sq",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 25, 0),
            ),
        ],
    )

    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    r = results[0]
    assert r.quality == "insufficient"
    assert r.p_miss is None
    # Same independent-gating point as the subway test above, mirrored for
    # the bus-incoming case.
    assert r.typical_wait_seconds == 135.0
    assert r.should_offer_reroute is False


def test_incoming_stat_type_is_agency_dependent():
    """Direct unit coverage of the Critical fix's selection function."""
    assert risk_engine._incoming_stat_type("subway") == "delay"
    assert risk_engine._incoming_stat_type("bus") == "prediction_error"


# --- insufficient data: missing bucket, or n < 200 -----------------------


def test_missing_buckets_returns_insufficient(conn, route_index):
    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:127N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="Q",
                from_stop_id="MTA_NYCT_Subway:127S",
                from_stop_name="Lexington Av/63 St",
                to_stop_id="MTA_NYCT_Subway:201N",
                to_stop_name="Union Sq",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 25, 0),
            ),
        ],
    )
    # No buckets inserted at all -> both incoming and outgoing are missing.
    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    r = results[0]
    assert r.quality == "insufficient"
    assert r.p_miss is None
    assert r.n == 0
    assert r.window_days == 0
    assert r.typical_wait_seconds is None
    assert r.should_offer_reroute is False


def test_low_n_bucket_returns_insufficient(conn, route_index):
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",  # subway incoming leg reads "delay", not "prediction_error"
        n_observations=10,  # below the n=50 threshold
    )
    # n=300 clears the threshold, but an all-zero histogram is just as "no
    # real signal" -- must still degrade typical_wait_seconds to None, not
    # divide by zero in _typical_wait_seconds.
    _insert_bucket(
        conn,
        agency="subway",
        route_id="Q",
        stop_id="127S",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [0.0] * 81}),
        n_observations=300,
    )

    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:127N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="Q",
                from_stop_id="MTA_NYCT_Subway:127S",
                from_stop_name="Lexington Av/63 St",
                to_stop_id="MTA_NYCT_Subway:201N",
                to_stop_name="Union Sq",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 25, 0),
            ),
        ],
    )
    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    r = results[0]
    assert r.quality == "insufficient"
    assert r.p_miss is None
    assert r.n == 10  # incoming bucket's own n_observations
    assert r.typical_wait_seconds is None  # outgoing bucket's histogram is all-zero despite n=300
    assert r.should_offer_reroute is False


def test_should_offer_reroute_true_when_typical_wait_exceeds_rush_threshold(conn, route_index):
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,  # a real RUSH_HOURS member
        stat_type="delay",
        histogram=json.dumps({"bin_width_s": 30, "min_s": -600, "counts": [200.0 if i == 20 else 0.0 for i in range(101)]}),
        n_observations=200,
    )
    # Headway bin i=20 (min_s=0, bin_width=30) -> midpoint 615s (10.25
    # min), well past RUSH_WAIT_THRESHOLD_SECONDS (7.5 min).
    _insert_bucket(
        conn,
        agency="subway",
        route_id="Q",
        stop_id="127S",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [200.0 if i == 20 else 0.0 for i in range(81)]}),
        n_observations=200,
    )

    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:127N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="Q",
                from_stop_id="MTA_NYCT_Subway:127S",
                from_stop_name="Lexington Av/63 St",
                to_stop_id="MTA_NYCT_Subway:201N",
                to_stop_name="Union Sq",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 25, 0),
            ),
        ],
    )

    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    r = results[0]
    assert r.typical_wait_seconds == 615.0
    assert r.should_offer_reroute is True


# --- ambiguous route_short_name (Gap 2) ----------------------------------


def test_ambiguous_route_short_name_is_insufficient(conn, route_index):
    # "S" matches all three real subway shuttles (GS/FS/H) -- unmatchable,
    # never a guess, even though a real bucket exists for one candidate.
    _insert_bucket(
        conn,
        agency="subway",
        route_id="GS",
        stop_id="A02N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="prediction_error",
        n_observations=500,
    )
    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="A03S",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [0.0] * 81}),
        n_observations=500,
    )

    itinerary = Itinerary(
        duration_seconds=900,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="S",
                from_stop_id="MTA_NYCT_Subway:A01N",
                from_stop_name="A",
                to_stop_id="MTA_NYCT_Subway:A02N",
                to_stop_name="B",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 5, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:A03S",
                from_stop_name="B",
                to_stop_id="MTA_NYCT_Subway:A04N",
                to_stop_name="C",
                start_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
            ),
        ],
    )
    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    assert results[0].quality == "insufficient"
    assert results[0].p_miss is None


# --- p_miss / wait-time, hand-computed bounds (2026-10-12 redesign) ------


def test_p_miss_from_incoming_zero_when_buffer_never_blown():
    # All incoming offsets are exactly 0 (bin 0 midpoint of min_s=-15,
    # bin_width=30 -> -15 + 15 = 0); buffer is positive -> never blown.
    incoming_histogram = {"bin_width_s": 30, "min_s": -15, "counts": [1000.0] + [0.0] * 9}
    p_miss = risk_engine._p_miss_from_incoming(incoming_histogram, buffer_seconds=60)
    assert p_miss == 0.0


def test_p_miss_from_incoming_one_when_incoming_always_exceeds_buffer():
    # Incoming offset always 2000s (a single bin, midpoint 1985+15=2000);
    # buffer=0 -> every real offset blows it -> p_miss is exactly 1.0,
    # with no headway/outgoing data involved at all (the redesign's whole
    # point: this is a pure function of the incoming distribution).
    incoming_histogram = {"bin_width_s": 30, "min_s": 1985, "counts": [1000.0]}
    p_miss = risk_engine._p_miss_from_incoming(incoming_histogram, buffer_seconds=0)
    assert p_miss == 1.0


def test_p_miss_from_incoming_is_exact_fraction_past_buffer():
    # Real HIST_CONFIG["delay"] shape (bin_width_s=30, min_s=-600, 101
    # bins). Bin i's midpoint is -600 + 30*(i+0.5). i=20 -> 15s (under a
    # 60s buffer, not a miss); i=100 (the clip bucket) -> 2415s (over the
    # buffer, a miss). 400 observations under, 600 over -> exact fraction
    # past the buffer, no Monte Carlo noise -- must be exactly 0.6.
    counts = [0.0] * 101
    counts[20] = 400.0
    counts[100] = 600.0
    incoming_histogram = {"bin_width_s": 30, "min_s": -600, "counts": counts}
    p_miss = risk_engine._p_miss_from_incoming(incoming_histogram, buffer_seconds=60)
    assert p_miss == 0.6


def test_typical_wait_seconds_is_weighted_mean_of_headway_histogram():
    # All weight on one bin (midpoint 135s, see min_s=0/bin_width=30/i=4)
    # -> typical wait is exactly that midpoint.
    outgoing_histogram = {"bin_width_s": 30, "min_s": 0, "counts": [0.0, 0.0, 0.0, 0.0, 200.0]}
    assert risk_engine._typical_wait_seconds(outgoing_histogram) == 135.0


def test_wait_threshold_seconds_rush_vs_nonrush():
    assert risk_engine._wait_threshold_seconds("weekday", 8) == risk_engine.RUSH_WAIT_THRESHOLD_SECONDS
    assert risk_engine._wait_threshold_seconds("weekday", 13) == risk_engine.NONRUSH_WAIT_THRESHOLD_SECONDS
    # Weekend never counts as rush, even at a rush-hour-shaped hour_bucket.
    assert risk_engine._wait_threshold_seconds("weekend", 8) == risk_engine.NONRUSH_WAIT_THRESHOLD_SECONDS


# --- Gap 3 (bus direction) fallback: direction-agnostic combination ------


def test_bus_bucket_lookup_combines_both_directions(conn):
    _insert_bucket(
        conn,
        agency="bus",
        route_id="Q102",
        stop_id="450154",
        direction="0",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [100.0] + [0.0] * 80}),
        n_observations=100,
        n_ambiguous=1,
        window_start="2026-07-01",
    )
    _insert_bucket(
        conn,
        agency="bus",
        route_id="Q102",
        stop_id="450154",
        direction="1",
        day_type="weekday",
        hour_bucket=8,
        stat_type="headway",
        histogram=json.dumps({"bin_width_s": 30, "min_s": 0, "counts": [150.0] + [0.0] * 80}),
        n_observations=150,
        n_ambiguous=2,
        window_start="2026-07-10",
    )

    combined = risk_engine._fetch_bus_bucket(conn, "Q102", "450154", "weekday", 8, "headway")

    assert combined["n_observations"] == 250
    assert combined["n_ambiguous"] == 3
    assert combined["window_start"] == "2026-07-10"  # the more recently-started of the two (conservative)
    histogram = json.loads(combined["histogram"])
    assert histogram["counts"][0] == 250.0


# --- multi-transfer itinerary ---------------------------------------------


def test_multi_transfer_itinerary_returns_one_result_per_transfer_in_order(conn, route_index):
    itinerary = Itinerary(
        duration_seconds=2700,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="X:1",
                from_stop_name="Roosevelt Island",
                to_stop_id="X:2",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="SUBWAY",
                route_short_name="Q",
                from_stop_id="X:2",
                from_stop_name="Lexington Av/63 St",
                to_stop_id="X:3",
                to_stop_name="Union Sq",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 25, 0),
            ),
            Leg(
                mode="BUS",
                route_short_name="Q102",
                from_stop_id="X:3",
                from_stop_name="Union Sq",
                to_stop_id="X:4",
                to_stop_name="Destination",
                start_time_ms=_local_ms(2026, 8, 24, 8, 30, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 45, 0),
            ),
        ],
    )
    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 2
    assert results[0].from_route == "F"
    assert results[0].to_route == "Q"
    assert results[0].transfer_stop_name == "Lexington Av/63 St"
    assert results[1].from_route == "Q"
    assert results[1].to_route == "Q102"
    assert results[1].transfer_stop_name == "Union Sq"


# --- Gap 1 unit coverage ----------------------------------------------------


def test_strip_feed_prefix_removes_feed_id():
    assert risk_engine._strip_feed_prefix("MTA_NYCT_Subway:127N") == "127N"


def test_strip_feed_prefix_leaves_bare_id_unchanged():
    assert risk_engine._strip_feed_prefix("127N") == "127N"


def test_ferry_transfer_degrades_to_insufficient_not_crash(conn, tmp_path):
    ferry_zip = _routes_zip(tmp_path, "ferry.zip", [("AS", "AS")])
    subway_zip = _routes_zip(tmp_path, "subway.zip", [("F", "F")])
    route_index_with_ferry = RouteIndex.from_gtfs([subway_zip, ferry_zip])

    itinerary = Itinerary(
        duration_seconds=1800,
        legs=[
            Leg(
                mode="SUBWAY",
                route_short_name="F",
                from_stop_id="MTA_NYCT_Subway:B06N",
                from_stop_name="Roosevelt Island",
                to_stop_id="MTA_NYCT_Subway:127N",
                to_stop_name="Lexington Av/63 St",
                start_time_ms=_local_ms(2026, 8, 24, 8, 0, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 10, 0),
            ),
            Leg(
                mode="FERRY",
                route_short_name="AS",
                from_stop_id="NYCFerry:4",
                from_stop_name="Hunters Point South",
                to_stop_id="NYCFerry:17",
                to_stop_name="East 34th Street",
                start_time_ms=_local_ms(2026, 8, 24, 8, 15, 0),
                end_time_ms=_local_ms(2026, 8, 24, 8, 30, 0),
            ),
        ],
    )
    # No reliability_buckets rows for agency='ferry' exist -- none ever
    # will, in this scope (no ferry collector). This must degrade
    # honestly, not crash and not fabricate a number.
    results = get_risk(itinerary, conn=conn, route_index=route_index_with_ferry)

    assert len(results) == 1
    assert results[0].quality == "insufficient"
    assert results[0].p_miss is None
