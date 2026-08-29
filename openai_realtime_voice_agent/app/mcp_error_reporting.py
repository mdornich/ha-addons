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

MAX_DETAIL = 400


def _trim(detail: Any) -> str:
    return str(detail).strip()[:MAX_DETAIL]


def _classify_payload(payload: dict) -> tuple[str, str | None]:
    """Classify a decoded JSON body from the Home Assistant MCP server."""
    # A script that stopped on an error puts it in `result.error` while
    # `success` stays true.
    result = payload.get("result")
    if isinstance(result, dict) and result.get("error"):
        return PAYLOAD_ERROR, _trim(result["error"])
    # `success: false` means the service ran and reported a failure -- a payload
    # error, not a hard failure. Telling the model the tool "did not run" would
    # invite a retry of a call that will fail identically.
    if payload.get("success") is False:
        return PAYLOAD_ERROR, _trim(payload.get("error") or payload)
    # Home Assistant's own intent responses. `response_type: "error"` is the
    # everyday failure ("no valid targets" when a light name doesn't match), and
    # an `action_done` with a non-empty `data.failed` is a partial failure.
    # Neither carries the word "error" at the top level, so both used to log as
    # a clean success -- the most common HA failure, invisible.
    if payload.get("response_type") == "error":
        data = payload.get("data")
        code = data.get("code") if isinstance(data, dict) else None
        speech = payload.get("speech")
        spoken = None
        if isinstance(speech, dict):
            plain = speech.get("plain")
            if isinstance(plain, dict):
                spoken = plain.get("speech")
        return PAYLOAD_ERROR, _trim(spoken or code or payload)
    data = payload.get("data")
    if isinstance(data, dict) and data.get("failed"):
        return PAYLOAD_ERROR, _trim(f"failed targets: {data['failed']}")
    return OK, None


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

    # Anywhere in the text, not just at the front: pipecat concatenates every
    # content chunk into one string (`response += content.text`), so a tool that
    # emits a good chunk 0 and an error chunk 1 would otherwise read as a clean
    # success -- exactly the blindness this module exists to remove.
    for prefix in _PIPECAT_ERROR_PREFIXES:
        index = text.find(prefix)
        if index != -1:
            return TOOL_ERROR, _trim(text[index:])

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return OK, None
    if not isinstance(payload, dict):
        return OK, None
    return _classify_payload(payload)


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
    tool_names = list(tool_names)
    registry = getattr(llm_service, "_functions", None)
    if registry is None:
        # pipecat renamed the private registry: without this the wrap silently
        # no-ops and the add-on reverts to logging failures as successes, with a
        # green checkmark. Say so loudly instead.
        logger.error(
            "❌ MCP error reporting DISABLED: pipecat's function registry "
            "(_functions) is missing -- failed tool calls will log as successes"
        )
        return 0
    for name in tool_names:
        item = registry.get(name)
        if item is None or getattr(item, "handler", None) is None:
            continue
        llm_service.register_function(name, wrap_handler(name, item.handler))
        wrapped += 1
    if tool_names and not wrapped:
        logger.error(
            f"❌ MCP error reporting DISABLED: none of the {len(tool_names)} MCP "
            "tools were found in pipecat's registry -- failed tool calls will "
            "log as successes"
        )
    return wrapped
