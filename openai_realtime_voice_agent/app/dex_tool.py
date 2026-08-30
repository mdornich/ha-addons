"""The `ask_dex` function-tool — Dex, reached verbatim, only when named.

Per issue #1143 as ratified: Trixie is the microphone, not the editor. The
user's transcript goes to Dex unaltered, Dex's reply comes back and is spoken
unaltered, and the tool is called ONLY when the user names Dex. Trixie must not
summarise, rephrase, or answer on Dex's behalf.

TRANSPORT — READ THIS BEFORE WIRING ANYTHING
--------------------------------------------
Dex runs on Hermes. The supported application adapter is
`agents/_runtime/hermes_invoke.py` in the 980labsOS repo
(`docs/architecture/agent-interaction-model.md` "Runtime roles"), and it is a
LOCAL SUBPROCESS adapter: it shells out to the `hermes` CLI through
`scripts/agent-fleet/run-with-workload-env.sh` on the gateway host (the Mac
mini). **It has no network surface at all.** There is no HTTP endpoint, no
socket, nothing this container can reach.

This add-on is a Docker container on the Home Assistant host. It cannot invoke a
subprocess on another machine, and inventing a transport (SSH, a bespoke MCP
server, a Hermes fork) is exactly the improvisation
`docs/architecture/agent-interaction-model.md` exists to prevent — see issue
#211, where three such mechanisms were proposed for a question canon had already
answered.

So this module ships the CLIENT half only, against a contract that does not yet
have a server:

    POST  <dex_adapter_url>
    Content-Type: application/json
    {"text": "<the user's verbatim transcript>",
     "source": "ha-voice-realtime",
     "conversation_id": "<optional, for follow-ups in one Realtime session>"}

    200 OK
    {"reply": "<Dex's verbatim reply text>"}
      -- `reply` is preferred; `text`, `response` and `message` are accepted as
         aliases so a minimal adapter needs no negotiation.
    any non-2xx, or a body with neither key -> spoken failure, nothing invented.

Until something serves that URL, leave `dex_adapter_url` blank: the tool is then
NOT registered with the model at all (see main.py), so it cannot be called and
cannot hallucinate a Dex answer. If it somehow is called with no URL configured
it returns an explicit "not wired" sentence rather than a plausible reply.

WHAT STILL HAS TO BE BUILT (out of scope for this brief, tracked on #1143):
a small HTTP front for `invoke_hermes_profile(profile="dex", message=...)` on
the gateway host, bound to the LAN, authenticated, and — because it is a new
synchronous callable surface — ratified the way the interaction model requires
before it carries anything action-bearing.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

# Dex is a full agent turn on Hermes: #1143 measured a ~6 s floor and it can run
# much longer. The persona says "give me a moment" before the call for exactly
# this reason. 30 s is the ceiling; past that the session must be given back to
# the user rather than left silent.
DEFAULT_TIMEOUT_S = 30.0

# Keys accepted for the reply text, in order of preference.
_REPLY_KEYS = ("reply", "text", "response", "message")

_MSG_EMPTY = "I didn't catch what you wanted to ask Dex."
_MSG_NOT_WIRED = "I can't reach Dex yet — the Dex adapter isn't set up on this system."
_MSG_TIMEOUT = "Dex is taking too long to answer. Try again in a moment."
_MSG_FAILED = "I couldn't reach Dex just now."
_MSG_EMPTY_REPLY = "Dex didn't send anything back."


def get_dex_tool_definition() -> Dict[str, Any]:
    """OpenAI Realtime function-tool definition for Dex."""
    return {
        "type": "function",
        "name": "ask_dex",
        "description": (
            "Relay a message to Dex, Mitch's chief of staff agent. Use this "
            "ONLY when the user explicitly names Dex — 'ask Dex...', 'tell "
            "Dex...', 'what does Dex say about...'. Never use it for anything "
            "else: not for the home (that is house), not for general facts "
            "(that is web_search), and never on your own initiative. Pass the "
            "user's words through EXACTLY as they said them, minus the words "
            "addressing Dex; do not rewrite, summarise, expand or interpret "
            "them. Speak Dex's reply back word for word — you are the "
            "microphone, not the editor. Dex takes several seconds, so tell "
            "the user you need a moment BEFORE you call this."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "The user's message for Dex, verbatim, with the "
                        "addressing words removed. 'Ask Dex what's on my "
                        "calendar tomorrow' -> \"what's on my calendar "
                        'tomorrow".'
                    ),
                }
            },
            "required": ["text"],
        },
    }


class DexToolError(RuntimeError):
    """Raised inside the tool for a condition worth a distinct spoken reply."""

    def __init__(self, spoken: str, detail: str) -> None:
        self.spoken = spoken
        self.detail = detail
        super().__init__(detail)


def _post_sync(url: str, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    """Blocking POST. Run via asyncio.to_thread so the voice loop keeps running.

    urllib rather than aiohttp/httpx on purpose: no new dependency in an image
    that already takes >2 h to build from source on the Pi, and trivially
    mockable in tests.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise DexToolError(_MSG_FAILED, f"dex adapter HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise DexToolError(_MSG_FAILED, f"dex adapter unreachable: {e.reason}") from e

    if not body.strip():
        raise DexToolError(_MSG_EMPTY_REPLY, "dex adapter returned an empty body")
    try:
        parsed = json.loads(body)
    except ValueError as e:
        raise DexToolError(
            _MSG_FAILED, f"dex adapter returned non-JSON: {body[:200]!r}"
        ) from e
    if not isinstance(parsed, dict):
        raise DexToolError(
            _MSG_FAILED,
            f"dex adapter returned {type(parsed).__name__}, expected object",
        )
    return parsed


def extract_reply(payload: Dict[str, Any]) -> str:
    """Pull Dex's reply text out of the adapter response, verbatim."""
    for key in _REPLY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def create_dex_tool_handler(
    adapter_url: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poster: Optional[Callable[[str, Dict[str, Any], float], Dict[str, Any]]] = None,
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    """Create the `ask_dex` handler for pipecat's OpenAIRealtimeLLMService.

    `poster` exists for tests; production uses the urllib implementation.
    """
    post = poster or _post_sync
    url = (adapter_url or "").strip()

    async def dex_tool_handler(params: "FunctionCallParams") -> None:
        text = ((params.arguments or {}).get("text") or "").strip()
        logger.info(f"🤖 ask_dex called: {text!r}")
        if not text:
            await params.result_callback(_MSG_EMPTY)
            return
        if not url:
            # No transport exists. Say so plainly rather than let the model fill
            # the silence with something Dex never said.
            logger.error("❌ ask_dex called but dex_adapter_url is not configured")
            await params.result_callback(_MSG_NOT_WIRED)
            return

        payload = {"text": text, "source": "ha-voice-realtime"}
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(post, url, payload, timeout_s),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.error(f"❌ ask_dex timed out after {timeout_s}s")
            await params.result_callback(_MSG_TIMEOUT)
            return
        except DexToolError as e:
            logger.error(f"❌ ask_dex failed: {e.detail}")
            await params.result_callback(e.spoken)
            return
        except Exception as e:  # noqa: BLE001 - a dead tool must still speak
            logger.error(f"❌ ask_dex failed: {e}", exc_info=True)
            await params.result_callback(_MSG_FAILED)
            return

        elapsed_ms = round((loop.time() - started) * 1000, 1)
        reply = extract_reply(response)
        logger.info(f"🤖 ask_dex {elapsed_ms} ms -> {reply[:200]!r}")
        if not reply:
            await params.result_callback(_MSG_EMPTY_REPLY)
            return
        await params.result_callback(reply)

    return dex_tool_handler
