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
    assert results[0].quality == "insufficient"
    assert results[0].p_miss is None


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
    assert results[0].quality == "insufficient"
    assert results[0].p_miss is None


def test_incoming_stat_type_is_agency_dependent():
    """Direct unit coverage of the Critical fix's selection function."""
    assert risk_engine._incoming_stat_type("subway") == "delay"
    assert risk_engine._incoming_stat_type("bus") == "prediction_error"


# --- insufficient data: missing bucket, or n < 200 -----------------------


def test_missing_buckets_returns_insufficient_and_skips_monte_carlo(conn, route_index, monkeypatch):
    calls = []
    monkeypatch.setattr(
        risk_engine, "_monte_carlo_p_miss", lambda *a, **k: calls.append(1) or 0.5
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
    # No buckets inserted at all -> both incoming and outgoing are missing.
    results = get_risk(itinerary, conn=conn, route_index=route_index)

    assert len(results) == 1
    r = results[0]
    assert r.quality == "insufficient"
    assert r.p_miss is None
    assert r.n == 0
    assert r.window_days == 0
    assert calls == []  # Monte Carlo genuinely never ran


def test_low_n_bucket_returns_insufficient_and_skips_monte_carlo(conn, route_index, monkeypatch):
    calls = []
    monkeypatch.setattr(
        risk_engine, "_monte_carlo_p_miss", lambda *a, **k: calls.append(1) or 0.5
    )

    _insert_bucket(
        conn,
        agency="subway",
        route_id="F",
        stop_id="127N",
        day_type="weekday",
        hour_bucket=8,
        stat_type="delay",  # subway incoming leg reads "delay", not "prediction_error"
        n_observations=50,  # below the n=200 threshold
    )
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
    assert r.n == 50  # min(50, 300)
    assert calls == []


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


# --- Monte Carlo miss condition, hand-computed bounds --------------------


def test_monte_carlo_p_miss_zero_when_buffer_never_blown():
    # All incoming offsets are exactly 0 (bin 0 midpoint of min_s=-15,
    # bin_width=30 -> -15 + 15 = 0); buffer is positive -> never blown.
    incoming_histogram = {"bin_width_s": 30, "min_s": -15, "counts": [1000.0] + [0.0] * 9}
    outgoing_histogram = {"bin_width_s": 30, "min_s": 0, "counts": [1000.0] + [0.0] * 9}
    p_miss = risk_engine._monte_carlo_p_miss(incoming_histogram, outgoing_histogram, buffer_seconds=60, draws=200)
    assert p_miss == 0.0


def test_monte_carlo_p_miss_one_when_incoming_far_exceeds_buffer_and_headway():
    # Incoming offset always 2000s (a single bin); buffer=0 and headway
    # always 15s -- the overrun (2000s) vastly exceeds the headway, so
    # every draw is a genuine miss.
    incoming_histogram = {"bin_width_s": 30, "min_s": 1985, "counts": [1000.0]}
    outgoing_histogram = {"bin_width_s": 30, "min_s": 0, "counts": [1000.0]}
    p_miss = risk_engine._monte_carlo_p_miss(incoming_histogram, outgoing_histogram, buffer_seconds=0, draws=200)
    assert p_miss == 1.0


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
