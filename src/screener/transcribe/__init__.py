"""Speech into text, over HTTP.

Whisper on CPU, in a container of its own. `screener.transcribe.server` is that
container; everything else in this repository asks it for a transcript through
the client here, which is `httpx` and nothing more.

Only the client is re-exported. `serve` and `build_server` deliberately are not:
importing them is one import away from `faster_whisper`, and the two processes
that actually want a transcript — the gateway bot and the status service — must
not pay for ctranslate2, onnxruntime and PyAV to ask for one. Reach into
`screener.transcribe.server` if you are the service, and nothing else should be.

A transcript carries `Utterance` timings — where each phrase falls in the
audio — because Whisper separates them anyway and a two hour stream is unusable
without them. `text` is still the join of all of them, so a caller that only
wants the words never sees the difference.

Not WhisperX, which is this plus wav2vec2 forced alignment and pyannote
diarization. Those give *word* level timestamps and speaker labels, neither of
which means anything for one person talking into their phone, and both of which
need torch.
"""

from screener.transcribe.client import (
    MAX_AUDIO_BYTES,
    MAX_SECONDS,
    Transcript,
    Utterance,
    transcribe,
    transcriber_url,
)

__all__ = [
    "MAX_AUDIO_BYTES",
    "MAX_SECONDS",
    "Transcript",
    "Utterance",
    "transcribe",
    "transcriber_url",
]
