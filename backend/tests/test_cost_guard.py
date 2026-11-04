# backend/tests/test_cost_guard.py
import pytest
import cost_guard


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cost_guard, "LOG_PATH", tmp_path / "cost_log.ndjson")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_SPEND_CAP_USD", "5")


HAIKU = cost_guard._HAIKU_MODEL


def test_cost_usd_accounts_for_input_and_output_tokens():
    cost = cost_guard._cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000}, HAIKU)
    rates = cost_guard.PRICING_PER_MTOK_USD[HAIKU]
    assert cost == pytest.approx(rates["input"] + rates["output"])


def test_cost_usd_accounts_for_cache_write_and_read_tokens():
    # Cache tokens are billed separately from (not folded into) input_tokens
    # -- a call with cache_creation/cache_read but zero plain input_tokens
    # must still cost something, at the cache-specific rates.
    cost = cost_guard._cost_usd({
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }, HAIKU)
    rates = cost_guard.PRICING_PER_MTOK_USD[HAIKU]
    assert cost == pytest.approx(rates["cache_write"] + rates["cache_read"])


def test_cost_usd_ignores_missing_cache_fields():
    # Callers that never use caching (e.g. a plain single-shot call) won't
    # have these keys in usage at all -- must not raise or count as cost.
    cost = cost_guard._cost_usd({"input_tokens": 1_000_000, "output_tokens": 0}, HAIKU)
    assert cost == pytest.approx(cost_guard.PRICING_PER_MTOK_USD[HAIKU]["input"])


def test_cost_usd_applies_the_real_rate_for_a_non_haiku_model():
    # 2026-10-17 real bug fix: this used to apply Haiku's rate to every
    # call regardless of which model ran -- would have silently
    # undercounted a Sonnet call (2-3x cheaper under the old flat rate)
    # against the $5 hard cap the moment a non-Haiku caller showed up
    # (app/location_resolver.py, same session).
    cost = cost_guard._cost_usd({"input_tokens": 1_000_000, "output_tokens": 0}, "claude-sonnet-5")
    assert cost == pytest.approx(cost_guard.PRICING_PER_MTOK_USD["claude-sonnet-5"]["input"])
    assert cost != pytest.approx(cost_guard.PRICING_PER_MTOK_USD[HAIKU]["input"])


def test_cost_usd_falls_back_to_haiku_rate_for_an_unrecognized_model():
    cost = cost_guard._cost_usd({"input_tokens": 1_000_000, "output_tokens": 0}, "some-future-model")
    assert cost == pytest.approx(cost_guard.PRICING_PER_MTOK_USD[HAIKU]["input"])


def test_log_call_raises_once_month_to_date_spend_reaches_cap():
    small_usage = {"input_tokens": 100, "output_tokens": 0}  # a fraction of a cent
    cost_guard.log_call(small_usage, agent="test")  # under cap: does not raise

    huge_usage = {"input_tokens": 10_000_000, "output_tokens": 0}  # $10, over the $5 cap
    with pytest.raises(cost_guard.CostCapExceeded):
        cost_guard.log_call(huge_usage, agent="test")


def test_log_call_returns_this_calls_cost_not_cumulative():
    cost = cost_guard.log_call({"input_tokens": 500_000, "output_tokens": 0}, agent="test")
    assert cost == pytest.approx(0.5 * cost_guard.PRICING_PER_MTOK_USD[HAIKU]["input"])


def test_log_call_defaults_to_haiku_when_model_not_passed():
    cost = cost_guard.log_call({"input_tokens": 500_000, "output_tokens": 0}, agent="test")
    assert cost == pytest.approx(0.5 * cost_guard.PRICING_PER_MTOK_USD[HAIKU]["input"])


def test_log_call_uses_the_passed_models_real_rate():
    cost = cost_guard.log_call({"input_tokens": 500_000, "output_tokens": 0}, agent="test", model="claude-sonnet-5")
    assert cost == pytest.approx(0.5 * cost_guard.PRICING_PER_MTOK_USD["claude-sonnet-5"]["input"])
