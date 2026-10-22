import math
import struct
import wave
from pathlib import Path

from decision_helper.transcribe import decode_audio


def test_decoder_resamples_audio_for_whisper(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(
            b"".join(struct.pack("<hh", int(8000 * math.sin(i / 20)), 0) for i in range(48000))
        )

    samples = decode_audio(path)

    assert samples.dtype.name == "float32"
    assert abs(len(samples) - 16000) < 100
    assert 0.05 < float(abs(samples).max()) < 1.0
