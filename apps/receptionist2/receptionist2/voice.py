"""Local voice-note transcription, adapted from Mel's decision_helper.transcribe.

Run as `python -m receptionist2.voice <audio> <model> <model-dir>` in a
short-lived subprocess so the gateway never holds the Whisper model in memory.
It prints one JSON object: {"transcript": ...} or {"error": ...}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000
TRANSCRIPT_PREVIEW_CHARS = 3300


def voice_prompt(transcript: str) -> str:
    return (
        "[Voice note from the user, transcribed locally with Whisper. It may "
        "contain transcription errors, especially in names and numbers.]\n\n"
        f"{transcript}"
    )


def transcript_preview(transcript: str) -> str:
    if len(transcript) <= TRANSCRIPT_PREVIEW_CHARS:
        return transcript
    return (
        transcript[:TRANSCRIPT_PREVIEW_CHARS].rsplit(" ", 1)[0]
        + " … (shortened here; Run sends the full transcript)"
    )


def decode_audio(path: Path) -> Any:
    """Decode to 16 kHz mono float32 with PyAV.

    faster-whisper's own decoder passes av.open(metadata_errors=...), which
    current PyAV releases reject.
    """
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=SAMPLE_RATE
    )
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
            chunks.extend(
                resampled.to_ndarray().reshape(-1)
                for resampled in resampler.resample(frame)
            )
        chunks.extend(
            resampled.to_ndarray().reshape(-1)
            for resampled in resampler.resample(None)
        )
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe(path: Path, model_name: str, model_dir: Path) -> str:
    from faster_whisper import WhisperModel

    model_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        download_root=str(model_dir),
    )
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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print(json.dumps({"error": "usage: voice <audio> <model> <model-dir>"}))
        return 2
    try:
        transcript = transcribe(Path(args[0]), args[1], Path(args[2]))
    except Exception as error:  # reported to the user verbatim
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps({"transcript": transcript}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
