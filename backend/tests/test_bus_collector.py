"""Tests for collectors/bus_collector.py's run_forever(should_continue=...)
parameter, added 2026-10-05 for weekend-only collection (see app/main.py).
Not a re-test of poll_once/the GTFS-RT decode logic -- those already run
live, confirmed via this project's extensive live verification history
(see CLAUDE.md); this only covers the new stop-condition behavior."""
from unittest.mock import patch

from collectors.bus_collector import run_forever


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
