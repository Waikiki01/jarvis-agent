"""Offline speech-to-text with faster-whisper (optional: pip install -e ".[voice]").

The browser records push-to-talk audio (webm/opus via MediaRecorder) and POSTs it to
/transcribe. Decoding (PyAV, bundled with faster-whisper) and recognition run on this
machine, so no audio leaves it. Without the package, the UI falls back to the browser's
Web Speech API (which, in Chrome, sends audio to Google).
"""
import io
import threading
from functools import lru_cache

from .config import get_settings

MAX_AUDIO_BYTES = 10 * 1024 * 1024  # ~10 min of opus; push-to-talk clips are a few KB
_lock = threading.Lock()            # one transcription at a time on CPU


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return get_settings().stt_model != ""


@lru_cache
def _model():
    from faster_whisper import WhisperModel

    s = get_settings()
    # int8 on CPU: ~4x smaller and faster than float32 with almost the same accuracy.
    return WhisperModel(s.stt_model, device=s.stt_device, compute_type="int8")


def decode(audio: bytes, rate: int = 16000):
    """Any container/codec PyAV reads (webm/opus, ogg, wav, mp3...) -> mono float32 at 16 kHz.

    Done here rather than by faster-whisper: its decoder passes an argument that newer
    PyAV releases removed (TypeError: unexpected keyword 'metadata_errors')."""
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
    parts = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            parts.append(out.to_ndarray().reshape(-1))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32) / 32768.0


def transcribe(audio: bytes) -> str:
    s = get_settings()
    samples = decode(audio)
    if samples.size < 1600:  # < 0.1 s
        return ""
    with _lock:
        segments, _info = _model().transcribe(
            samples, language=s.stt_language or None, beam_size=1,
            vad_filter=True,  # drop silence: avoids Whisper "hallucinating" text on empty audio
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
