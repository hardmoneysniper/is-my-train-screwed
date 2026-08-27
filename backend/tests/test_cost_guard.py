# backend/tests/test_cost_guard.py
import pytest
import cost_guard


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cost_guard, "LOG_PATH", tmp_path / "cost_log.ndjson")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_SPEND_CAP_USD", "5")


def test_cost_usd_accounts_for_input_and_output_tokens():
    cost = cost_guard._cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert cost == pytest.approx(
        cost_guard.PRICE_PER_MTOK_INPUT_USD + cost_guard.PRICE_PER_MTOK_OUTPUT_USD
    )


def test_cost_usd_accounts_for_cache_write_and_read_tokens():
    # Cache tokens are billed separately from (not folded into) input_tokens
    # -- a call with cache_creation/cache_read but zero plain input_tokens
    # must still cost something, at the cache-specific rates.
    cost = cost_guard._cost_usd({
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    })
    assert cost == pytest.approx(
        cost_guard.PRICE_PER_MTOK_CACHE_WRITE_USD + cost_guard.PRICE_PER_MTOK_CACHE_READ_USD
    )


def test_cost_usd_ignores_missing_cache_fields():
    # Callers that never use caching (e.g. a plain single-shot call) won't
    # have these keys in usage at all -- must not raise or count as cost.
    cost = cost_guard._cost_usd({"input_tokens": 1_000_000, "output_tokens": 0})
    assert cost == pytest.approx(cost_guard.PRICE_PER_MTOK_INPUT_USD)


def test_log_call_raises_once_month_to_date_spend_reaches_cap():
    small_usage = {"input_tokens": 100, "output_tokens": 0}  # a fraction of a cent
    cost_guard.log_call(small_usage, agent="test")  # under cap: does not raise

    huge_usage = {"input_tokens": 10_000_000, "output_tokens": 0}  # $10, over the $5 cap
    with pytest.raises(cost_guard.CostCapExceeded):
        cost_guard.log_call(huge_usage, agent="test")


def test_log_call_returns_this_calls_cost_not_cumulative():
    cost = cost_guard.log_call({"input_tokens": 500_000, "output_tokens": 0}, agent="test")
    assert cost == pytest.approx(0.5 * cost_guard.PRICE_PER_MTOK_INPUT_USD)
