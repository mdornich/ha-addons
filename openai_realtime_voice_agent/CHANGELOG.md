# Changelog

All notable changes to this add-on. Newest first.

## 0.7.8

- **The follow-up window now logs WHEN the speech landed, not just how much.**
  2026-09-02 23:02: the wake turn read `🎚️ VAD speech_started — level so far:
  peak=29172 (-1.0 dBFS) rms=5196 loud=560ms/1280ms`, and the follow-up window
  ten seconds later read `🧽 follow-up cut-off → input_audio_buffer.clear (...
  only 0 ms of speech; level peak=3541 (-19.3 dBFS) rms=494 (-36.4 dBFS)
  loud=2896ms/9984ms)`. A ~3 s sentence WAS spoken into that window; it arrived
  18 dB below the wake turn and OpenAI's VAD never fired. (On the working 21:38
  run the window peaked at -1.1 dBFS.) The suspicion is that the puck's XMOS
  echo canceller attenuates near-end speech for a while after the speaker plays
  — the reply, then the follow-up chime — so WHEN in the window the user speaks
  decides whether they are heard at all. Totals cannot answer that.
  `InputBufferTracker` now keeps a per-second peak profile since the last reset
  (bucketed by accumulated audio_ms, so it is deterministic and testable) and
  the offset of the first speech-like frame, and appends both to every existing
  level line — cut-off reasons, `VAD speech_started`, reset debug:
  `... loud=2896ms/9984ms profile(dBFS/s)=[-37,-36,-19,-20,-21,-35,-36,-36,-36,-36]
  first_loud=+2.4s`. Diagnostic only; no behaviour change.

## 0.7.7

- **A stuck "the user is still speaking" flag, unstuck at the bot-reply
  boundary.** 2026-09-02 21:38: "Hey Trixie, can you pause the media room?" was
  transcribed at 21:38:20,693 and 9 ms later logged `🎚️ VAD speech_started` —
  pipecat's EMULATED UserStartedSpeaking, synthesised from the transcript rather
  than a server VAD event. Its matching stop never reached the tracker, because
  the PhaseEmitter swallowed it at 21:38:21,506
  (`📞 'thinking' suppressed — bot is replying (stale VAD tail)`). So
  `InputBufferTracker.speaking` stayed True through a silent 10 s follow-up
  window and 21:38:34 read it as
  `🗣️ follow-up cut-off → ... (user was still speaking at the cut-off; level ...
  loud=1952ms/11200ms)`. The tracker now takes the start of the bot's reply as a
  hard turn boundary and resets there, and that stale-tail suppression — which
  is about the LED, not about buffer accounting — still tells the tracker the
  user's turn ended.

- **No more answering a commit that had nothing in it.** The same cut-off sent
  `input_audio_buffer.commit` and `response.create` back to back; OpenAI replied
  `⚠️ benign realtime error ignored: input_audio_buffer_commit_empty` and the
  orphan response said "Goodbye, Mitch." to an empty room. The commit now waits
  up to 1.5 s to learn its own outcome — `input_audio_buffer.committed` means a
  real turn and gets its `response.create`, an empty commit or no answer at all
  logs `🧽 follow-up cut-off → commit was empty, no response requested` and
  closes the turn instead of speaking into the dark.

## 0.7.6

- **We can finally see whether the puck was streaming anything.** 2026-09-01
  21:04: the first turn answered, then the follow-up question spoken inside the
  10 s window never became a turn —
  `🧽 follow-up cut-off → ... only 0 ms of speech (< 600 ms)` with a full window
  of audio in the buffer. Same signature 08-30 21:37 and 08-31 09:53. That line
  could not tell a mic whose gain was never restored after playback apart from
  real speech OpenAI's server VAD ignored, and there is no shell on the HA host
  to pull a recording. So the tracker now measures the audio itself — peak, RMS
  (both int16 scale and dBFS) and `loud` ms of frames at or above ~-36 dBFS,
  a speech-like-audio count that owes nothing to the VAD — and every cut-off
  reason carries it. `speech_started` logs the same summary, so a detected
  utterance gives us the reference level to compare a missed one against.

## 0.7.5

- **The reconnect that could never succeed.** pipecat's `reset_conversation()`
  disconnects, then replays completed function calls via
  `self._context.get_messages()`, then reconnects — but `_context` is None until
  the first real turn and our startup pre-seed only ran under semantic_vad while
  the live config runs server_vad. The pipeline is built once per add-on
  process, so from every add-on start until the first real turn any connection
  death — a drop, the 60-min cap, the proactive refresh — raised
  `AttributeError` between the disconnect and the reconnect, and every retry hit
  the same wall (wake chime, then silence, until the add-on was restarted). `SafeRealtimeLLMService` now reimplements the reset:
  it pre-seeds an empty context when there is none, never lets the replay abort
  the reconnect, and always reconnects in a `finally`. The startup pre-seed also
  runs for every turn-detection mode now.
- **A dead session can no longer hide.** The background loop is now a health
  loop: if the OpenAI session has had no websocket (or never became
  api-session-ready) for 20 s it forces a recovery on its own — the reactive path
  needs ErrorFrames, and a silently absent session produces none. After three
  consecutive failed reconnects the log carries one `REALTIME_SESSION_WEDGED`
  line with the last error, so a zombie add-on is greppable instead of invisible.

## 0.7.3

- **`ask_dex` now has a server to talk to, and authenticates to it.** 980labsOS
  ships `dex-voice-ask` (launchd on the Mac mini, `agents/dex/voice/dex_voice_ask.py`),
  the authenticated HTTP front for the Hermes Dex adapter — ratified into
  `docs/architecture/agent-interaction-model.md`'s inline-query exception list on
  2026-08-30 as a verbatim relay. New option `dex_adapter_token` (password): when
  set it is sent as `Authorization: Bearer ...` on every adapter call. Without it
  0.7.2 sent no credential at all, so every call to a token-protected adapter
  would have been a 401. The token is never logged; the start log shows only
  `token=set`/`token=unset`.

## 0.7.2

- **A conversation now ends.** 0.7.1 reopened the follow-up window after EVERY
  assistant reply, so any answered utterance restarted the chain: ambient room
  speech kept getting answered and the turn never terminated — observed live
  2026-08-30 as 2+ minutes of continuously billed audio with the LED ring never
  going dark. New option `follow_up_max_turns` (default `3`, `0` = unlimited)
  counts consecutive completed assistant turns since the last wake; on reaching
  the cap the add-on pushes `follow_up_ms: 0` to the device just before the
  end-of-reply `idle`, so the window does not reopen and the puck falls back to
  wake-word-only. The counter resets on wake, on device reconnect, and on
  session recreate (a new pipeline builds a new counter). Logs
  `follow-up chain cap reached (3) — waiting for wake word`.

## 0.7.1

Three fixes from the first in-room session on 0.7.0.

- **A follow-up window that expires mid-question no longer throws the question
  away.** The device closes its follow-up mic on a timer and sends
  `{"type":"flush"}`; 0.7.0 answered that with an unconditional
  `input_audio_buffer.clear`, so a question the server VAD had not committed yet
  was discarded — no answer, ring still circling (observed 13:49:01). The
  cut-off now COMMITS the buffer and creates a response when it holds real
  speech (~600 ms, or the user was still talking), and only clears when the
  buffer is effectively empty. New `app/input_buffer.py` counts speech — not
  audio — since the last commit, so a silent 20 s window still clears.
  Tunable with `FOLLOW_UP_COMMIT_MIN_SPEECH_MS` / `FOLLOW_UP_SPEECH_GRACE_MS`.
- **The `house` tool is told how to phrase a request.** The model was relaying
  the user's words as reported speech — `house` called with "I asked if it was
  going to rain" — which matches no Home Assistant sentence and falls to the
  slow LLM path (2.9 s) where the literal "is it going to rain" is answered
  locally in 16 ms. The tool description now asks for one short imperative or
  question in the present tense, framing stripped, with worked examples; the
  persona says the same thing from the other side.
- **The follow-up cut-off logic moved out of a closure** into
  `app/follow_up.py` so it can be unit-tested. It could not be, before.

## 0.7.0

**The home is Home Assistant's again.** The MCP client path is removed
entirely — `mcp_service.py`, `mcp_error_reporting.py`, the `ha_mcp_url` and
`mcp_tool_allowlist` options, all of it. It cost 9–14 s per action, carried no
device context, and had no timers or lists.

In its place the assistant gets **one** house tool:

- **`house(text)`** runs Home Assistant's own Assist pipeline
  (`assist_pipeline/run`, `start_stage=intent`, `end_stage=intent`) on your
  device, resolved by pipeline name at run time. Native timers that ring on the
  speaker, lists, area targeting, media, custom sentences, and your pipeline's
  own AI fallback — none of it re-implemented here, all of it at the speed HA
  already runs it. New options: `ha_device_id` (**required**),
  `assist_pipeline_name` (default `Trixie`).
- **`ask_dex(text)`** relays a message to the Dex agent, verbatim in and
  verbatim out, and only when the user names Dex. New option `dex_adapter_url`;
  blank (the default) means the tool is not offered to the model at all. NOTE:
  no such endpoint exists yet — see DOCS.md §3b for the contract a server must
  satisfy.
- `web_search` is unchanged and is now explicitly scoped to the outside world.

## 0.6.2

- Failure reporting now also catches Home Assistant's own "I don't know that
  device" style answers and partly-failed actions, which previously logged as
  clean successes, and an error that arrives after other output. A service that
  ran and reported a problem is no longer described to the assistant as one that
  never ran.

## 0.6.1

**Failed tool calls are now reported as failures**

- When a Home Assistant tool call failed, the log said the tool "completed
  successfully" and the assistant was handed the raw Python error text. A broken
  tool could therefore fail every time with nothing in the log to find it by.
  Failures are now logged as failures, with the reason, and the assistant is told
  plainly that the action did not run.

## 0.6.0

> ⚠️ **This update has two parts — please update both:**
> 1. **This add-on** (the update you're installing now).
> 2. **The Voice PE firmware** — open **ESPHome Device Builder** and click **Update** (or **Install**) on your device.
>
> The device and the add-on use one shared protocol; updating only one half can cause odd behaviour.

A reliability and voice-control polish release.

**Stop word**

- **Saying "stop" now usually works on the first try.** The spoken "stop" could
  previously be answered by the assistant a moment later, so you sometimes had to
  repeat it; that follow-on reply is now cancelled, so a single "stop" is
  typically enough.
- **Saying "stop" during a web search returns the device to rest promptly** — the
  light ring no longer keeps showing the "replying" animation for several seconds.
- **Fewer accidental stops** on the assistant's own speech.
- The light ring briefly flashes **red** to confirm your "stop" was registered. *(firmware)*

**Reliability**

- **No more unresponsive sessions.** A silently dropped connection to OpenAI is
  now detected and repaired within seconds, instead of leaving the assistant deaf
  until a restart.
- **The roughly hourly reconnect now happens proactively during a quiet moment**,
  so it practically never interrupts a conversation.
- **Smart-home commands are no longer cancelled** if you keep talking while they run.
- The light can no longer get **stuck on "thinking"**, and long web searches get
  all the time they need.

**No more "answers out of nowhere"**

- The assistant no longer occasionally replies — or repeats its previous answer —
  right after the wake word when you said nothing.
- A sentence that got cut off is no longer answered minutes later on your next wake.

**Settings**

- New **"Wake mic delay"** setting: a short pause after the wake chime before the
  mic opens, so the chime can't be mistaken for speech (default 700 ms).
- The **"Follow-up mic delay"** default is now **700 ms**. Existing installs keep
  their saved value — raise yours if the assistant ever answers right after its
  own reply.

## 0.5.0

A big stable release: everything built and tested on the dev channel over the
past days. **Also update the Voice PE firmware** (v1.1.0 — one click in ESPHome
Builder) to get the full effect of the "stop" improvements; the two halves
work best together.

- **"Stop" now works through the whole reply AND the after-reply listening
  window.** The device detects the word more reliably, and the bridge treats
  it as authoritative: in-flight audio is discarded and an answer OpenAI had
  already started for the stop word itself is cancelled on arrival — no more
  "Okay, I'll be quiet" replies to your "stop".
- **Fixed: an answer could cut off mid-sentence, after which the assistant
  went deaf** until the next reconnect. Harmless protocol races (e.g. your
  sentence being split into two turns by a pause) no longer kill the session.
- **Fixed an audio race that could inject noise/hiss into replies** (firmware,
  paired with this release).
- **Mute behaves properly now** (firmware): the ring goes dark with red
  markers by the microphones, and muting also ends an open listening window
  immediately — both from Home Assistant and with the physical side switch.
- **The LED Ring switch in Home Assistant works again** (firmware): entity off
  = device dark at rest; entity on = the gentle "ready" pulse.
- **Completely reworked Configuration tab**: options grouped logically
  (Basics → Model & voice → Conversation → Web search → Audio →
  Home Assistant → Advanced), every description rewritten in plain practical
  language, and a full Dutch translation included (shown automatically when
  your HA is set to Dutch). Confusing or broken switches were removed; rarely
  needed expert fields stay hidden until you need them.
- **The add-on now has its own icon.**
- Friendlier defaults for new installs: follow-up mic delay 200 ms and
  playback buffer 150 ms. **Existing installs keep their saved values** — if
  yours still say 0, consider setting 200/150 manually (Conversation / Audio
  groups) for fewer ghost triggers and less crackle.

### Heads-up: the firmware stub template was improved

The per-device stub in ESPHome Builder used to reference the firmware in a
form that lets ESPHome **cache the downloaded YAML for a day** — clicking
Update shortly after a release could then silently rebuild yesterday's code.
The stub templates in the firmware repo are fixed; existing users can apply
the same fix once by replacing **only the `packages:` block** in their
device's YAML in ESPHome Builder (everything else — your name, secrets,
`dashboard_import` — stays exactly the same):

```yaml
packages:
  realtime:
    url: https://github.com/xandervanerven/home-assistant-voice-pe
    ref: main
    files: [home-assistant-voice.realtime.yaml]
    refresh: 0s
```

Current templates for reference:
[esphome-builder.dhcp.yaml](https://github.com/xandervanerven/home-assistant-voice-pe/blob/main/esphome-builder.dhcp.yaml) ·
[esphome-builder.static-ip.yaml](https://github.com/xandervanerven/home-assistant-voice-pe/blob/main/esphome-builder.static-ip.yaml)

## 0.4.26

- **Web search is now ON by default**, using **gpt-5.5** (the best-quality search
  model), so the assistant can look things up online — weather, news, facts — out
  of the box. **Existing installs keep their saved setting**: if you had it off,
  switch `enable_web_search` on (and set `web_search_model` to `gpt-5.5`) in the
  add-on Configuration. The cheaper mini/nano models stay available.

## 0.4.25

- **Fix:** the first thing you said in the few seconds right after an automatic
  reconnect (e.g. after the 60-minute session cap) could be ignored
  (`conversation_already_has_active_response`). The reconnected session no longer
  creates a duplicate response, so that turn answers normally.

## 0.4.24

- **Renamed** to **OpenAI Realtime 2 Voice Agent**.
- Rewrote the store/info description and added a full **Documentation** tab
  (install steps, OpenAI key, Home Assistant MCP setup, recommended settings, web
  search, credits). Removed stale text from the original upstream client.
- Default system prompt is now an English, voice-tuned prompt (silent tool calls,
  varied confirmations, language pinning). Your own saved prompt is not changed.
- Default `follow_up_open_delay_ms` and `playback_prebuffer_ms` set to `0` (raise
  them if the device hears its own tail or you hear crackle).

## 0.4.23

- **Fix:** the 60-minute session cap sometimes left the session dead until a
  restart. It now reconnects automatically in all cases (both the keepalive-drop
  and the `session_expired` forms).

## 0.4.22

- **New options:** voice **speed** (0.25–1.5), **max reply length**
  (`max_output_tokens`), and **input noise reduction** (off / near-field /
  far-field). All default to current behaviour.

## 0.4.21

- Model, voice, web-search-model and transcription-model options are now
  **dropdowns** with the known-good values, each with a **custom** entry if you
  need a value not in the list.

## 0.4.20

- **New:** optional **web search**. Turn on `enable_web_search` to let the
  assistant look things up online (weather, news, facts). Uses your OpenAI key;
  off by default. Model configurable via `web_search_model` (default gpt-5.4-mini).

## 0.4.19

- Clarified the MCP option help text for both the built-in HA MCP Server and the
  unofficial ha-mcp add-on.

## 0.4.18

- **Fix:** removed a meaningless filler reply ("I'm ready to continue…") that could
  appear on the first turn of a session.

## 0.4.17

- **Fix:** cap restored conversation history (`max_context_messages`, default 12) to
  bound per-turn token cost and avoid hitting OpenAI's rate limit.

## 0.4.16

- **Fix:** the device no longer gets stuck blinking "thinking" after a turn-ending
  error (e.g. a rate limit) — it returns to idle so you can retry.

## 0.4.14

- **New:** `playback_prebuffer_ms` jitter buffer to reduce occasional crackle at the
  start of replies.

## 0.4.12 – 0.4.13

- **Fix:** "say stop, then immediately ask again → silence". Disabled the broken
  server-side audio truncation that wedged the next turn.

## 0.4.9 – 0.4.11

- **New:** auto-reconnect the OpenAI Realtime session when its connection drops
  (keepalive timeout / 60-minute cap), instead of going dead until a restart.
  Refined so a normal device disconnect doesn't trigger an unnecessary reconnect.

## 0.4.6 – 0.4.8

- **New:** configurable post-reply **follow-up listening window** (answer back
  without re-saying the wake word) + its open-delay, and per-option help text in the
  UI.
- **New:** the assistant's and user's transcripts are logged to the add-on log
  (`🤖 assistant:` / `🗣️ user:`).

## 0.4.0 – 0.4.4

- **Fix:** resample the device's 16 kHz mic to the 24 kHz OpenAI requires (garbled
  speech), and drop empty audio chunks.
- **New:** device **"stop"** interrupt now actually cancels the reply and clears
  buffered audio.

## 0.3.x

- Switched the target to **gpt-realtime-2**, pinned pipecat-ai 0.0.97, and tuned
  turn detection (semantic VAD), phase delivery to the device, and the startup
  sequence to stop double-responses. Made the disconnect tool and transcription
  model configurable.

## Earlier

- Initial pipecat + WebSocket implementation (forked from
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)).
