"""Record the follow-up window's microphone audio to a fetchable WAV.

WHY THIS EXISTS (2026-09-02 23:37, add-on 0.7.8)

A follow-up window expired with this level line and no VAD event at all:

    🧽 follow-up cut-off → ... level peak=19926 (-4.3 dBFS) rms=1288 (-28.1 dBFS)
    loud=10000ms/10000ms profile(dBFS/s)=[-16,-15,-16,-15,-11,-16,-14,-13,-14,-4]
    first_loud=+0.0s

The mic feed sat at speech level from the very first frame for all ten seconds
and OpenAI's VAD never fired. Levels cannot tell us WHICH of the three possible
causes that is: the puck's own reply leaking/looping back into its mic, room
noise, or garbled PCM (wrong rate/endianness/interleave). The only way to
settle it is to LISTEN to what the add-on actually received.

There is no shell on the HA host, so the file has to arrive somewhere HA
already serves. Anything under HA's config `www/` directory is published at
`http://<ha>/local/<path>` with no extra configuration, which is why this
writes to `<config>/www/trixie-debug/followup-<timestamp>.wav` and logs the
`/local/...` URL to fetch it with.

Design notes:

* ARMED, not always-on. The wake turn is not interesting (it demonstrably
  works) and recording it would double the disk churn. The recorder is armed
  only at the boundary that precedes a follow-up window — the bot starting its
  reply — and disarmed by a wake or an interrupt, which start a fresh wake turn
  instead.
* No pipecat, no HA, no websockets: frames in, a WAV path out. That keeps it
  unit-testable with `tmp_path` and a couple of synthetic frames.
* OFF unless `debug_record_followup` is on. A disabled recorder buffers
  nothing and touches no filesystem.
"""

from __future__ import annotations

import logging
import math
import os
import time
import wave
from array import array
from pathlib import Path

logger = logging.getLogger(__name__)

# Candidate mount points for HA's config directory inside an add-on container.
# `map: - homeassistant_config:rw` mounts it at /homeassistant on current
# Supervisor builds; older builds (and the legacy `config:rw` mapping) use
# /config. Which one exists is decided at runtime rather than guessed, and the
# choice is logged once so a wrong mount is obvious in the add-on log.
CONFIG_DIR_CANDIDATES = ("/homeassistant", "/config")

# Sub-directory under `<config>/www`. Served at http://<ha>/local/trixie-debug/.
WWW_SUBDIR = "trixie-debug"

# Hard cap per file. A follow-up window is ~10 s; 30 s bounds a pathological
# one (or a device that never stops streaming) at ~1 MB of 16 kHz PCM16.
MAX_SECONDS = 30.0

# How many recordings to keep. `www/` is user-visible and never garbage
# collected by HA, so the recorder prunes its own directory.
KEEP_FILES = 6


def _dbfs(value: float) -> str:
    """int16-scale amplitude as dBFS, or '-inf' at (or below) zero."""
    if value <= 0:
        return "-inf"
    return f"{20.0 * math.log10(value / 32768.0):.1f}"


def resolve_config_dir(candidates=CONFIG_DIR_CANDIDATES) -> str | None:
    """First existing HA config mount, or None when nothing is mapped."""
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


class FollowUpWindowRecorder:
    """Buffers the mic frames of one follow-up window and writes them as a WAV.

    Lifecycle, driven from `app/websocket_handler.py`:

        arm()       ← the bot started replying; a follow-up window follows
        note_audio()← every device-rate PCM16 mono mic frame
        finalize()  ← the follow-up cut-off fired (commit branch or clear
                      branch); write the file, disarm, prune

        disarm()    ← a wake or an interrupt: this is a wake turn, not a
                      follow-up. Drop whatever was buffered, record nothing.
    """

    def __init__(
        self,
        enabled: bool = False,
        config_dir: str | None = None,
        max_seconds: float = MAX_SECONDS,
        keep: int = KEEP_FILES,
        clock=time.localtime,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_seconds = float(max_seconds)
        self.keep = int(keep)
        self.clock = clock
        self._config_dir = config_dir
        self._resolved = config_dir is not None
        self._armed = False
        self._chunks: list[bytes] = []
        self._sample_rate: int | None = None
        self._bytes = 0
        self._peak = 0

    # ------------------------------------------------------------- lifecycle
    def arm(self) -> None:
        """A bot reply started: the window that follows is worth recording."""
        if not self.enabled:
            return
        self._drop()
        self._armed = True

    def disarm(self) -> None:
        """A wake/interrupt boundary: not a follow-up window, keep nothing."""
        self._drop()

    def _drop(self) -> None:
        self._armed = False
        self._chunks = []
        self._sample_rate = None
        self._bytes = 0
        self._peak = 0

    @property
    def armed(self) -> bool:
        return self._armed

    # ----------------------------------------------------------------- input
    def note_audio(self, pcm: bytes | None, sample_rate: int) -> None:
        """One device-rate PCM16 mono mic frame."""
        if not self.enabled or not self._armed or not pcm or sample_rate <= 0:
            return
        if self._sample_rate is None:
            self._sample_rate = int(sample_rate)
        elif int(sample_rate) != self._sample_rate:
            # A rate change mid-window would splice two different clocks into
            # one WAV. Keep what we have and ignore the rest of the window.
            return
        limit = int(self.max_seconds * self._sample_rate) * 2
        if self._bytes >= limit:
            return
        room = limit - self._bytes
        if len(pcm) > room:
            pcm = pcm[:room]
        self._chunks.append(bytes(pcm))
        self._bytes += len(pcm)
        self._note_peak(pcm)

    def _note_peak(self, pcm: bytes) -> None:
        try:
            samples = array("h")
            samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        except ValueError:  # pragma: no cover - defensive
            return
        for sample in samples:
            magnitude = -sample if sample < 0 else sample
            if magnitude > self._peak:
                self._peak = magnitude

    # ---------------------------------------------------------------- output
    def finalize(self, reason: str = "") -> Path | None:
        """Write the buffered window to a WAV; return its path (or None)."""
        if not self.enabled or not self._armed:
            self._drop()
            return None
        chunks, sample_rate, peak = self._chunks, self._sample_rate, self._peak
        self._drop()
        if not chunks or not sample_rate:
            return None
        directory = self._recording_dir()
        if directory is None:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S", self.clock())
        path = directory / f"followup-{stamp}.wav"
        payload = b"".join(chunks)
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(payload)
        except OSError as e:
            logger.warning(f"⚠️ follow-up window recording failed ({e!r})")
            return None
        seconds = (len(payload) / 2.0) / sample_rate
        suffix = f" [{reason}]" if reason else ""
        logger.info(
            f"🎙️ follow-up window recorded → /local/{WWW_SUBDIR}/{path.name} "
            f"({seconds:.1f} s, peak {_dbfs(peak)} dBFS){suffix}"
        )
        self._prune(directory)
        return path

    def _recording_dir(self) -> Path | None:
        if not self._resolved:
            self._config_dir = resolve_config_dir()
            self._resolved = True
            if self._config_dir is None:
                logger.warning(
                    "⚠️ debug_record_followup is on but no Home Assistant config "
                    f"mount was found (looked for {', '.join(CONFIG_DIR_CANDIDATES)}) "
                    "— is `map: homeassistant_config:rw` present in config.yaml?"
                )
            else:
                logger.info(
                    f"🎙️ follow-up recordings → {self._config_dir}/www/{WWW_SUBDIR} "
                    f"(served at /local/{WWW_SUBDIR}/)"
                )
        if self._config_dir is None:
            return None
        directory = Path(self._config_dir) / "www" / WWW_SUBDIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"⚠️ cannot create {directory} ({e!r})")
            return None
        return directory

    def _prune(self, directory: Path) -> None:
        if self.keep <= 0:
            return
        try:
            files = sorted(directory.glob("followup-*.wav"))
        except OSError:  # pragma: no cover - defensive
            return
        for stale in files[: max(0, len(files) - self.keep)]:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - defensive
                pass
