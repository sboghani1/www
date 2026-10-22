from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000


def decode_audio(path: Path) -> Any:
    """Decode to 16 kHz mono float32.

    faster-whisper's own decoder passes av.open(metadata_errors=...), which
    current PyAV releases rejected, so decode with the stable PyAV API here.
    """
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    with av.open(str(path), mode="r") as container:
        frames = container.decode(audio=0)
        while True:
            try:
                frame = next(frames)
            except StopIteration:
                break
            except av.error.InvalidDataError:
                continue
            frame.pts = None
            chunks.extend(resampled.to_ndarray().reshape(-1) for resampled in resampler.resample(frame))
        chunks.extend(resampled.to_ndarray().reshape(-1) for resampled in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class WhisperTranscriber:
    def __init__(self, model_name: str, model_dir: Path) -> None:
        self.model_name = model_name
        self.model_dir = model_dir
        self._model: Any = None
        self._lock = threading.Lock()

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self.model_dir.mkdir(parents=True, exist_ok=True)
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.model_dir),
                )
        return self._model

    def warm(self) -> None:
        try:
            self._load_model()
        except Exception:
            # Transcription reports the real error to the user later.
            pass

    def transcribe(self, path: Path) -> str:
        model = self._load_model()
        segments, _ = model.transcribe(
            decode_audio(path),
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
        )
        transcript = " ".join(
            str(segment.text).strip()
            for segment in segments
            if str(segment.text).strip()
        )
        if not transcript:
            raise RuntimeError("No speech was detected in the audio")
        return transcript
