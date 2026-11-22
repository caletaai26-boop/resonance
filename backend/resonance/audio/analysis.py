"""Deterministic signal measurements. Every number reported by the Audio Forensics agent comes from here."""
from __future__ import annotations

import numpy as np
import pyloudnorm as pyln
from scipy import signal


def db(x: float, floor: float = -120.0) -> float:
    return float(max(floor, 20.0 * np.log10(max(x, 1e-12))))


def integrated_lufs(y: np.ndarray, sr: int) -> float | None:
    """ITU-R BS.1770 integrated loudness. None if the signal is too short/silent to measure."""
    if y.shape[0] < int(0.5 * sr):
        return None
    try:
        v = float(pyln.Meter(sr).integrated_loudness(y if y.ndim > 1 else y[:, None]))
    except Exception:
        return None
    return v if np.isfinite(v) else None


def true_peak_db(y: np.ndarray, sr: int, oversample: int = 4) -> float:
    """Inter-sample peak estimate by 4x polyphase oversampling (BS.1770 Annex 2 style)."""
    y2 = y if y.ndim > 1 else y[:, None]
    peak = 0.0
    for c in range(y2.shape[1]):
        up = signal.resample_poly(y2[:, c], oversample, 1)
        peak = max(peak, float(np.max(np.abs(up))) if up.size else 0.0)
    return db(peak)


def sample_peak_db(y: np.ndarray) -> float:
    return db(float(np.max(np.abs(y))) if y.size else 0.0)


def clipping(y: np.ndarray, sr: int, threshold: float = 0.985, min_run: int = 3) -> dict:
    """Runs of >= min_run consecutive samples at/above threshold are counted as clipped events."""
    m = np.max(np.abs(y), axis=1) if y.ndim > 1 else np.abs(y)
    hot = m >= threshold
    if not hot.any():
        return {"events": 0, "clipped_samples": 0, "times": []}
    d = np.diff(np.concatenate([[0], hot.astype(np.int8), [0]]))
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    runs = [(s, e) for s, e in zip(starts, ends) if e - s >= min_run]
    return {
        "events": len(runs),
        "clipped_samples": int(sum(e - s for s, e in runs)),
        "times": [round(s / sr, 3) for s, _ in runs[:200]],
    }


def frame_rms_db(mono: np.ndarray, sr: int, frame_s: float = 0.05) -> tuple[np.ndarray, float]:
    n = max(1, int(frame_s * sr))
    k = len(mono) // n
    if k == 0:
        return np.array([db(float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0)]), frame_s
    fr = mono[: k * n].reshape(k, n)
    rms = np.sqrt(np.mean(fr**2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-9)), frame_s


def noise_floor(mono: np.ndarray, sr: int) -> dict:
    """Noise floor = 5th percentile of 50 ms RMS frames; 'programme level' = 90th percentile.
    Their difference is a rough SNR *estimate* (music never stops in most live recordings, so the
    floor is biased upward – reported as an estimate, not a measurement)."""
    rdb, _ = frame_rms_db(mono, sr)
    floor, prog = float(np.percentile(rdb, 5)), float(np.percentile(rdb, 90))
    return {"noise_floor_db": round(floor, 1), "programme_level_db": round(prog, 1), "snr_estimate_db": round(prog - floor, 1)}


def silent_regions(mono: np.ndarray, sr: int, thresh_db: float = -50.0, min_len: float = 0.4) -> list[dict]:
    rdb, fs = frame_rms_db(mono, sr)
    quiet = rdb < thresh_db
    out, start = [], None
    for i, q in enumerate(np.append(quiet, False)):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * fs >= min_len:
                out.append({"start": round(start * fs, 2), "end": round(i * fs, 2)})
            start = None
    return out


def loudness_profile(mono: np.ndarray, sr: int, window_s: float = 3.0, hop_s: float = 1.0) -> dict:
    """Short-term level over time, used to flag uneven volume."""
    rdb, fs = frame_rms_db(mono, sr)
    w, h = max(1, int(window_s / fs)), max(1, int(hop_s / fs))
    lin = 10 ** (rdb / 10)
    vals, times = [], []
    for i in range(0, max(1, len(lin) - w + 1), h):
        vals.append(float(10 * np.log10(np.mean(lin[i : i + w]) + 1e-12)))
        times.append(round(i * fs, 2))
    v = np.array(vals)
    active = v[v > (np.max(v) - 40)] if v.size else v
    return {
        "times": times,
        "level_db": [round(x, 1) for x in vals],
        "range_db": round(float(np.percentile(active, 95) - np.percentile(active, 5)), 1) if active.size > 2 else 0.0,
    }


def spectral_summary(mono: np.ndarray, sr: int) -> dict:
    f, pxx = signal.welch(mono, sr, nperseg=min(8192, max(256, len(mono))))
    total = float(np.sum(pxx)) + 1e-20
    c = np.cumsum(pxx) / total

    def band(lo: float, hi: float) -> float:
        return round(10 * np.log10(float(np.sum(pxx[(f >= lo) & (f < hi)])) / total + 1e-12), 1)

    return {
        "rolloff_95_hz": round(float(f[min(np.searchsorted(c, 0.95), len(f) - 1)]), 0),
        "rolloff_99_hz": round(float(f[min(np.searchsorted(c, 0.99), len(f) - 1)]), 0),
        "centroid_hz": round(float(np.sum(f * pxx) / total), 0),
        "band_energy_db": {
            "sub_80": band(0, 80), "low_80_250": band(80, 250), "lowmid_250_1k": band(250, 1000),
            "mid_1k_4k": band(1000, 4000), "high_4k_10k": band(4000, 10000), "air_10k_up": band(10000, sr / 2 + 1),
        },
    }


def stereo_summary(y: np.ndarray) -> dict:
    if y.ndim < 2 or y.shape[1] < 2:
        return {"is_stereo": False, "correlation": None, "side_to_mid_db": None}
    l, r = y[:, 0], y[:, 1]
    denom = float(np.sqrt(np.sum(l**2) * np.sum(r**2))) + 1e-12
    corr = float(np.sum(l * r) / denom)
    mid, side = (l + r) / 2, (l - r) / 2
    s2m = db(float(np.sqrt(np.mean(side**2)))) - db(float(np.sqrt(np.mean(mid**2))))
    return {"is_stereo": True, "correlation": round(corr, 3), "side_to_mid_db": round(s2m, 1),
            "effectively_mono": bool(corr > 0.995)}


def waveform_peaks(mono: np.ndarray, n: int = 2000) -> list[float]:
    if mono.size == 0:
        return []
    k = max(1, len(mono) // n)
    m = len(mono) // k
    return [round(float(v), 4) for v in np.max(np.abs(mono[: m * k].reshape(m, k)), axis=1)]


def spectrogram_png(mono: np.ndarray, sr: int, path, width: int = 1600, height: int = 360) -> None:
    """Log-frequency magnitude spectrogram rendered with a copper/ivory colour ramp."""
    import librosa
    from PIL import Image

    hop = max(256, int(len(mono) / width))
    S = np.abs(librosa.stft(mono, n_fft=2048, hop_length=hop))
    M = librosa.feature.melspectrogram(S=S**2, sr=sr, n_mels=height // 2, fmax=min(12000, sr / 2))
    D = librosa.power_to_db(M, ref=np.max, top_db=80)
    z = np.clip((D + 80) / 80, 0, 1)[::-1]
    stops = np.array([[11, 13, 18], [43, 31, 30], [140, 78, 46], [205, 143, 92], [242, 232, 213]], dtype=np.float32)
    pos = z * (len(stops) - 1)
    i0 = np.clip(pos.astype(int), 0, len(stops) - 2)
    t = (pos - i0)[..., None]
    rgb = (stops[i0] * (1 - t) + stops[i0 + 1] * t).astype(np.uint8)
    Image.fromarray(rgb).resize((width, height), Image.BILINEAR).save(str(path))
