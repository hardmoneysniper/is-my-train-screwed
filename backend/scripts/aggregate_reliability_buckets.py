"""backend/scripts/aggregate_reliability_buckets.py

Nightly fold of `arrival_events` into `reliability_buckets` (spec §5.2),
with exponential decay across days: `hist = 0.95*hist + 0.05*today`, `n`
decayed the same way. See task-5-brief.md for the binding design
decisions this implements -- in short:

- "Yesterday" means every `(agency, service_date)` in `arrival_events`
  not yet in the new `processed_days` bookkeeping table (handles both the
  large initial backfill and steady-state one-day-per-run identically).
- Headway is derived by sorting events per `(route_id, stop_id,
  direction)` and diffing consecutive `observed_arrival_ts`, keyed by the
  LATER event's own `(day_type, hour_bucket)`.
- Delay is a direct per-event value (bus events always have
  `delay_seconds = NULL`, so bus days produce zero `delay` buckets --
  expected, not a bug).
- Prediction-error excludes ambiguous-derivation-quality observations
  from the histogram/`n_observations` entirely, but still counts them in
  `n_ambiguous` for provenance (spec-sanctioned choice, see the brief).
- A bucket with no prior row is inserted as-is (no decay against an empty
  prior). An existing bucket is decayed element-wise across its full
  histogram array, `window_start` left untouched (it's a fixed anchor,
  not a moving window).
- A whole day's bucket upserts + its `processed_days` insert happen in
  one transaction, so a mid-fold crash never half-marks a day as done.

Runs as a Railway cron service (Task 10 wires up the schedule) -- this
file is just the script.
"""
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

from db import get_connection

DECAY_OLD = 0.95
DECAY_NEW = 0.05

# stat_type -> fixed-width histogram shape (task-5-brief.md step 5). The
# last bin index doubles as the clip bucket for values >= the top edge;
# for delay/prediction_error, bin 0 doubles as the clip bucket for values
# below the bottom edge (headway values are never negative, so it has no
# analogous low-side clip).
HIST_CONFIG = {
    "headway": {"bin_width_s": 30, "min_s": 0, "n_bins": 81},
    "delay": {"bin_width_s": 30, "min_s": -600, "n_bins": 101},
    "prediction_error": {"bin_width_s": 30, "min_s": -600, "n_bins": 101},
}

# route_id, stop_id, direction, day_type, hour_bucket
BucketKey = tuple[str, str, str, str, int]


def _bin_index(value_s: float, cfg: dict) -> int:
    idx = int((value_s - cfg["min_s"]) // cfg["bin_width_s"])
    return max(0, min(idx, cfg["n_bins"] - 1))


def _empty_entry(stat_type: str) -> dict:
    cfg = HIST_CONFIG[stat_type]
    return {"counts": [0.0] * cfg["n_bins"], "n_observations": 0, "n_ambiguous": 0}


def find_unprocessed_days(conn) -> list[tuple[str, date]]:
    """Every (agency, service_date) present in arrival_events that isn't
    yet in processed_days, oldest first per agency. Cross-agency order
    doesn't matter -- each bucket key includes agency, so subway's and
    bus's decay chains are fully independent (task-5-brief.md)."""
    rows = conn.execute(
        """
        SELECT DISTINCT ae.agency, ae.service_date
        FROM arrival_events ae
        WHERE NOT EXISTS (
            SELECT 1 FROM processed_days pd
            WHERE pd.agency = ae.agency AND pd.service_date = ae.service_date
        )
        ORDER BY ae.agency, ae.service_date
        """
    ).fetchall()
    return [(row["agency"], date.fromisoformat(row["service_date"])) for row in rows]


def _fetch_events(conn, agency: str, service_date: date) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM arrival_events WHERE agency = ? AND service_date = ?",
        (agency, service_date.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def accumulate_today(events: list[dict]) -> dict[str, dict[BucketKey, dict]]:
    """Fold one day's raw arrival_events rows into per-(stat_type, key)
    raw histogram entries (today's contribution only -- no decay here,
    that happens at upsert time against whatever's already stored)."""
    parsed = []
    for e in events:
        parsed.append(
            {
                **e,
                "observed_arrival_ts": datetime.fromisoformat(e["observed_arrival_ts"]),
                "predicted_arrival_ts_at_T_minus_5": (
                    datetime.fromisoformat(e["predicted_arrival_ts_at_T_minus_5"])
                    if e["predicted_arrival_ts_at_T_minus_5"]
                    else None
                ),
            }
        )

    today: dict[str, dict[BucketKey, dict]] = {"headway": {}, "delay": {}, "prediction_error": {}}

    # --- headway: group by (route_id, stop_id, direction), sort by time,
    # diff consecutive pairs, keyed by the LATER event's own day_type/hour_bucket.
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for e in parsed:
        groups[(e["route_id"], e["stop_id"], e["direction"])].append(e)
    for group_events in groups.values():
        group_events.sort(key=lambda e: e["observed_arrival_ts"])
        for prev, curr in zip(group_events, group_events[1:]):
            diff_s = (curr["observed_arrival_ts"] - prev["observed_arrival_ts"]).total_seconds()
            key = (curr["route_id"], curr["stop_id"], curr["direction"], curr["day_type"], curr["hour_bucket"])
            entry = today["headway"].setdefault(key, _empty_entry("headway"))
            entry["counts"][_bin_index(diff_s, HIST_CONFIG["headway"])] += 1.0
            entry["n_observations"] += 1
            if curr["derivation_quality"] == "ambiguous":
                entry["n_ambiguous"] += 1

    # --- delay: direct per-event value, no cross-event computation. Bus
    # events always have delay_seconds = NULL (Task 4's deliberate scope
    # decision), so this naturally produces zero buckets for agency='bus'.
    for e in parsed:
        if e["delay_seconds"] is None:
            continue
        key = (e["route_id"], e["stop_id"], e["direction"], e["day_type"], e["hour_bucket"])
        entry = today["delay"].setdefault(key, _empty_entry("delay"))
        entry["counts"][_bin_index(float(e["delay_seconds"]), HIST_CONFIG["delay"])] += 1.0
        entry["n_observations"] += 1
        if e["derivation_quality"] == "ambiguous":
            entry["n_ambiguous"] += 1

    # --- prediction_error: ambiguous-quality observations are EXCLUDED
    # from the histogram/n_observations (design choice, see module
    # docstring + task-5-brief.md step 4) but still counted in
    # n_ambiguous so that provenance reflects them.
    for e in parsed:
        if e["predicted_arrival_ts_at_T_minus_5"] is None:
            continue
        key = (e["route_id"], e["stop_id"], e["direction"], e["day_type"], e["hour_bucket"])
        entry = today["prediction_error"].setdefault(key, _empty_entry("prediction_error"))
        if e["derivation_quality"] == "ambiguous":
            entry["n_ambiguous"] += 1
            continue
        diff_s = (e["observed_arrival_ts"] - e["predicted_arrival_ts_at_T_minus_5"]).total_seconds()
        entry["counts"][_bin_index(diff_s, HIST_CONFIG["prediction_error"])] += 1.0
        entry["n_observations"] += 1

    return today


def _fetch_existing_bucket(conn, agency: str, key: BucketKey, stat_type: str):
    route_id, stop_id, direction, day_type, hour_bucket = key
    return conn.execute(
        """
        SELECT histogram, n_observations, n_ambiguous, window_start
        FROM reliability_buckets
        WHERE agency = ? AND route_id = ? AND stop_id = ? AND direction = ?
          AND day_type = ? AND hour_bucket = ? AND stat_type = ?
        """,
        (agency, route_id, stop_id, direction, day_type, hour_bucket, stat_type),
    ).fetchone()


def _upsert_bucket(
    conn,
    agency: str,
    key: BucketKey,
    stat_type: str,
    today_entry: dict,
    service_date: date,
    now: datetime,
) -> bool:
    """Insert or decay-merge one bucket. Returns True if this was a fresh
    insert (no prior row), False if an existing row was decayed."""
    route_id, stop_id, direction, day_type, hour_bucket = key
    cfg = HIST_CONFIG[stat_type]
    existing = _fetch_existing_bucket(conn, agency, key, stat_type)

    if existing is None:
        histogram = {
            "bin_width_s": cfg["bin_width_s"],
            "min_s": cfg["min_s"],
            "counts": list(today_entry["counts"]),
        }
        n_observations = float(today_entry["n_observations"])
        n_ambiguous = float(today_entry["n_ambiguous"])
        window_start = service_date.isoformat()
        created = True
    else:
        old_histogram = json.loads(existing["histogram"])
        new_counts = [
            DECAY_OLD * old_c + DECAY_NEW * new_c
            for old_c, new_c in zip(old_histogram["counts"], today_entry["counts"])
        ]
        histogram = {
            "bin_width_s": cfg["bin_width_s"],
            "min_s": cfg["min_s"],
            "counts": new_counts,
        }
        n_observations = DECAY_OLD * existing["n_observations"] + DECAY_NEW * today_entry["n_observations"]
        n_ambiguous = DECAY_OLD * existing["n_ambiguous"] + DECAY_NEW * today_entry["n_ambiguous"]
        window_start = existing["window_start"]  # fixed anchor -- never moved
        created = False

    # Real upsert-by-key (Task 1 built the UNIQUE index this relies on).
    # The merge math above already accounts for decay vs. fresh-insert, so
    # DO UPDATE SET just writes the precomputed final values -- crucially
    # it does NOT touch window_start, leaving it at its original value.
    conn.execute(
        """
        INSERT INTO reliability_buckets (
            agency, route_id, stop_id, direction, day_type, hour_bucket,
            stat_type, histogram, n_observations, n_ambiguous, window_start,
            last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agency, route_id, stop_id, direction, day_type, hour_bucket, stat_type)
        DO UPDATE SET
            histogram = excluded.histogram,
            n_observations = excluded.n_observations,
            n_ambiguous = excluded.n_ambiguous,
            last_updated = excluded.last_updated
        """,
        (
            agency,
            route_id,
            stop_id,
            direction,
            day_type,
            hour_bucket,
            stat_type,
            json.dumps(histogram),
            n_observations,
            n_ambiguous,
            window_start,
            now.isoformat(),
        ),
    )
    return created


def process_day(conn, agency: str, service_date: date) -> dict:
    """Fold one (agency, service_date)'s events into reliability_buckets
    and mark it processed, all in one transaction -- a mid-fold crash
    must never leave the day half-marked done (task-5-brief.md)."""
    events = _fetch_events(conn, agency, service_date)
    today = accumulate_today(events)
    now = datetime.now(timezone.utc)

    buckets_created = 0
    buckets_updated = 0
    observations_folded = {"headway": 0, "delay": 0, "prediction_error": 0}

    try:
        for stat_type, keyed in today.items():
            for key, entry in keyed.items():
                created = _upsert_bucket(conn, agency, key, stat_type, entry, service_date, now)
                if created:
                    buckets_created += 1
                else:
                    buckets_updated += 1
                observations_folded[stat_type] += entry["n_observations"]

        conn.execute(
            "INSERT INTO processed_days (agency, service_date, processed_at) VALUES (?, ?, ?)",
            (agency, service_date.isoformat(), now.isoformat()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(
        f"[aggregate_reliability_buckets] {agency} {service_date} -> "
        f"buckets_created={buckets_created} buckets_updated={buckets_updated} "
        f"observations_folded={observations_folded}"
    )
    return {
        "buckets_created": buckets_created,
        "buckets_updated": buckets_updated,
        "observations_folded": observations_folded,
    }


def run_aggregate(conn) -> None:
    for agency, service_date in find_unprocessed_days(conn):
        process_day(conn, agency, service_date)


if __name__ == "__main__":
    conn = get_connection()
    try:
        run_aggregate(conn)
    except Exception as exc:
        print(f"[aggregate_reliability_buckets] fatal: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()
