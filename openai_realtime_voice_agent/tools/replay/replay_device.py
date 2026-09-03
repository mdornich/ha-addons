#!/usr/bin/env python3
"""A fake Voice PE puck: replay recorded audio at a locally running add-on.

Speaks the same websocket protocol the real `va_client` firmware does (see
app/raw_audio_serializer.py and app/websocket_handler.py):

  device -> add-on   TEXT  {"type":"start"}       once per connection
                     TEXT  {"type":"wake"}        every wake word
                     TEXT  {"type":"flush"}       a follow-up window expired
                     TEXT  {"type":"interrupt"}   the "stop" wake word
                     BIN   PCM16 mono 16 kHz mic audio

  add-on -> device   TEXT  {"type":"hello", "follow_up_ms":N,
                            "follow_up_open_delay_ms":N,
                            "wake_open_delay_ms":N,
                            "playback_prebuffer_ms":N, "audio_out":"pcm"}
                     TEXT  {"type":"phase","value":"listening|thinking|replying|idle"}
                     BIN   TTS audio (24 kHz PCM16)

What it measures per turn: wake -> listening, listening -> thinking,
thinking -> first TTS byte, and total TTS bytes received.

It does NOT model the puck: no XMOS echo canceller, no room acoustics, no
speaker. See README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import time
import wave

DEVICE_RATE = 16000
FRAME_MS = 20
BYTES_PER_SAMPLE = 2
FRAME_BYTES = int(DEVICE_RATE * FRAME_MS / 1000) * BYTES_PER_SAMPLE  # 640

VALID_KINDS = ("wake", "followup")


# --------------------------------------------------------------------------
# pure helpers (unit-tested; no network)
# --------------------------------------------------------------------------


def parse_turn_spec(spec: str) -> tuple[str, str]:
    """Parse a `--turn wake:/path/x.wav` argument into (kind, path)."""
    kind, sep, path = spec.partition(":")
    if not sep or not path:
        raise ValueError(f"bad --turn {spec!r}: expected <wake|followup>:<path.wav>")
    if kind not in VALID_KINDS:
        raise ValueError(f"bad --turn kind {kind!r}: expected one of {VALID_KINDS}")
    return kind, path


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolate PCM16 mono from src_rate to dst_rate.

    Deliberately naive: fixtures are generated at 16 kHz already, so this is a
    convenience path for arbitrary WAVs, not a quality resampler.
    """
    if src_rate == dst_rate or not data:
        return data
    n_in = len(data) // BYTES_PER_SAMPLE
    n_out = max(1, int(n_in * dst_rate / src_rate))
    step = n_in / n_out
    src = memoryview(data).cast("h")
    out = bytearray(n_out * BYTES_PER_SAMPLE)
    dst = memoryview(out).cast("h")
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j + 1 < n_in else a
        dst[i] = int(a + (b - a) * frac)
    return bytes(out)


def load_pcm16_mono_16k(path: str) -> bytes:
    """Read a WAV file as PCM16 mono 16 kHz bytes."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != BYTES_PER_SAMPLE:
            raise ValueError(f"{path}: only 16-bit PCM WAVs are supported")
        channels = w.getnchannels()
        rate = w.getframerate()
        data = w.readframes(w.getnframes())
    if channels == 2:
        src = memoryview(data).cast("h")
        mono = bytearray(len(data) // 2)
        dst = memoryview(mono).cast("h")
        for i in range(len(dst)):
            dst[i] = (src[2 * i] + src[2 * i + 1]) // 2
        data = bytes(mono)
    elif channels != 1:
        raise ValueError(f"{path}: {channels} channels is not supported")
    return resample_pcm16(data, rate, DEVICE_RATE)


def frame_pcm(data: bytes, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    """Split PCM into fixed-size frames, zero-padding the tail.

    The add-on drops any frame with an odd byte count, so frames must stay
    sample-aligned; a short tail is padded rather than sent ragged.
    """
    if frame_bytes <= 0 or frame_bytes % BYTES_PER_SAMPLE:
        raise ValueError("frame_bytes must be a positive even number")
    frames = []
    for off in range(0, len(data), frame_bytes):
        chunk = data[off : off + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        frames.append(chunk)
    return frames


def _delta_ms(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a) * 1000.0, 1)


def build_report(turns: list[dict], meta: dict) -> dict:
    """Turn raw per-turn event stamps into the report structure.

    Each input turn dict carries monotonic timestamps (or None) under
    `wake_at`, `mic_open_at`, `phases` (list of {value, at}), `first_tts_at`,
    `tts_bytes`, plus `index`/`kind`/`wav`.
    """
    out_turns = []
    for t in turns:
        phases = t.get("phases", [])
        first = {}
        for p in phases:
            first.setdefault(p["value"], p["at"])
        latencies = {
            "wake_to_listening_ms": _delta_ms(t.get("wake_at"), first.get("listening")),
            "mic_open_to_listening_ms": _delta_ms(
                t.get("mic_open_at"), first.get("listening")
            ),
            "listening_to_thinking_ms": _delta_ms(
                first.get("listening"), first.get("thinking")
            ),
            "thinking_to_first_tts_byte_ms": _delta_ms(
                first.get("thinking"), t.get("first_tts_at")
            ),
            "thinking_to_replying_ms": _delta_ms(
                first.get("thinking"), first.get("replying")
            ),
            "replying_to_idle_ms": _delta_ms(first.get("replying"), first.get("idle")),
        }
        origin = t.get("wake_at") or t.get("mic_open_at")
        out_turns.append(
            {
                "index": t["index"],
                "kind": t["kind"],
                "wav": t["wav"],
                "phase_sequence": [p["value"] for p in phases],
                "phases": [
                    {"value": p["value"], "t_ms": _delta_ms(origin, p["at"])}
                    for p in phases
                ],
                "tts_bytes": t.get("tts_bytes", 0),
                "text_messages": t.get("text_messages", []),
                "flush_sent": t.get("flush_sent", False),
                "interrupt_sent": t.get("interrupt_sent", False),
                "latencies": latencies,
            }
        )
    return {"meta": meta, "turns": out_turns}


def summarize(report: dict) -> str:
    """Human-readable summary of a report."""
    lines = [
        f"replay: {report['meta'].get('url')}  "
        f"hello={json.dumps(report['meta'].get('hello'), sort_keys=True)}"
    ]
    for t in report["turns"]:
        lat = t["latencies"]

        def ms(key: str) -> str:
            v = lat.get(key)
            return "  --  " if v is None else f"{v:7.0f}"

        lines.append(
            f"turn {t['index']} [{t['kind']}] {t['wav']}\n"
            f"  phases      : {' -> '.join(t['phase_sequence']) or '(none)'}\n"
            f"  wake->listen: {ms('wake_to_listening_ms')} ms\n"
            f"  listen->think:{ms('listening_to_thinking_ms')} ms\n"
            f"  think->TTS  : {ms('thinking_to_first_tts_byte_ms')} ms\n"
            f"  reply->idle : {ms('replying_to_idle_ms')} ms\n"
            f"  TTS bytes   : {t['tts_bytes']}"
        )
        if t.get("flush_sent"):
            lines[-1] += "\n  flush sent  : yes (follow-up window expired)"
        if t.get("interrupt_sent"):
            lines[-1] += "\n  interrupt   : yes"
    for line in report["meta"].get("transcript", []):
        lines.append(f"  log: {line}")
    return "\n".join(lines)


def scrape_transcript(container: str, since: str) -> list[str]:
    """Pull assistant/user transcript lines out of the container log."""
    try:
        proc = subprocess.run(
            ["docker", "logs", "--since", since, container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        return [f"(docker logs failed: {exc!r})"]
    lines = []
    for line in (proc.stdout + proc.stderr).splitlines():
        if "assistant:" in line or "user:" in line:
            lines.append(line.strip())
    return lines


# --------------------------------------------------------------------------
# the fake puck
# --------------------------------------------------------------------------


class ReplayDevice:
    def __init__(self, url: str, args: argparse.Namespace):
        self.url = url
        self.args = args
        self.hello: dict = {}
        self.turns: list[dict] = []
        self.current: dict | None = None
        self.text_messages: list[dict] = []
        self._idle_event = asyncio.Event()
        self._closed = asyncio.Event()

    # -- receive ----------------------------------------------------------
    async def _reader(self, ws) -> None:
        async for message in ws:
            now = time.monotonic()
            if isinstance(message, bytes):
                if self.current is not None:
                    if self.current.get("first_tts_at") is None:
                        self.current["first_tts_at"] = now
                    self.current["tts_bytes"] += len(message)
                continue
            try:
                data = json.loads(message)
            except ValueError:
                continue
            self.text_messages.append({"at": now, "raw": data})
            if self.current is not None:
                self.current["text_messages"].append(data)
            kind = data.get("type")
            if kind == "hello":
                self.hello = data
            elif kind == "phase":
                value = data.get("value")
                if self.current is not None:
                    self.current["phases"].append({"value": value, "at": now})
                if value == "idle":
                    self._idle_event.set()
                else:
                    self._idle_event.clear()
        self._closed.set()

    # -- send -------------------------------------------------------------
    async def _send_json(self, ws, obj: dict) -> None:
        await ws.send(json.dumps(obj, separators=(",", ":")))

    async def _stream(self, ws, frames: list[bytes], turn: dict) -> None:
        """Stream mic frames in real time, honouring --stop-at-ms."""
        start = time.monotonic()
        stop_at = self.args.stop_at_ms
        for i, frame in enumerate(frames):
            target = start + (i * FRAME_MS) / 1000.0
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if stop_at is not None and not turn["interrupt_sent"]:
                if (time.monotonic() - start) * 1000.0 >= stop_at:
                    await self._send_json(ws, {"type": "interrupt"})
                    turn["interrupt_sent"] = True
            await ws.send(frame)

    async def _run_turn(self, ws, index: int, kind: str, path: str) -> None:
        pcm = load_pcm16_mono_16k(path)
        frames = frame_pcm(pcm)
        turn: dict = {
            "index": index,
            "kind": kind,
            "wav": path,
            "wake_at": None,
            "mic_open_at": None,
            "phases": [],
            "text_messages": [],
            "first_tts_at": None,
            "tts_bytes": 0,
            "flush_sent": False,
            "interrupt_sent": False,
        }
        self.turns.append(turn)
        self.current = turn

        if kind == "wake":
            await self._send_json(ws, {"type": "wake"})
            turn["wake_at"] = time.monotonic()
            delay_ms = int(self.hello.get("wake_open_delay_ms", 700))
        else:
            # The real device only opens a follow-up mic after the reply has
            # truly finished (the debounced `idle`) plus the echo-tail delay.
            try:
                await asyncio.wait_for(
                    self._idle_event.wait(), timeout=self.args.followup_idle_timeout_s
                )
            except asyncio.TimeoutError:
                print(
                    f"turn {index}: no idle phase within "
                    f"{self.args.followup_idle_timeout_s}s — opening the mic anyway",
                    file=sys.stderr,
                )
            delay_ms = int(self.hello.get("follow_up_open_delay_ms", 700))
            if int(self.hello.get("follow_up_ms", 0)) <= 0:
                print(
                    f"turn {index}: server says follow_up_ms=0 — the real device "
                    "would NOT open a window here",
                    file=sys.stderr,
                )
        await asyncio.sleep(delay_ms / 1000.0)
        turn["mic_open_at"] = time.monotonic()
        # Reset so a stale idle from the previous turn can't satisfy the next.
        self._idle_event.clear()

        await self._stream(ws, frames, turn)

        # After the audio, hold the mic open the way the firmware does and wait
        # for the server to react. A follow-up window that expires with nothing
        # committed is what produces {"type":"flush"}.
        window_ms = int(self.hello.get("follow_up_ms", 0)) if kind == "followup" else 0
        deadline = turn["mic_open_at"] + (
            window_ms / 1000.0 if window_ms else self.args.turn_timeout_s
        )
        silence = b"\x00" * FRAME_BYTES
        reacted = False
        while time.monotonic() < deadline:
            if any(p["value"] in ("thinking", "replying") for p in turn["phases"]):
                reacted = True
                break
            await asyncio.sleep(FRAME_MS / 1000.0)
            await ws.send(silence)
        if kind == "followup" and window_ms and not reacted:
            await self._send_json(ws, {"type": "flush"})
            turn["flush_sent"] = True

        # Wait for the reply to finish (idle) so the next turn starts clean.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._idle_event.wait(), timeout=self.args.turn_timeout_s
            )
        self.current = None

    async def run(self) -> dict:
        import websockets

        started_wall = time.time()
        async with websockets.connect(self.url, max_size=None) as ws:
            reader = asyncio.create_task(self._reader(ws))
            # The real device sends this once per CONNECTION, not per wake.
            await self._send_json(ws, {"type": "start"})
            # Give the server's `hello` a moment to arrive before turn 1 needs
            # its wake_open_delay_ms.
            for _ in range(50):
                if self.hello:
                    break
                await asyncio.sleep(0.02)
            for index, spec in enumerate(self.args.turn, start=1):
                kind, path = parse_turn_spec(spec)
                await self._run_turn(ws, index, kind, path)
                if index != len(self.args.turn):
                    await asyncio.sleep(self.args.gap_ms / 1000.0)
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

        meta = {
            "url": self.url,
            "hello": self.hello,
            "started_at": started_wall,
            "gap_ms": self.args.gap_ms,
        }
        if self.args.container:
            since = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(started_wall - 2)
            ) + "Z"
            meta["transcript"] = scrape_transcript(self.args.container, since)
        return build_report(self.turns, meta)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="ws://127.0.0.1:8080/")
    p.add_argument(
        "--turn",
        action="append",
        required=True,
        metavar="wake|followup:PATH.wav",
        help="a turn to replay; repeat for multi-turn runs",
    )
    p.add_argument("--gap-ms", type=int, default=500, help="pause between turns")
    p.add_argument(
        "--stop-at-ms",
        type=int,
        default=None,
        help='send {"type":"interrupt"} this many ms into each turn\'s audio',
    )
    p.add_argument("--container", default=None, help="docker container to scrape logs from")
    p.add_argument("--report", default="report.json")
    p.add_argument("--turn-timeout-s", type=float, default=30.0)
    p.add_argument("--followup-idle-timeout-s", type=float, default=30.0)
    return p


async def _amain(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    for spec in args.turn:
        parse_turn_spec(spec)  # fail fast on a typo before touching the network
    device = ReplayDevice(args.url, args)
    report = await device.run()
    with open(args.report, "w") as fh:
        json.dump(report, fh, indent=2)
    print(summarize(report))
    print(f"\nwrote {args.report}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
