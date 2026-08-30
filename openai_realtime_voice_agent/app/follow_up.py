"""What to do when the device's follow-up window expires.

Its own module, not a closure inside `WebSocketHandler.build_pipeline`, for one
reason: the 2026-08-30 13:49:01 data-loss bug (an expiring window discarding the
user's half-spoken question) lived in a closure and therefore had no test. This
imports only the OpenAI Realtime event models, so it is unit-testable with a
mocked service.
"""

import logging

from pipecat.services.openai.realtime import events as openai_rt_events

logger = logging.getLogger(__name__)


async def handle_follow_up_cutoff(openai_service, input_buffer, phase_emitter) -> bool:
    """Commit-or-clear on `{"type":"flush"}` from the device.

    Two very different situations arrive on that one message, and add-on 0.7.0
    answered both with a clear:

      (a) the window closed on SILENCE, or on a stale half segment a later wake
          would otherwise "complete" into a garbage answer. Clear is right.
      (b) the user was MID-UTTERANCE when the window closed — observed live
          2026-08-30 13:49:01: the server VAD had not committed, the clear threw
          Mitch's question away, he got no answer at all, and the puck's ring
          kept circling. Here the honest reading of a closing window is "the
          user finished talking": commit the buffer and ask for a response,
          exactly as an end-of-turn would have.

    `InputBufferTracker` tells the two apart by SPEECH milliseconds since the
    last commit — silence does not count, because the mic streams for the whole
    window. Returns True when the buffer was committed, False when cleared.
    """
    decision = input_buffer.decide_cutoff()
    if decision.commit:
        try:
            await openai_service.send_client_event(
                openai_rt_events.InputAudioBufferCommitEvent())
            await openai_service.send_client_event(
                openai_rt_events.ResponseCreateEvent())
            input_buffer.note_commit("follow-up cut-off")
            logger.info(
                "🗣️ follow-up cut-off → input_audio_buffer.commit + "
                f"response.create ({decision.reason}) — treating expiry "
                "as end-of-utterance"
            )
            # NB: deliberately NO phase_emitter.note_wake() here. A turn is now
            # in flight, and the server-VAD stop that follows our commit is NOT
            # dangling — marking a wake boundary would make the dangling-VAD
            # guard suppress the phase and cancel the very response we just
            # asked for, which is the same silence we are fixing.
            return True
        except Exception as e:
            logger.warning(
                f"⚠️ follow-up cut-off commit failed ({e!r}) — clearing instead")
    # A turn boundary for the dangling-VAD guard: the follow-up closed without
    # speech, so any later server-VAD stop is a stale segment closing late.
    phase_emitter.note_wake()
    try:
        await openai_service.send_client_event(
            openai_rt_events.InputAudioBufferClearEvent())
        input_buffer.note_clear("follow-up cut-off")
        logger.info(
            "🧽 follow-up cut-off → input_audio_buffer.clear "
            f"(drop partial utterance; {decision.reason})"
        )
    except Exception as e:
        logger.debug(f"🧽 mic-flush input clear no-op ({e!r})")
    return False


class FollowUpChain:
    """How many follow-ups a single wake may chain before the device goes quiet.

    The 2026-08-30 21:xx live failure: the add-on reopens the follow-up window
    after EVERY assistant reply, so any answered utterance reopened the mic,
    ambient speech in the room kept getting answered, and the conversation never
    ended — 2+ minutes of continuously billed audio with the ring never going
    dark. Nothing in the loop was wrong individually; there was simply no
    terminating condition other than a silent window.

    This is that terminating condition: count CONSECUTIVE completed assistant
    turns since the last device wake. When the count reaches `max_turns`, the
    window is not reopened after that reply and the device falls back to
    wake-word-only. `max_turns = 0` means unlimited (the pre-0.7.2 behaviour).

    Deliberately a plain object with no I/O: the PhaseEmitter owns an instance
    and consults it at the one moment that matters (the debounced end-of-reply
    `idle`), which keeps this unit-testable without a pipeline.
    """

    def __init__(self, max_turns: int = 0) -> None:
        self.max_turns = max(0, int(max_turns))
        self._turns = 0

    @property
    def turns(self) -> int:
        """Completed assistant turns since the last wake."""
        return self._turns

    def note_wake(self) -> None:
        """A device wake (or a follow-up window that closed without speech)
        starts a fresh chain."""
        self._turns = 0

    def note_assistant_turn_complete(self) -> bool:
        """Count one completed assistant turn.

        Returns True if the follow-up window may reopen after this reply,
        False if the chain cap has been reached and the device should go back
        to wake-word-only.
        """
        self._turns += 1
        if self.max_turns == 0:
            return True
        return self._turns < self.max_turns
