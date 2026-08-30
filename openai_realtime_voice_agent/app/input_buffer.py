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
import os
import time
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

    # ----------------------------------------------------------------- inputs
    def note_audio(self, num_bytes: int, sample_rate: int) -> None:
        """One PCM16 mono frame arrived from the device."""
        if num_bytes <= 0 or sample_rate <= 0:
            return
        ms = (num_bytes / 2.0) / sample_rate * 1000.0
        self.audio_ms += ms
        if self.speaking:
            self.speech_ms += ms

    def note_speech_started(self) -> None:
        """Server VAD heard the user start. From here, audio counts as speech."""
        self.speaking = True

    def note_speech_stopped(self) -> None:
        """Server VAD heard the user stop — a server commit follows."""
        self.speaking = False
        self._last_speech_stop = self.clock()

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
                f"(was {self.speech_ms:.0f} ms speech / {self.audio_ms:.0f} ms audio)"
            )
        self.speech_ms = 0.0
        self.audio_ms = 0.0
        self.speaking = False
        self._last_speech_stop = None

    # ---------------------------------------------------------------- queries
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
        if audio_ms < MIN_COMMITTABLE_AUDIO_MS:
            return CutoffDecision(
                False, f"buffer effectively empty ({audio_ms:.0f} ms audio)",
                speech_ms, audio_ms)
        if self.speaking:
            return CutoffDecision(
                True, "user was still speaking at the cut-off", speech_ms, audio_ms)
        if speech_ms >= self.min_commit_ms:
            return CutoffDecision(
                True, f"{speech_ms:.0f} ms of speech uncommitted "
                      f"(>= {self.min_commit_ms:.0f} ms)",
                speech_ms, audio_ms)
        if self.speaking_recently():
            return CutoffDecision(
                True, "speech ended inside the grace window", speech_ms, audio_ms)
        return CutoffDecision(
            False, f"only {speech_ms:.0f} ms of speech (< {self.min_commit_ms:.0f} ms)",
            speech_ms, audio_ms)
