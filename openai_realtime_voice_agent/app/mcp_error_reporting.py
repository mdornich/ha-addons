"""Make failed MCP tool calls legible, in the log and to the model.

Pipecat's `MCPClient._call_tool` reports every call that produced *any* text as
`Tool 'X' completed successfully`, whether or not the call actually worked. It
never inspects `CallToolResult.isError`, and it swallows the exception from
`session.call_tool`. Two failure shapes were seen live on 2026-08-29:

    Tool response chunk 0: Error calling tool: UndefinedError: 'SearchMedia
        object' has no attribute 'get'
    Tool 'play_music_library' completed successfully      <-- it did not

    Tool response chunk 0: {"success": true, "result": {"error": "No Sonos
        favorite matching 'Elton John Radio'. Available: ..."}}

The first is a hard tool failure that read as a success in the log, so every
music request failed for an hour with nothing in the log saying so. The second
is a *soft* failure: the transport succeeded, but the Home Assistant script
returned an error in its payload -- fine for the model, still worth seeing.

This module wraps each registered MCP handler so that:

* the outcome is classified and logged by us -- ❌ for a failure, with the
  reason, so grepping the log for a broken tool actually works;
* a hard failure reaches the model as an unambiguous `TOOL FAILED: ...` string
  instead of a raw Python traceback fragment, so it says the action failed
  rather than paraphrasing an exception.

Nothing here changes a successful call's payload: the model sees exactly the
same text it does today.
"""

import dataclasses
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Prefixes pipecat itself produces when the MCP call blew up or returned nothing.
_PIPECAT_ERROR_PREFIXES = (
    "Error calling tool:",
    "Error calling mcp tool",
    "Sorry, could not call the mcp tool",
)

OK = "ok"
TOOL_ERROR = "tool_error"  # hard failure: the call did not run
PAYLOAD_ERROR = "payload_error"  # call ran, the service reported a problem


def classify_result(response: Any) -> tuple[str, str | None]:
    """Classify one MCP tool response.

    Returns `(outcome, detail)` where outcome is OK / TOOL_ERROR /
    PAYLOAD_ERROR and detail is a short reason, or None when it worked.

    Pure: no logging, no I/O, so the tests can pin every shape.
    """
    if response is None:
        return TOOL_ERROR, "no response"
    if not isinstance(response, str):
        return OK, None

    text = response.strip()
    if not text:
        return TOOL_ERROR, "empty response"

    for prefix in _PIPECAT_ERROR_PREFIXES:
        if text.startswith(prefix):
            return TOOL_ERROR, text

    # Home Assistant's MCP server answers with JSON; a script that stopped on an
    # error puts it in `result.error` while `success` stays true.
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return OK, None
    if not isinstance(payload, dict):
        return OK, None
    if payload.get("success") is False:
        return TOOL_ERROR, str(payload.get("error") or payload)[:400]
    result = payload.get("result")
    if isinstance(result, dict) and result.get("error"):
        return PAYLOAD_ERROR, str(result["error"])[:400]
    return OK, None


def describe_for_model(function_name: str, detail: str | None) -> str:
    """The string a hard failure should hand the model instead of a traceback."""
    reason = (detail or "unknown error").strip()
    return (
        f"TOOL FAILED: {function_name} did not run. {reason} "
        "Tell the user the action failed; do not claim it worked."
    )


def wrap_handler(function_name: str, handler):
    """Return `handler` with outcome logging and hard-failure rewriting.

    The wrapper swaps in its own `result_callback`, inspects what the inner
    handler produced, and forwards it (rewritten only for a hard failure).
    """

    async def wrapped(params):
        async def intercept(result, *args, **kwargs):
            outcome, detail = classify_result(result)
            if outcome == TOOL_ERROR:
                logger.error(f"❌ Tool '{function_name}' FAILED: {detail}")
                result = describe_for_model(function_name, detail)
            elif outcome == PAYLOAD_ERROR:
                logger.warning(f"⚠️ Tool '{function_name}' reported: {detail}")
            else:
                logger.info(f"✅ Tool '{function_name}' ok")
            return await params.result_callback(result, *args, **kwargs)

        return await handler(dataclasses.replace(params, result_callback=intercept))

    return wrapped


def install(llm_service, tool_names) -> int:
    """Re-register every named MCP tool on `llm_service` through `wrap_handler`.

    Call AFTER `MCPClient.register_tools_schema`. Returns how many were wrapped.
    Missing names are skipped rather than raised: a tool trimmed by the
    allow-list must not take the session down.
    """
    wrapped = 0
    registry = getattr(llm_service, "_functions", {})
    for name in tool_names:
        item = registry.get(name)
        if item is None or getattr(item, "handler", None) is None:
            continue
        llm_service.register_function(name, wrap_handler(name, item.handler))
        wrapped += 1
    return wrapped
