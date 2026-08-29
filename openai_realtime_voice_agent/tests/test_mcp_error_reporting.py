"""Tests for MCP tool-failure reporting.

Every string in the classification tests below is verbatim from the add-on log
on 2026-08-29, where pipecat logged a hard tool failure as "completed
successfully" and every music request silently failed.

Run: python3 -m pytest addon/openai_realtime_voice_agent/tests -q
"""

import asyncio
import dataclasses
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mcp_error_reporting import (  # noqa: E402
    OK,
    PAYLOAD_ERROR,
    TOOL_ERROR,
    classify_result,
    describe_for_model,
    install,
    wrap_handler,
)

# The failure that hid for an hour: pipecat called this "completed successfully".
HARD_FAILURE = (
    "Error calling tool: UndefinedError: "
    "'homeassistant.components.media_player.browse_media.SearchMedia object' "
    "has no attribute 'get'"
)
# A script that ran fine and reported a miss. The model handled this one well.
SOFT_FAILURE = (
    '{"success": true, "result": {"error": "No Sonos favorite matching '
    "'Elton John Radio'. Available: 2cellos Radio (My Station); Acoustic "
    'Christmas"}}'
)
SUCCESS = '{"success": true, "result": {"played": "Amos Lee Radio", "player": "media_player.media_room_2"}}'
ACTION_DONE = '{"speech": {}, "response_type": "action_done", "data": {"success": [{"name": "brewery"}], "failed": []}}'


def test_hard_failure_is_a_tool_error():
    outcome, detail = classify_result(HARD_FAILURE)
    assert outcome == TOOL_ERROR
    assert "SearchMedia" in detail


def test_pipecat_fallback_string_is_a_tool_error():
    assert classify_result("Sorry, could not call the mcp tool")[0] == TOOL_ERROR
    assert classify_result("Error calling mcp tool HassTurnOn: boom")[0] == TOOL_ERROR


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_response_is_a_tool_error(empty):
    assert classify_result(empty)[0] == TOOL_ERROR


def test_payload_error_is_reported_but_not_rewritten():
    outcome, detail = classify_result(SOFT_FAILURE)
    assert outcome == PAYLOAD_ERROR
    assert "No Sonos favorite" in detail


def test_explicit_success_false_is_a_tool_error():
    outcome, detail = classify_result('{"success": false, "error": "nope"}')
    assert outcome == TOOL_ERROR
    assert detail == "nope"


@pytest.mark.parametrize("good", [SUCCESS, ACTION_DONE, "Live Context: An overview"])
def test_successful_responses_classify_as_ok(good):
    assert classify_result(good) == (OK, None)


def test_a_json_list_response_is_not_mistaken_for_an_error():
    assert classify_result('["a", "b"]') == (OK, None)


@dataclasses.dataclass
class FakeParams:
    function_name: str
    tool_call_id: str
    arguments: dict
    llm: object
    context: object
    result_callback: object


def run_wrapped(response, name="play_music_library"):
    """Drive wrap_handler with an inner handler that produces `response`."""
    delivered = []

    async def inner(params):
        await params.result_callback(response)

    async def outer_callback(result, *a, **k):
        delivered.append(result)

    params = FakeParams(name, "call_1", {}, None, None, outer_callback)
    asyncio.run(wrap_handler(name, inner)(params))
    return delivered


def test_a_hard_failure_reaches_the_model_as_an_explicit_failure():
    (delivered,) = run_wrapped(HARD_FAILURE)
    assert delivered.startswith("TOOL FAILED: play_music_library did not run.")
    assert "do not claim it worked" in delivered


def test_a_successful_payload_is_forwarded_untouched():
    assert run_wrapped(SUCCESS) == [SUCCESS]


def test_a_payload_error_is_forwarded_untouched():
    """The model already handles these well -- only the log should change."""
    assert run_wrapped(SOFT_FAILURE) == [SOFT_FAILURE]


def test_a_hard_failure_is_logged_as_an_error(caplog):
    with caplog.at_level(logging.ERROR):
        run_wrapped(HARD_FAILURE)
    assert any(
        "❌ Tool 'play_music_library' FAILED" in r.message for r in caplog.records
    )
    assert not any(r.levelno == logging.INFO for r in caplog.records)


def test_a_success_is_not_logged_as_an_error(caplog):
    with caplog.at_level(logging.INFO):
        run_wrapped(SUCCESS)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_describe_for_model_survives_a_missing_detail():
    assert "unknown error" in describe_for_model("HassTurnOn", None)


class FakeItem:
    def __init__(self, handler):
        self.handler = handler


class FakeLLM:
    def __init__(self, names):
        async def handler(params):
            await params.result_callback(SUCCESS)

        self._functions = {n: FakeItem(handler) for n in names}

    def register_function(self, name, handler, **kwargs):
        self._functions[name] = FakeItem(handler)


def test_install_wraps_every_registered_tool():
    llm = FakeLLM(["HassTurnOn", "play_music_library"])
    before = dict(llm._functions)
    assert install(llm, ["HassTurnOn", "play_music_library"]) == 2
    for name in before:
        assert llm._functions[name].handler is not before[name].handler


def test_install_skips_names_that_were_never_registered():
    """A tool trimmed by the allow-list must not take the session down."""
    llm = FakeLLM(["HassTurnOn"])
    assert install(llm, ["HassTurnOn", "not_registered"]) == 1


def test_wrap_handler_works_on_pipecats_real_params_object():
    """The wrapper swaps result_callback via dataclasses.replace -- that only
    holds while pipecat's FunctionCallParams stays a dataclass. Pin it."""
    llm_service = pytest.importorskip("pipecat.services.llm_service")
    delivered = []

    async def inner(params):
        await params.result_callback(HARD_FAILURE)

    async def outer_callback(result, *a, **k):
        delivered.append(result)

    params = llm_service.FunctionCallParams(
        function_name="play_music_library",
        tool_call_id="call_1",
        arguments={},
        llm=None,
        context=None,
        result_callback=outer_callback,
    )
    asyncio.run(wrap_handler("play_music_library", inner)(params))
    assert delivered[0].startswith("TOOL FAILED:")
