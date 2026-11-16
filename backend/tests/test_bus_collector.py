"""Tests for collectors/bus_collector.py's run_forever(should_continue=...)
parameter, added 2026-10-05 for weekend-only collection (see app/main.py),
plus the 2026-11-16 per-route day-type gate (compute_effective_corridors/
_load_day_type_gate). Not a re-test of poll_once/the GTFS-RT decode logic
-- those already run live, confirmed via this project's extensive live
verification history (see CLAUDE.md); this only covers the stop-condition
and day-type-gate behavior."""
import json
from datetime import datetime, timezone
from unittest.mock import patch

from collectors.bus_collector import compute_effective_corridors, run_forever


def test_run_forever_stops_when_should_continue_turns_false(tmp_path):
    call_count = {"n": 0}

    def should_continue():
        call_count["n"] += 1
        return call_count["n"] <= 2  # true for cycles 1-2, false on cycle 3

    with patch("collectors.bus_collector._api_key", return_value="fake-key"), \
         patch("collectors.bus_collector.poll_once", return_value=[]), \
         patch("collectors.bus_collector.time.sleep"), \
         patch("collectors.bus_collector.DATA_DIR", tmp_path):
        run_forever(should_continue=should_continue)

    # Checked once per cycle: twice continuing, once stopping -- never
    # polled past the point should_continue turned false.
    assert call_count["n"] == 3


def test_run_forever_never_calls_should_continue_when_not_given(tmp_path):
    call_count = {"n": 0}

    def poll_once_then_raise_to_stop(key):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise KeyboardInterrupt()
        return []

    with patch("collectors.bus_collector._api_key", return_value="fake-key"), \
         patch("collectors.bus_collector.poll_once", side_effect=poll_once_then_raise_to_stop), \
         patch("collectors.bus_collector.time.sleep"), \
         patch("collectors.bus_collector.DATA_DIR", tmp_path):
        try:
            run_forever()
        except KeyboardInterrupt:
            pass

    # Default (should_continue=None) behaves exactly as before this
    # change -- an unconditional while True, never exits on its own.
    assert call_count["n"] == 2


_A_WEEKDAY = datetime(2026, 11, 16, tzinfo=timezone.utc)  # a real Monday
_A_WEEKEND = datetime(2026, 11, 14, tzinfo=timezone.utc)  # a real Saturday


def test_compute_effective_corridors_returns_all_when_gate_file_missing(tmp_path):
    with patch("collectors.bus_collector.CORRIDORS", ["M60+", "Q90"]), \
         patch("collectors.bus_collector.DAY_TYPE_GATE_PATH", tmp_path / "missing.json"):
        assert compute_effective_corridors(_A_WEEKDAY) == ["M60+", "Q90"]


def test_compute_effective_corridors_returns_all_when_gate_file_corrupt(tmp_path):
    gate_path = tmp_path / "gate.json"
    gate_path.write_text("not valid json")
    with patch("collectors.bus_collector.CORRIDORS", ["M60+", "Q90"]), \
         patch("collectors.bus_collector.DAY_TYPE_GATE_PATH", gate_path):
        assert compute_effective_corridors(_A_WEEKDAY) == ["M60+", "Q90"]


def test_compute_effective_corridors_excludes_a_route_on_a_day_type_it_does_not_need(tmp_path):
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps({"M60+": ["weekend"], "Q90": ["weekday", "weekend"]}))
    with patch("collectors.bus_collector.CORRIDORS", ["M60+", "Q90"]), \
         patch("collectors.bus_collector.DAY_TYPE_GATE_PATH", gate_path):
        # Weekday: M60+ only needs weekend, so it's gated off; Q90 needs both.
        assert compute_effective_corridors(_A_WEEKDAY) == ["Q90"]
        # Weekend: M60+ needs weekend, so it's back in.
        assert compute_effective_corridors(_A_WEEKEND) == ["M60+", "Q90"]


def test_compute_effective_corridors_excludes_a_route_with_an_empty_needed_list(tmp_path):
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps({"M60+": []}))
    with patch("collectors.bus_collector.CORRIDORS", ["M60+"]), \
         patch("collectors.bus_collector.DAY_TYPE_GATE_PATH", gate_path):
        assert compute_effective_corridors(_A_WEEKDAY) == []
        assert compute_effective_corridors(_A_WEEKEND) == []


def test_compute_effective_corridors_always_includes_a_route_absent_from_the_gate(tmp_path):
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps({"M60+": []}))  # Q90 never mentioned
    with patch("collectors.bus_collector.CORRIDORS", ["M60+", "Q90"]), \
         patch("collectors.bus_collector.DAY_TYPE_GATE_PATH", gate_path):
        assert compute_effective_corridors(_A_WEEKDAY) == ["Q90"]


def test_run_forever_skips_poll_once_entirely_when_no_corridor_is_needed(tmp_path):
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps({"M60+": []}))
    call_count = {"n": 0}

    def should_continue():
        call_count["n"] += 1
        return call_count["n"] <= 2

    with patch("collectors.bus_collector._api_key", return_value="fake-key"), \
         patch("collectors.bus_collector.CORRIDORS", ["M60+"]), \
         patch("collectors.bus_collector.DAY_TYPE_GATE_PATH", gate_path), \
         patch("collectors.bus_collector.poll_once") as mock_poll_once, \
         patch("collectors.bus_collector.time.sleep"), \
         patch("collectors.bus_collector.DATA_DIR", tmp_path):
        run_forever(should_continue=should_continue)

    mock_poll_once.assert_not_called()
    assert call_count["n"] == 3
