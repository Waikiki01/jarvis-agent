"""Offline speech-to-text: decoding and the /transcribe endpoint (no Whisper model needed)."""
import io
import math
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from jarvis import stt
from jarvis.agents import fixed_router
from jarvis.api import create_app
from tests.test_agent import fake


def tone_wav(seconds=0.5, rate=44100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate)))
        w.writeframes(b"".join(struct.pack("<hh", s, s) for s in frames))
    return buf.getvalue()


def test_decode_resamples_to_16k_mono_float():
    pytest.importorskip("av")  # installed with the [voice] extra; CI runs without it
    samples = stt.decode(tone_wav(0.5))
    assert samples.dtype.name == "float32"
    assert abs(samples.size - 8000) < 200          # 0.5 s at 16 kHz, stereo folded to mono
    assert 0.1 < float(abs(samples).max()) <= 1.0


@pytest.fixture
def client(monkeypatch):
    def make(available: bool, text: str = "hello jarvis"):
        monkeypatch.setattr(stt, "available", lambda: available)
        monkeypatch.setattr(stt, "transcribe", lambda audio: text)
        return TestClient(create_app(model=fake(), router=fixed_router("assistant")))
    return make


def test_transcribe_endpoint(client):
    with client(True) as c:
        assert c.get("/health").json()["stt"] == "whisper"
        assert c.post("/transcribe", content=tone_wav()).json() == {"text": "hello jarvis"}
        assert c.post("/transcribe", content=b"").status_code == 400
        assert c.post("/transcribe", content=b"x" * (stt.MAX_AUDIO_BYTES + 1)).status_code == 413


def test_transcribe_without_whisper_falls_back_to_browser(client):
    with client(False) as c:
        assert c.get("/health").json()["stt"] == "browser"
        assert c.post("/transcribe", content=tone_wav()).status_code == 501
