# OpenAI Realtime 2 Voice Agent — Documentation

This add-on runs an **OpenAI `gpt-realtime-2`** voice session and bridges it to Home
Assistant control and web search. It is the backend half of a two-part project; the
front half is custom **firmware for the Home Assistant Voice PE** device (see
[Firmware](#firmware-home-assistant-voice-pe-only) below).

```
Voice PE device  ──WebSocket──▶  this add-on  ──▶  OpenAI Realtime API
(mic up / speaker down)               │  tools
                                       ▼
                     house()  → HA Assist pipeline  → your home
                     ask_dex() → the Dex agent adapter
                     web_search() → the outside world
```

---

## 1. Install the add-on

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Repositories**, add:
   `https://github.com/xandervanerven/ha-openai-realtime`
3. Find **OpenAI Realtime 2 Voice Agent** in the store and click **Install**.
   (There is no prebuilt image — Home Assistant builds it locally the first time,
   which takes a few minutes.)
4. Open the add-on's **Configuration** tab to set it up (next sections).

## 2. Get an OpenAI API key

1. Go to <https://platform.openai.com/> → **API keys** → **Create new secret key**.
2. Make sure **billing** is set up on your OpenAI account — `gpt-realtime-2` (audio)
   and web search are paid usage.
3. Paste the key into the add-on's **`openai_api_key`** option.

> **Heads-up on rate limits:** new accounts start at a low tokens-per-minute (TPM)
> tier. `gpt-realtime-2` audio is token-heavy, so if you see *"Rate limit reached"*
> in the logs, raise your usage tier in the OpenAI dashboard, or keep
> `max_context_messages` modest (default 12).

## 3. Let it control Home Assistant (the `house` tool)

**As of 0.7.0 the MCP path is gone.** The assistant reaches your home through a
single tool, `house`, which runs **Home Assistant's own Assist pipeline** — the
same pipeline a Voice PE uses on stock firmware, with your device's `device_id`
attached so it knows which room you are in.

That means nothing about your home is re-implemented in this add-on. Timers
(which ring on the speaker), shopping and to-do lists, area targeting, media
search and playback, your custom sentences, and your pipeline's own AI fallback
for anything unmatched — all of it is whatever Home Assistant already does, at
whatever speed Home Assistant already does it (15–350 ms for local intents on a
typical house). When HA gains a new intent, the assistant gains it with no
add-on update.

**Setup:**

1. Build (or keep) an Assist pipeline in **Settings → Voice assistants**. Give it
   a name and set **"Prefer handling commands locally"** on, so the fast local
   intents run before any LLM.
2. **Expose the entities** you want voice control over to Assist
   (Settings → Voice assistants → *Exposed entities*). The pipeline only acts on
   what is exposed.
3. In the add-on set **`ha_device_id`** to the device ID of your Voice PE.
   Settings → Devices → open the device → **⋮ → Copy device ID**.
   **This is required.** Without it the pipeline has no room context, and the
   `house` tool is not offered to the model at all — the assistant will not be
   able to touch your home.
4. Set **`assist_pipeline_name`** to your pipeline's name (default `Trixie`). It
   is resolved **by name at run time**, so rebuilding the pipeline does not break
   the add-on — but two pipelines with the same name is refused rather than
   guessed.
5. Leave **`longlived_token`** blank. The add-on authenticates with its own
   Supervisor token against `ws://supervisor/core/websocket`. Only paste a HA
   long-lived token if the log shows a 401/403 at startup.

The add-on log prints one line per call with the elapsed time, whether HA handled
it locally, and the reply:

```
🏠 house called: 'turn on the brewery lights'
🏠 house 214.8 ms local=True type=action_done -> 'Turned on brewery lights'
```

## 3b. Talking to Dex (the `ask_dex` tool)

Optional, and **off unless you configure it**. When `dex_adapter_url` is set, the
assistant gets an `ask_dex` tool it may use **only when you name Dex** — "ask Dex
what's on my calendar tomorrow". Your words go across verbatim and Dex's reply is
spoken back verbatim; the assistant is the microphone, not the editor.

There is **no such endpoint in 980labsOS today**. The supported Dex adapter,
`agents/_runtime/hermes_invoke.py`, is a *local subprocess* adapter — it shells
out to the `hermes` CLI on the gateway host and has no network surface, and this
add-on runs in a container on the Home Assistant host. Inventing a transport is
explicitly out of bounds (`docs/architecture/agent-interaction-model.md`).

So `ask_dex` ships as the client half of this contract, waiting for a server:

```
POST <dex_adapter_url>
Content-Type: application/json
{"text": "<the user's verbatim transcript>", "source": "ha-voice-realtime"}

200 OK
{"reply": "<Dex's verbatim reply text>"}
```

`text`, `response` and `message` are accepted as aliases for `reply`. Any non-2xx,
a non-JSON body, or a body with none of those keys is reported to the user as a
failure — the assistant never invents an answer on Dex's behalf. The call is
bounded at 30 s.

**What still has to be built** (tracked on #1143, not shipped here): a small
authenticated HTTP front for `invoke_hermes_profile(profile="dex", message=...)`
on the gateway host, bound to the LAN. Because that is a new synchronous callable
surface it needs the ratification the interaction model requires before it
carries anything action-bearing.

Leave `dex_adapter_url` blank and the tool is not exposed to the model at all.

## 4. Recommended starting settings

**The defaults are the recommended settings** — for a first run you only need the
API key, `ha_device_id` (section 3), and ideally your language. The
Configuration tab is grouped: **🔑 Basics → 🗣️ Model & voice → 💬 Conversation →
🌐 Web search → 🎚️ Audio → 🏠 Home Assistant → ⚙️ Advanced → 🔍 Debug**, and every
option has plain-language inline help.

| Option | Default | Note |
|---|---|---|
| `openai_model` | `gpt-realtime-2` | newest speech-to-speech model |
| `openai_voice` | `marin` | `marin`/`cedar` are the newest voices |
| `transcription_language` | *(blank)* | set your ISO code (e.g. `nl`): locks the language + logs the user transcript |
| `instructions` | *(English default)* | the system prompt; swap the LANGUAGE line for your language |
| `follow_up_listen_seconds` | `8` | mic stays open this long so you can answer back |
| `follow_up_open_delay_ms` | `700` | echo guard before the follow-up mic opens; lower = snappier but risks ghost turns |
| `wake_open_delay_ms` | `700` | the same echo guard right after the wake chime; lower = snappier wake but risks a ghost turn |
| `vad_eagerness` | `low` | waits longest before deciding you're done talking |
| `playback_prebuffer_ms` | `150` | raise to ~250 if you hear crackle; 0 = play immediately |
| `max_context_messages` | `12` | bounds per-turn token cost |
| `enable_web_search` | `true` | online lookups; set `false` to disable |
| `web_search_model` | `gpt-5.5` | best-quality search model; mini/nano are cheaper |

The legacy `server_vad` turn-detection fields live at the bottom of ⚙️ Advanced and
only appear when you enable **"Show unused optional configuration options"** —
leave them unset unless you have a specific reason.

## 5. Web search

When **`enable_web_search`** is on (**the default**), the assistant gets a `web_search` tool. When
it needs current or general info (weather, news, facts), it calls that tool; the
add-on then makes a **second, server-side OpenAI call** (the Responses API
`web_search` built-in tool, on **`web_search_model`**) and reads a short spoken
answer back.

- Uses your **existing OpenAI key** — no extra account.
- Default model `gpt-5.5` (best quality). Cheaper options trade price/quality
  (`gpt-5.4`, `gpt-5-mini`, the nano models, …) — a few cents per search.
- Adds ~1–3 s while it searches (the device shows "thinking").
- If the model name is rejected, the assistant just says it couldn't search — it
  won't crash the session, so you can change `web_search_model` and retry.

## 6. Options reference & tuning

Every option has a description on the **Configuration** tab. The ones worth knowing:

- **Model / voice / transcription model** are dropdowns with a **`custom`** entry +
  a `*_custom` text field if you want a value not in the list.
- **`transcription_language`** turns the side-channel transcript on. With it set you
  get `🗣️ user: …` lines in the add-on log (handy for debugging); it does **not**
  change what the model understands — the main model hears your audio natively.
- **`follow_up_open_delay_ms` / `playback_prebuffer_ms`** default to `700` / `150`
  — an echo guard and jitter cushion. Lowering them makes the device feel
  snappier, but below ~700 ms open delay the reply's own speaker tail can leak
  into the fresh follow-up mic and become a ghost turn (the assistant "answers
  nobody" or repeats itself); raise the prebuffer if you hear crackle at the
  start of replies.

## 7. Reading the logs

The add-on log shows each turn: `🗣️ user:` (when transcription language is set),
`🤖 assistant:` (the reply text), `📞 phase ->` (device state), tool calls, and
`🔌 …reconnecting` / `✅ reconnected` on a connection recovery. View it on the add-on
**Log** tab.

## Known limitations

- **No voice timers or alarms yet.** Setting a timer by voice isn't supported (the
  official Home Assistant timer intent isn't wired up). Everything else — lights,
  switches, scenes, climate, and questions — works.
- **A brief reconnect about once an hour.** OpenAI limits a realtime session to
  60 minutes. The add-on refreshes proactively during a quiet moment, so you'll
  rarely notice it, but a reconnect can occasionally cause a ~1–2 second pause.
- **Rarely, the assistant may stop itself** on a word in its own reply that sounds
  like "stop" — just ask again.

**Using "stop":** say "stop" (or press the center button) to interrupt the assistant
*while it's speaking* — during a reply, or during the short listening window right
after one. It has no effect before the assistant has started answering (there's
nothing to stop yet).

## Firmware (Home Assistant Voice PE only)

This add-on expects the custom **Voice PE firmware** that turns the device into a
thin client (it streams mic audio here and plays the reply). That firmware:

- is **specific to the Home Assistant Voice PE** hardware (ESP32-S3 + XMOS), and
- lives in its own **public** repository:
  **[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)**.

You flash it once via a tiny per-device "stub" in ESPHome Builder; after that, firmware
updates are **one click** — no tokens, no copy-pasting. That repo has the full
from-scratch guide (flashing + adopting the device in ESPHome Builder).

## Credits

- Backend forked from **[fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)** (Felix Fricke).
- Firmware thin-client design based on **[maxmaxme/home-assistant-voice-pe](https://github.com/maxmaxme/home-assistant-voice-pe)**, a fork of **[esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe)** (Nabu Casa / ESPHome).
- Inspiration from **[marcinnowak79/home-assistant-voice-pe](https://github.com/marcinnowak79/home-assistant-voice-pe)** (gemini-live-proxy).
- Built on **[pipecat-ai](https://github.com/pipecat-ai/pipecat)**, the **OpenAI Realtime API**, and Home Assistant's own **[Assist pipeline](https://www.home-assistant.io/voice_control/)**.
