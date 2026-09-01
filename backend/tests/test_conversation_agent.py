# backend/tests/test_conversation_agent.py
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock
import pytest
import cost_guard
from app.agents.conversation_agent import ConversationAgent
from app.models.monitoring import MonitoredTrip
from app.models.transit import Itinerary, Leg
from app.models.risk import TransferRisk


def _fake_itinerary():
    return Itinerary(
        duration_seconds=1800,
        legs=[
            Leg(mode="SUBWAY", route_short_name="F", from_stop_name="Roosevelt Island",
                to_stop_name="Lex/63", from_stop_id="mtasbwy:R01", to_stop_id="mtasbwy:R02",
                start_time_ms=0, end_time_ms=1000),
        ],
    )


def _tool_use(name, id, input):
    # MagicMock(name=...) is a constructor-only special case (it sets the
    # mock's repr name, not a readable .name attribute) -- assign .name as
    # a plain attribute after construction so tool_use.name actually reads
    # back the tool name string, which conversation_agent.py's dispatch
    # now depends on.
    block = MagicMock(type="tool_use", id=id, input=input)
    block.name = name
    return block


def _fake_usage(input_tokens=100, output_tokens=20, cache_creation=0, cache_read=0):
    return MagicMock(model_dump=MagicMock(return_value={
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }))


@pytest.fixture(autouse=True)
def isolated_cost_log(tmp_path, monkeypatch):
    # Every ConversationAgent call now logs to cost_guard -- redirect its
    # log file so these tests never touch the real local spend log, and
    # reset the spend cap to a value nothing in this file's small fixture
    # usage would ever hit.
    monkeypatch.setattr(cost_guard, "LOG_PATH", tmp_path / "cost_log.ndjson")
    monkeypatch.setenv("ANTHROPIC_MONTHLY_SPEND_CAP_USD", "5")


@pytest.mark.asyncio
async def test_respond_calls_plan_route_tool_and_narrates_result():
    fake_tool_use_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    fake_final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="Take the F train — about 30 minutes.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = []
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[fake_tool_use_response, fake_final_response])

        agent = ConversationAgent()
        reply = await agent.respond("How do I get from Roosevelt Island to Lex/63?", conversation_history=[], anonymous_id="anon-1")

    assert reply == "Take the F train — about 30 minutes."
    mock_plan.assert_called_once()

    # system + tools are cached as one prefix: the breakpoint must sit on
    # the last (only) system block, not on the tool -- see the comment in
    # conversation_agent.py for why the ordering matters.
    call_kwargs = mock_client.messages.create.call_args_list[0].kwargs
    assert call_kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in call_kwargs["tools"][0]


@pytest.mark.asyncio
async def test_respond_propagates_cost_cap_exceeded():
    # If the local spend cap is already exhausted, the very next call's
    # cost_guard.log_call() must raise -- and that must propagate out of
    # respond() rather than being swallowed into a normal reply, since a
    # silently-caught cap breach would let the agent keep spending past
    # its budget.
    fake_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="irrelevant")],
        usage=_fake_usage(input_tokens=10_000_000),  # $10 of input alone, over the $5 cap
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock), \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(return_value=fake_response)

        agent = ConversationAgent()
        with pytest.raises(cost_guard.CostCapExceeded):
            await agent.respond("How do I get from Roosevelt Island to Lex/63?", conversation_history=[], anonymous_id="anon-1")


@pytest.mark.asyncio
async def test_respond_calls_get_risk_with_real_itinerary_from_plan_route():
    itinerary = _fake_itinerary()
    fake_transfer_risk = TransferRisk(
        from_route="F", to_route="Q", transfer_stop_name="Roosevelt Island",
        p_miss=0.12, n=500, window_days=14, quality="ok",
    )

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_2", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="12% chance of missing your transfer.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary]
        mock_get_risk.return_value = [fake_transfer_risk]
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, get_risk_response, final_response]
        )

        agent = ConversationAgent()
        reply = await agent.respond("How do I get from Roosevelt Island to Lex/63?", conversation_history=[], anonymous_id="anon-1")

    assert reply == "12% chance of missing your transfer."
    mock_get_risk.assert_called_once()
    # Must be the exact Itinerary object plan_route returned, not a
    # re-parsed copy and not anything the LLM supplied in its tool_use input.
    (called_itinerary,), _ = mock_get_risk.call_args
    assert called_itinerary is itinerary


@pytest.mark.asyncio
async def test_get_risk_itinerary_index_omitted_defaults_to_zero():
    itinerary_0 = _fake_itinerary()
    itinerary_1 = _fake_itinerary()

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_2", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="ok")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary_0, itinerary_1]
        mock_get_risk.return_value = []
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, get_risk_response, final_response]
        )

        agent = ConversationAgent()
        await agent.respond("plan a trip", conversation_history=[], anonymous_id="anon-1")

    (called_itinerary,), _ = mock_get_risk.call_args
    assert called_itinerary is itinerary_0


@pytest.mark.asyncio
async def test_get_risk_with_no_prior_plan_route_returns_honest_error():
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_1", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="I need to plan a route first.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock), \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[get_risk_response, final_response])

        agent = ConversationAgent()
        await agent.respond("what's the risk?", conversation_history=[], anonymous_id="anon-1")

    mock_get_risk.assert_not_called()
    tool_result_message = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    assert "no itinerary available" in tool_result_content


@pytest.mark.asyncio
async def test_get_risk_with_out_of_range_index_returns_honest_error():
    itinerary = _fake_itinerary()

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_2", {"itinerary_index": 5})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="That option doesn't exist.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary]
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, get_risk_response, final_response]
        )

        agent = ConversationAgent()
        await agent.respond("check option 5", conversation_history=[], anonymous_id="anon-1")

    mock_get_risk.assert_not_called()
    tool_result_message = mock_client.messages.create.call_args_list[2].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    assert "no itinerary available" in tool_result_content


@pytest.mark.asyncio
async def test_get_risk_tool_result_is_json_serialized_transfer_risk_list():
    itinerary = _fake_itinerary()
    fake_transfer_risk = TransferRisk(
        from_route="F", to_route="Q", transfer_stop_name="Roosevelt Island",
        p_miss=0.12, n=500, window_days=14, quality="ok",
    )

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_2", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="12% chance of missing your transfer.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary]
        mock_get_risk.return_value = [fake_transfer_risk]
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, get_risk_response, final_response]
        )

        agent = ConversationAgent()
        await agent.respond("plan and check risk", conversation_history=[], anonymous_id="anon-1")

    tool_result_message = mock_client.messages.create.call_args_list[2].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    import json
    assert json.loads(tool_result_content) == [fake_transfer_risk.model_dump()]


@pytest.mark.asyncio
async def test_respond_calls_find_stop_tool_with_llm_query_and_narrates_result():
    fake_stop_matches = [
        {"stop_id": "R01", "stop_name": "Roosevelt Island", "lat": 40.7597, "lon": -73.9532},
    ]

    find_stop_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("find_stop", "tool_1", {"query": "Roosevelt Island"})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="Roosevelt Island is at 40.7597, -73.9532.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock), \
         patch("app.agents.conversation_agent.get_stop_index") as mock_get_stop_index, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_get_stop_index.return_value.find_by_name.return_value = fake_stop_matches
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[find_stop_response, final_response])

        agent = ConversationAgent()
        reply = await agent.respond("Where is Roosevelt Island?", conversation_history=[], anonymous_id="anon-1")

    assert reply == "Roosevelt Island is at 40.7597, -73.9532."
    mock_get_stop_index.return_value.find_by_name.assert_called_once_with("Roosevelt Island")

    tool_result_message = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    import json
    assert json.loads(tool_result_content) == fake_stop_matches


@pytest.mark.asyncio
async def test_system_prompt_carries_citation_format_and_get_risk_data_flows_through():
    # This tests that the SYSTEM_PROMPT (as actually sent to messages.create)
    # carries the '%*' + footer citation format instructions, and that a
    # mocked 'quality: ok' get_risk result flows through respond() unmodified
    # -- it does NOT prove Haiku follows the format (that needs a live call,
    # see task-11-report.md), only that the prompt content and tool-result
    # plumbing are correct.
    itinerary = _fake_itinerary()
    fake_transfer_risk = TransferRisk(
        from_route="F", to_route="Q", transfer_stop_name="Roosevelt Island",
        p_miss=0.12, n=500, window_days=14, quality="ok",
    )

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_2", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(
            type="text",
            text="There's about a 12%* chance of missing that transfer.\n"
                 "*Based on 500 observed patterns in the last 14 days.*",
        )],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary]
        mock_get_risk.return_value = [fake_transfer_risk]
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, get_risk_response, final_response]
        )

        agent = ConversationAgent()
        reply = await agent.respond("plan and check risk", conversation_history=[], anonymous_id="anon-1")

    assert reply == (
        "There's about a 12%* chance of missing that transfer.\n"
        "*Based on 500 observed patterns in the last 14 days.*"
    )

    # Every messages.create call carries the same cached system prompt, and
    # it contains the citation-format rule and its trailing-'*' example --
    # a regression here (e.g. reverting to Task 7's prompt) would silently
    # drop the format instruction Haiku is supposed to follow.
    for call in mock_client.messages.create.call_args_list:
        system_text = call.kwargs["system"][-1]["text"]
        assert "%*" in system_text
        assert "Based on {n} observed patterns in the last {window_days} days." in system_text


@pytest.mark.asyncio
async def test_find_stop_zero_matches_returns_honest_empty_list():
    find_stop_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("find_stop", "tool_1", {"query": "Nonexistent Place"})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="I couldn't find that stop.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock), \
         patch("app.agents.conversation_agent.get_stop_index") as mock_get_stop_index, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_get_stop_index.return_value.find_by_name.return_value = []
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[find_stop_response, final_response])

        agent = ConversationAgent()
        await agent.respond("Where is Nonexistent Place?", conversation_history=[], anonymous_id="anon-1")

    tool_result_message = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    import json
    assert json.loads(tool_result_content) == []


# --- create_monitored_trip / cancel_monitored_trip dispatch -----------------
#
# Note on coverage honesty (per task-5-brief.md): the first test below
# confirms the create_monitored_trip tool is *available* on every call but
# not forced -- it cannot prove "the LLM chooses not to offer monitoring,"
# since that judgment lives in SYSTEM_PROMPT wording, not in dispatch code a
# mocked-response unit test can exercise. These tests only verify the
# dispatch plumbing: when a scripted response *does* call one of these
# tools, the right module function gets called with the right arguments and
# its result is relayed back to the LLM unmodified.


@pytest.mark.asyncio
async def test_zero_transfer_trip_with_no_deadline_never_calls_create_monitored_trip():
    itinerary = _fake_itinerary()

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_2", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="No transfer needed for this trip.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.monitoring.create_monitored_trip") as mock_create, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary]
        mock_get_risk.return_value = []  # empty list -- no transfer to discuss
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, get_risk_response, final_response]
        )

        agent = ConversationAgent()
        reply = await agent.respond("How do I get from Roosevelt Island to Lex/63?",
                                     conversation_history=[], anonymous_id="anon-1")

    assert reply == "No transfer needed for this trip."
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_create_monitored_trip_called_with_exact_itinerary_from_plan_route():
    itinerary = _fake_itinerary()
    fake_transfer_risk = TransferRisk(
        from_route="F", to_route="Q", transfer_stop_name="Roosevelt Island",
        p_miss=0.12, n=500, window_days=14, quality="ok",
    )

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    get_risk_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("get_risk", "tool_2", {})],
        usage=_fake_usage(),
    )
    create_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("create_monitored_trip", "tool_3", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="I'm monitoring your trip now.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.monitoring.create_monitored_trip") as mock_create, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary]
        mock_get_risk.return_value = [fake_transfer_risk]
        mock_create.return_value = 42
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, get_risk_response, create_response, final_response]
        )

        agent = ConversationAgent()
        reply = await agent.respond("Monitor this trip please.",
                                     conversation_history=[], anonymous_id="anon-1")

    assert reply == "I'm monitoring your trip now."
    mock_create.assert_called_once()
    # Must be the exact Itinerary object plan_route returned, not a
    # re-parsed copy and not anything the LLM supplied in its tool_use input
    # (same identity-assertion pattern as
    # test_respond_calls_get_risk_with_real_itinerary_from_plan_route).
    (called_itinerary, called_anonymous_id, called_deadline_ts), _ = mock_create.call_args
    assert called_itinerary is itinerary
    assert called_anonymous_id == "anon-1"
    assert called_deadline_ts is None


@pytest.mark.asyncio
async def test_create_monitored_trip_with_deadline_dispatches_without_prior_get_risk():
    itinerary = _fake_itinerary()
    deadline_ts = 1_700_010_000_000

    plan_route_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("plan_route", "tool_1", {"from_lat": 40.7597, "from_lon": -73.9532,
                                   "to_lat": 40.7644, "to_lon": -73.9656})],
        usage=_fake_usage(),
    )
    create_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("create_monitored_trip", "tool_2", {"deadline_ts": deadline_ts})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="Monitoring your trip for that deadline.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock) as mock_plan, \
         patch("app.agents.conversation_agent.risk_engine.get_risk") as mock_get_risk, \
         patch("app.agents.conversation_agent.monitoring.create_monitored_trip") as mock_create, \
         patch("app.agents.conversation_agent.deadline.compute_deadline_threshold") as mock_deadline, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_plan.return_value = [itinerary]
        mock_create.return_value = 7
        mock_deadline.return_value = 1_700_005_000_000
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(
            side_effect=[plan_route_response, create_response, final_response]
        )

        agent = ConversationAgent()
        reply = await agent.respond("I need to be there by a deadline, monitor it.",
                                     conversation_history=[], anonymous_id="anon-1")

    assert reply == "Monitoring your trip for that deadline."
    mock_get_risk.assert_not_called()  # confirms the deadline-alone path doesn't require a risk check first
    mock_create.assert_called_once_with(itinerary, "anon-1", deadline_ts)
    mock_deadline.assert_called_once_with(itinerary, deadline_ts)

    tool_result_message = mock_client.messages.create.call_args_list[2].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    import json
    assert json.loads(tool_result_content) == {"trip_id": 7, "depart_by_ts": 1_700_005_000_000}


@pytest.mark.asyncio
async def test_cancel_monitored_trip_with_no_trip_id_and_multiple_active_trips_is_ambiguous():
    itinerary = _fake_itinerary()
    trip_a = MonitoredTrip(
        id=1, anonymous_id="anon-1", itinerary_snapshot=itinerary, status="active",
        created_at=datetime(2026, 8, 30, 8, 0, 0, tzinfo=timezone.utc),
        ttl_expires_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=timezone.utc),
    )
    trip_b = MonitoredTrip(
        id=2, anonymous_id="anon-1", itinerary_snapshot=itinerary, status="active",
        created_at=datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc),
        ttl_expires_at=datetime(2026, 8, 30, 11, 0, 0, tzinfo=timezone.utc),
    )

    cancel_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("cancel_monitored_trip", "tool_1", {})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="Which trip do you mean?")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock), \
         patch("app.agents.conversation_agent.get_connection") as mock_get_connection, \
         patch("app.agents.conversation_agent.monitoring.list_active_trips") as mock_list_active, \
         patch("app.agents.conversation_agent.monitoring.cancel_monitored_trip") as mock_cancel, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_list_active.return_value = [trip_a, trip_b]
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[cancel_response, final_response])

        agent = ConversationAgent()
        await agent.respond("cancel my trip", conversation_history=[], anonymous_id="anon-1")

    mock_cancel.assert_not_called()
    tool_result_message = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    import json
    parsed = json.loads(tool_result_content)
    assert parsed["error"] == "ambiguous"
    assert {t["trip_id"] for t in parsed["active_trips"]} == {1, 2}
    # Real, server-computed summaries only -- never LLM-invented text.
    assert all("summary" in t for t in parsed["active_trips"])


@pytest.mark.asyncio
async def test_cancel_monitored_trip_mismatched_ownership_relays_false_not_success():
    cancel_response = MagicMock(
        stop_reason="tool_use",
        content=[_tool_use("cancel_monitored_trip", "tool_1", {"trip_id": 99})],
        usage=_fake_usage(),
    )
    final_response = MagicMock(
        stop_reason="end_turn",
        content=[MagicMock(type="text", text="I couldn't cancel that trip.")],
        usage=_fake_usage(),
    )

    with patch("app.agents.conversation_agent.OTPClient.plan_route", new_callable=AsyncMock), \
         patch("app.agents.conversation_agent.monitoring.cancel_monitored_trip") as mock_cancel, \
         patch("app.agents.conversation_agent.AsyncAnthropic") as mock_anthropic_cls:
        mock_cancel.return_value = False
        mock_client = mock_anthropic_cls.return_value
        mock_client.messages.create = AsyncMock(side_effect=[cancel_response, final_response])

        agent = ConversationAgent()
        await agent.respond("cancel trip 99", conversation_history=[], anonymous_id="anon-1")

    mock_cancel.assert_called_once_with(99, "anon-1")
    tool_result_message = mock_client.messages.create.call_args_list[1].kwargs["messages"][-1]
    tool_result_content = tool_result_message["content"][0]["content"]
    import json
    assert json.loads(tool_result_content) == {"cancelled": False, "trip_id": 99}
