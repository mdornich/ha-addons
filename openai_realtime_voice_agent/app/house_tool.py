"""The `house` function-tool — Home Assistant's own Assist pipeline.

This is the ONLY way the Realtime assistant touches the home. It does not
re-implement anything: it hands the user's phrasing to the Assist pipeline the
house already runs ("Trixie"), on the puck's `device_id`, and speaks back
whatever the pipeline says.

Why this and not MCP: the MCP path cost 9-14 s per action, carried no device
context (so "turn on the lights" had no room), and had no timers or lists at
all. `assist_pipeline/run` with `start_stage=intent, end_stage=intent` is what
Home Assistant's own voice satellites use — measured 15-350 ms for local
intents on this house, with Claude Sonnet 5 as HA's own fallback when no
sentence matches. Timers started this way are real HA timers keyed to the
device, so the puck rings them. Lists, area targeting, media search, custom
sentences and favourites all come for free, and when HA gains an intent the
assistant gains it with no add-on change.

`end_stage=intent` (not `tts`): the Realtime model speaks the reply in its own
voice, so paying for HA's TTS synthesis would only add latency to a string we
throw away.

Auth is the add-on's `SUPERVISOR_TOKEN` against `ws://supervisor/core/websocket`
(the Supervisor's proxy to Core) — no long-lived token to manage or leak.
"""

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional, TYPE_CHECKING

import websockets

if TYPE_CHECKING:
    from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

# Supervisor's authenticated proxy to Core's websocket API. Available to any
# add-on with `homeassistant_api: true`.
DEFAULT_HA_WS_URL = "ws://supervisor/core/websocket"

# One bounded budget for the whole call (connect + auth + pipeline resolve +
# run). Native intents land in 15-350 ms; HA's own LLM fallback for an unmatched
# phrasing is the slow tail (measured 2.7-3.6 s for Q&A, 12-17 s for tool-using
# action turns). 20 s covers the worst measured turn without letting a wedged
# pipeline hold the voice session open indefinitely.
DEFAULT_TIMEOUT_S = 20.0

# Spoken-safe fallbacks. These are read aloud by the Realtime model, so they are
# sentences, not error codes; the real detail goes to the log.
_MSG_EMPTY = "I didn't catch what you wanted me to do."
_MSG_AUTH = "I can't reach Home Assistant right now — my access was refused."
_MSG_TIMEOUT = "Home Assistant took too long to answer that one."
_MSG_FAILED = "Something went wrong talking to Home Assistant."
_MSG_NO_MATCH = "Home Assistant didn't understand that one."


def get_house_tool_definition() -> Dict[str, Any]:
    """OpenAI Realtime function-tool definition for the house."""
    return {
        "type": "function",
        "name": "house",
        "description": (
            "Do or answer ANYTHING about this home. Use it for lights, "
            "switches, scenes, covers, locks, climate, media and music, "
            "volume, timers (setting, cancelling, and asking what is running "
            "or how much time is left), shopping and to-do lists, sensors, "
            "the weather here, and any question about the state of the house. "
            "Pass the user's request as they said it, in plain English, "
            "including the room if they named one — Home Assistant knows the "
            "rooms and devices, you do not. This is the ONLY way to touch the "
            "home: never guess a device name, never claim an action is done "
            "without calling this, and never answer a question about the house "
            "from your own knowledge. Do not use it for general world "
            "knowledge (that is web_search) or for anything addressed to Dex."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "The user's request in natural English, as spoken, "
                        "e.g. 'turn on the brewery lights', 'set a timer for "
                        "one minute', 'add oat milk to my kroger list', "
                        "'play the Beatles in the office', 'how much time is "
                        "left'."
                    ),
                }
            },
            "required": ["text"],
        },
    }


class HouseToolError(RuntimeError):
    """Raised inside the tool for a condition worth a distinct spoken reply."""

    def __init__(self, spoken: str, detail: str) -> None:
        self.spoken = spoken
        self.detail = detail
        super().__init__(detail)


class HouseClient:
    """One-shot `assist_pipeline/run` caller over the Supervisor websocket.

    Deliberately connection-per-call rather than a held socket: HA restarts,
    add-on restarts and Supervisor proxy hiccups are routine here, and a stale
    held socket fails the FIRST house command after every one of them (the exact
    shape of bug that made the old lane feel unreliable). A fresh connect costs
    single-digit milliseconds on the loopback-ish Supervisor path, which is
    noise next to the pipeline itself. The resolved pipeline id IS cached,
    because that lookup is a real extra round trip, and it is dropped whenever a
    run reports the pipeline is gone.
    """

    def __init__(
        self,
        device_id: str,
        pipeline_name: str = "Trixie",
        token: Optional[str] = None,
        url: str = DEFAULT_HA_WS_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.device_id = device_id
        self.pipeline_name = pipeline_name
        self.token = token
        self.url = url
        self.timeout_s = timeout_s
        self._pipeline_id: Optional[str] = None
        # Serialises concurrent tool calls: the model can fire two house() calls
        # in one turn, and two runs on one device_id confuse HA's satellite
        # state machine.
        self._lock = asyncio.Lock()

    # -- wire helpers ---------------------------------------------------
    async def _recv_json(self, ws) -> Dict[str, Any]:
        while True:
            raw = await ws.recv()
            if isinstance(raw, (bytes, bytearray)):
                continue
            return json.loads(raw)

    async def _auth(self, ws) -> None:
        await self._recv_json(ws)  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        msg = await self._recv_json(ws)
        # Checking the result matters: an invalid token otherwise fails several
        # frames later as an opaque ConnectionClosed.
        if msg.get("type") != "auth_ok":
            raise HouseToolError(_MSG_AUTH, f"HA websocket auth failed: {msg}")

    async def _resolve_pipeline(self, ws, msg_id: int) -> str:
        """Resolve the pipeline BY NAME. A pinned id points at nothing the
        moment the pipeline is rebuilt, and the run would then silently execute
        against HA's default pipeline."""
        if self._pipeline_id:
            return self._pipeline_id
        await ws.send(
            json.dumps({"id": msg_id, "type": "assist_pipeline/pipeline/list"})
        )
        while True:
            msg = await self._recv_json(ws)
            if msg.get("id") != msg_id:
                continue
            if not msg.get("success"):
                raise HouseToolError(
                    _MSG_FAILED, f"pipeline/list failed: {msg.get('error')}"
                )
            pipelines = (msg.get("result") or {}).get("pipelines") or []
            break
        wanted = self.pipeline_name.strip().lower()
        matches = [
            p for p in pipelines if (p.get("name") or "").strip().lower() == wanted
        ]
        if not matches:
            raise HouseToolError(
                _MSG_FAILED,
                f"no Assist pipeline named {self.pipeline_name!r}; have "
                f"{sorted(str(p.get('name')) for p in pipelines)}",
            )
        if len(matches) > 1:
            # Refuse to guess rather than run the house against the wrong brain.
            raise HouseToolError(
                _MSG_FAILED,
                f"{len(matches)} pipelines named {self.pipeline_name!r}: "
                f"{[p.get('id') for p in matches]}",
            )
        self._pipeline_id = matches[0]["id"]
        logger.info(
            f"🏠 resolved pipeline {self.pipeline_name!r} -> {self._pipeline_id}"
        )
        return self._pipeline_id

    async def _run(
        self, ws, msg_id: int, pipeline_id: str, text: str
    ) -> Dict[str, Any]:
        await ws.send(
            json.dumps(
                {
                    "id": msg_id,
                    "type": "assist_pipeline/run",
                    "device_id": self.device_id,
                    "start_stage": "intent",
                    "end_stage": "intent",
                    "pipeline": pipeline_id,
                    "input": {"text": text},
                }
            )
        )
        result: Dict[str, Any] = {
            "speech": None,
            "response_type": None,
            "processed_locally": None,
            "conversation_id": None,
            "error": None,
        }
        while True:
            msg = await self._recv_json(ws)
            if msg.get("id") != msg_id:
                continue
            if msg.get("type") == "result" and not msg.get("success"):
                err = msg.get("error") or {}
                # A dead pipeline id must not stay cached, or every subsequent
                # command fails the same way until the add-on restarts.
                self._pipeline_id = None
                raise HouseToolError(_MSG_FAILED, f"assist_pipeline/run failed: {err}")
            event = msg.get("event") or {}
            etype = event.get("type")
            data = event.get("data") or {}
            if etype == "intent-end":
                intent_output = data.get("intent_output") or {}
                response = intent_output.get("response") or {}
                speech = ((response.get("speech") or {}).get("plain") or {}).get(
                    "speech"
                )
                result["speech"] = speech
                result["response_type"] = response.get("response_type")
                result["conversation_id"] = intent_output.get("conversation_id")
                # `processed_locally` lives on the EVENT data, not inside
                # intent_output — reading it off intent_output returns None
                # every time and destroys the local-vs-LLM classification.
                result["processed_locally"] = data.get("processed_locally")
            elif etype == "error":
                result["error"] = data
                break
            elif etype == "run-end":
                break
        return result

    async def ask(self, text: str) -> Dict[str, Any]:
        async with self._lock:
            async with websockets.connect(
                self.url, max_size=None, ping_interval=None
            ) as ws:
                await self._auth(ws)
                pipeline_id = await self._resolve_pipeline(ws, 1)
                return await self._run(ws, 2, pipeline_id, text)


def create_house_tool_handler(
    device_id: str,
    pipeline_name: str = "Trixie",
    token: Optional[str] = None,
    url: str = DEFAULT_HA_WS_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Optional[HouseClient] = None,
) -> Callable[["FunctionCallParams"], Awaitable[None]]:
    """Create the `house` handler for pipecat's OpenAIRealtimeLLMService."""
    if client is None:
        client = HouseClient(
            device_id=device_id,
            pipeline_name=pipeline_name,
            token=token or os.environ.get("SUPERVISOR_TOKEN", ""),
            url=url,
            timeout_s=timeout_s,
        )

    async def house_tool_handler(params: "FunctionCallParams") -> None:
        text = ((params.arguments or {}).get("text") or "").strip()
        logger.info(f"🏠 house called: {text!r}")
        if not text:
            await params.result_callback(_MSG_EMPTY)
            return

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            result = await asyncio.wait_for(client.ask(text), timeout=client.timeout_s)
        except asyncio.TimeoutError:
            logger.error(f"❌ house timed out after {client.timeout_s}s: {text!r}")
            await params.result_callback(_MSG_TIMEOUT)
            return
        except HouseToolError as e:
            logger.error(f"❌ house failed: {e.detail}")
            await params.result_callback(e.spoken)
            return
        except Exception as e:  # noqa: BLE001 - a dead tool must still speak
            logger.error(f"❌ house failed: {e}", exc_info=True)
            await params.result_callback(_MSG_FAILED)
            return

        elapsed_ms = round((loop.time() - started) * 1000, 1)
        speech = (result.get("speech") or "").strip()
        # Logged on one line so the §5 measurement script can pull per-turn tool
        # time and the local-vs-LLM split straight out of the add-on log.
        logger.info(
            f"🏠 house {elapsed_ms} ms local={result.get('processed_locally')} "
            f"type={result.get('response_type')} -> {speech[:200]!r}"
        )
        if not speech:
            await params.result_callback(_MSG_NO_MATCH)
            return
        await params.result_callback(speech)

    return house_tool_handler
