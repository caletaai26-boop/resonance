"""Decoding / encoding through FFmpeg. The original upload is never modified; we decode to a WAV working copy."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf


class AudioDecodeError(Exception):
    pass


def ffprobe(path: Path) -> dict:
    """Container/stream facts straight from ffprobe. Raises AudioDecodeError if no audio stream."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as e:  # pragma: no cover
        raise AudioDecodeError("ffprobe timed out") from e
    if out.returncode != 0:
        raise AudioDecodeError(out.stderr.strip()[:400] or "ffprobe could not read the file")
    info = json.loads(out.stdout or "{}")
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise AudioDecodeError("no audio stream found in file")
    s, fmt = streams[0], info.get("format", {})
    return {
        "codec": s.get("codec_name"),
        "sample_rate": int(s.get("sample_rate", 0) or 0),
        "channels": int(s.get("channels", 0) or 0),
        "bit_rate": int(s.get("bit_rate") or fmt.get("bit_rate") or 0),
        "duration": float(s.get("duration") or fmt.get("duration") or 0.0),
        "container": fmt.get("format_name"),
        "bits_per_sample": int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0),
    }


def decode_to_wav(src: Path, dst: Path, sr: int | None = None, mono: bool = False) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn"]
    if sr:
        cmd += ["-ar", str(sr)]
    if mono:
        cmd += ["-ac", "1"]
    cmd += ["-c:a", "pcm_f32le", str(dst)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if out.returncode != 0 or not dst.exists():
        raise AudioDecodeError(out.stderr.strip()[:400] or "ffmpeg failed to decode the file")


def load(path: Path) -> tuple[np.ndarray, int]:
    """Returns (samples[n, channels] float32, sample_rate)."""
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y, sr


def load_mono(path: Path, sr: int | None = None) -> tuple[np.ndarray, int]:
    y, file_sr = load(path)
    m = y.mean(axis=1)
    if sr and sr != file_sr:
        import librosa

        m = librosa.resample(m, orig_sr=file_sr, target_sr=sr)
        file_sr = sr
    return m.astype(np.float32), file_sr


def save_wav(path: Path, y: np.ndarray, sr: int, subtype: str = "PCM_24") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), y, sr, subtype=subtype)


def encode_mp3(src_wav: Path, dst: Path, bitrate: str = "320k") -> None:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src_wav), "-c:a", "libmp3lame", "-b:a", bitrate, str(dst)],
        capture_output=True, text=True, timeout=1800,
    )
    if out.returncode != 0 or not dst.exists():
        raise AudioDecodeError(out.stderr.strip()[:400] or "ffmpeg failed to encode MP3")
