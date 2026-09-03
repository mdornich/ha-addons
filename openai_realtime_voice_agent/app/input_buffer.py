"""How much of the user is sitting uncommitted in OpenAI's input buffer.

WHY THIS EXISTS (2026-08-30, in the room, add-on 0.7.0)

The device owns the follow-up window: it opens the mic after a reply and, when
its timer runs out, sends `{"type":"flush"}`. The add-on's handler answered that
by sending `input_audio_buffer.clear` unconditionally:

    13:49:01  🧽 follow-up cut-off → input_audio_buffer.clear (drop partial utterance)

Mitch was mid-question when the window closed. Semantic VAD had not committed
yet, so his words were still in the input buffer — and the clear threw them
away. He got no answer at all while the puck's ring kept circling. The clear is
right for an EMPTY buffer (a window that closed on silence; a stale half segment
that a later wake would otherwise "complete" into a garbage answer) and wrong
for a buffer holding a real utterance, where the honest reading of a closing
window is "the user finished talking" — i.e. commit and answer.

Distinguishing the two needs a count the handler did not have, which is what
this tracker keeps: milliseconds of SPEECH appended since the last commit or
clear.

"Speech", not "audio", is the load-bearing part. The mic streams for the whole
window, so audio-since-commit would read ~20 s of silence and every expiry would
commit garbage. Only the audio arriving between the server VAD's speech_started
and speech_stopped counts, so silence contributes nothing and the "effectively
empty" case still clears. `audio_ms` is kept separately as a floor check
(OpenAI rejects a commit on a buffer under ~100 ms) and for the log line.

The tracker is pure and clock-injectable: no pipecat, no websockets, no OpenAI.
"""

from __future__ import annotations

import logging
import math
import os
import time
from array import array
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How much speech has to be sitting in the buffer before a follow-up cut-off is
# treated as end-of-utterance instead of a discard. ~600 ms is about two spoken
# syllables: below it there is nothing an answer could be built from, above it
# the user said something real and deserves a reply rather than silence.
DEFAULT_MIN_COMMIT_MS = 600.0

# OpenAI rejects `input_audio_buffer.commit` on a buffer holding less than
# ~100 ms of audio ("buffer too small"), which surfaces as an error event and
# no reply. Never send a commit under this, whatever the speech count says.
MIN_COMMITTABLE_AUDIO_MS = 100.0

# Frame RMS (int16 scale) at or above which a frame is counted as "speech-like
# audio". 500 is about -36 dBFS: comfortably above a quiet room's noise floor
# and far below normal speech into the puck's mic. This counter is deliberately
# INDEPENDENT of OpenAI's server VAD — the whole point of the 2026-09-01 21:04
# diagnostic is to tell "the puck streamed near-silence" apart from "the puck
# streamed real speech and OpenAI's VAD ignored it".
LOUD_FRAME_RMS = 500.0


def _dbfs(value: float) -> str:
    """int16-scale amplitude as dBFS, or '-inf' at (or below) zero."""
    if value <= 0:
        return "-inf"
    return f"{20.0 * math.log10(value / 32768.0):.1f}"


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


@dataclass
class CutoffDecision:
    """What the follow-up cut-off should do, and why (the why goes in the log)."""

    commit: bool
    reason: str
    speech_ms: float
    audio_ms: float


@dataclass
class InputBufferTracker:
    """Speech and audio appended to OpenAI's input buffer since the last commit.

    Counters are advanced by the pipeline (audio frames) and by the server VAD
    (speech start/stop), and reset by every event that empties the buffer:
    a commit (ours or the server's, which follows speech_stopped), a clear, and
    a fresh wake.
    """

    min_commit_ms: float = field(default_factory=lambda: _env_float(
        "FOLLOW_UP_COMMIT_MIN_SPEECH_MS", DEFAULT_MIN_COMMIT_MS))
    # A cut-off that lands within this long after speech_stopped is still the
    # tail of a real utterance: the server's own commit and our flush handler can
    # race, and losing that race must not cost the user the turn.
    speech_grace_s: float = field(default_factory=lambda: _env_float(
        "FOLLOW_UP_SPEECH_GRACE_MS", 1500.0) / 1000.0)
    clock: object = time.monotonic

    speech_ms: float = 0.0
    audio_ms: float = 0.0
    speaking: bool = False
    _last_speech_stop: float | None = None

    # Level accounting (2026-09-01): peak/RMS/loud-ms over everything appended
    # since the last commit or clear.
    peak: int = 0
    sum_sq: float = 0.0
    n_samples: int = 0
    loud_ms: float = 0.0

    # ----------------------------------------------------------------- inputs
    def note_audio(
        self, num_bytes: int, sample_rate: int, pcm: bytes | None = None
    ) -> None:
        """One PCM16 mono frame arrived from the device.

        `pcm` is the frame's raw bytes. When given, the frame's level is folded
        into the peak/RMS/loud-ms counters. Frames are 20-60 ms, so an
        `array('h')` pass is a few hundred samples — cheap enough per frame and
        free of a numpy dependency.
        """
        if num_bytes <= 0 or sample_rate <= 0:
            return
        ms = (num_bytes / 2.0) / sample_rate * 1000.0
        self.audio_ms += ms
        if self.speaking:
            self.speech_ms += ms
        if pcm:
            self._note_level(pcm, ms)

    def _note_level(self, pcm: bytes, ms: float) -> None:
        try:
            samples = array("h")
            samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        except ValueError:  # pragma: no cover - defensive
            return
        if not samples:
            return
        frame_peak = 0
        frame_sum_sq = 0.0
        for sample in samples:
            magnitude = -sample if sample < 0 else sample
            if magnitude > frame_peak:
                frame_peak = magnitude
            frame_sum_sq += float(sample) * float(sample)
        if frame_peak > self.peak:
            self.peak = frame_peak
        self.sum_sq += frame_sum_sq
        self.n_samples += len(samples)
        if math.sqrt(frame_sum_sq / len(samples)) >= LOUD_FRAME_RMS:
            self.loud_ms += ms

    def note_speech_started(self) -> None:
        """Server VAD heard the user start. From here, audio counts as speech."""
        self.speaking = True
        logger.info(f"🎚️ VAD speech_started — level so far: {self.level_summary()}")

    def note_speech_stopped(self) -> None:
        """Server VAD heard the user stop — a server commit follows."""
        self.speaking = False
        self._last_speech_stop = self.clock()

    def note_bot_started(self) -> None:
        """The bot began replying: the user's turn is over and the buffer empty.

        WHY (2026-09-02 21:38, add-on 0.7.6). A wake-word turn ran to completion
        — transcript, house call, "Paused." — but the tracker was left with
        `speaking = True` for the whole following follow-up window, because the
        UserStartedSpeaking that set it was pipecat's EMULATED frame (synthesised
        from the transcription, not a server VAD event) and its matching
        UserStoppedSpeaking was swallowed by the PhaseEmitter's stale-VAD-tail
        suppression, so `note_speech_stopped` never ran:

            21:38:20,693  🎚️ VAD speech_started
            21:38:21,506  📞 'thinking' suppressed — bot is replying (stale VAD tail)

        Ten seconds of an empty room later, `decide_cutoff` still read "user was
        still speaking at the cut-off", committed an empty buffer, and the bot
        said "Goodbye, Mitch." to nobody.

        This is a FULL `_reset`, not just a `speaking = False`. Once the bot is
        replying, the server has already committed (or the turn was cancelled)
        and — with barge_in false — the mic is gated, so nothing appended before
        this moment is still sitting in OpenAI's buffer. Keeping the ms/level
        counters would be strictly worse than resetting them: `decide_cutoff`'s
        second branch commits on `speech_ms >= min_commit_ms`, so a retained
        pre-reply speech count would produce exactly the same empty commit by a
        different route. Everything appended AFTER this call is what the next
        cut-off decision should be made from, which is what a reset gives.
        """
        self._reset("bot reply started")

    def note_commit(self, reason: str = "") -> None:
        """The buffer was committed (by the server VAD or by us): it is empty."""
        self._reset(f"commit ({reason})" if reason else "commit")

    def note_clear(self, reason: str = "") -> None:
        """The buffer was cleared: it is empty."""
        self._reset(f"clear ({reason})" if reason else "clear")

    def _reset(self, why: str) -> None:
        if self.speech_ms or self.audio_ms:
            logger.debug(
                f"📏 input buffer reset on {why} "
                f"(was {self.speech_ms:.0f} ms speech / {self.audio_ms:.0f} ms audio; "
                f"level {self.level_summary()})"
            )
        self.speech_ms = 0.0
        self.audio_ms = 0.0
        self.speaking = False
        self._last_speech_stop = None
        self.peak = 0
        self.sum_sq = 0.0
        self.n_samples = 0
        self.loud_ms = 0.0

    # ---------------------------------------------------------------- queries
    def rms(self) -> float:
        """int16-scale RMS over everything appended since the last reset."""
        if self.n_samples <= 0:
            return 0.0
        return math.sqrt(self.sum_sq / self.n_samples)

    def level_summary(self) -> str:
        """Human-readable level line for the logs.

        e.g. the cut-off log tail `level peak=1234 (-28.5 dBFS) rms=210
        (-43.9 dBFS) loud=840ms/10000ms`
        """
        rms = self.rms()
        return (
            f"peak={self.peak} ({_dbfs(self.peak)} dBFS) "
            f"rms={rms:.0f} ({_dbfs(rms)} dBFS) "
            f"loud={self.loud_ms:.0f}ms/{self.audio_ms:.0f}ms"
        )

    def speaking_recently(self) -> bool:
        """Mid-utterance, or within the grace window after the VAD's stop."""
        if self.speaking:
            return True
        if self._last_speech_stop is None:
            return False
        return (self.clock() - self._last_speech_stop) < self.speech_grace_s

    def decide_cutoff(self) -> CutoffDecision:
        """Commit-or-clear for a follow-up window that just expired."""
        speech_ms, audio_ms = self.speech_ms, self.audio_ms
        # Every branch carries the level summary: when the VAD never fired, the
        # only way to tell a muted/ducked mic apart from a VAD miss is the level
        # of what the puck actually streamed (2026-09-01 21:04).
        level = f"; level {self.level_summary()}"

        def decide(commit: bool, reason: str) -> CutoffDecision:
            return CutoffDecision(commit, reason + level, speech_ms, audio_ms)

        if audio_ms < MIN_COMMITTABLE_AUDIO_MS:
            return decide(False, f"buffer effectively empty ({audio_ms:.0f} ms audio)")
        if self.speaking:
            return decide(True, "user was still speaking at the cut-off")
        if speech_ms >= self.min_commit_ms:
            return decide(
                True,
                f"{speech_ms:.0f} ms of speech uncommitted "
                f"(>= {self.min_commit_ms:.0f} ms)",
            )
        if self.speaking_recently():
            return decide(True, "speech ended inside the grace window")
        return decide(
            False,
            f"only {speech_ms:.0f} ms of speech (< {self.min_commit_ms:.0f} ms)",
        )
