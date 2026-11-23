"""Real DSP used by the Restore, Mixing and Mastering agents. All functions take/return float arrays
shaped [n, channels] unless stated otherwise."""
from __future__ import annotations

import numpy as np
from scipy import signal

from .analysis import integrated_lufs, true_peak_db


def to_stereo(y: np.ndarray) -> np.ndarray:
    if y.ndim == 1:
        y = y[:, None]
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    return y[:, :2].astype(np.float64)


# ---------- filters -------------------------------------------------------------------------------
def _biquad(kind: str, f0: float, sr: int, gain_db: float = 0.0, q: float = 0.707) -> tuple[np.ndarray, np.ndarray]:
    """RBJ Audio EQ Cookbook biquads."""
    f0 = float(np.clip(f0, 10.0, sr / 2 - 100))
    A = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    cw, sw = np.cos(w), np.sin(w)
    alpha = sw / (2 * q)
    if kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]; a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * A, -2 * cw, 1 - alpha * A]; a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
    elif kind == "lowshelf":
        s = 2 * np.sqrt(A) * alpha
        b = [A * ((A + 1) - (A - 1) * cw + s), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - s)]
        a = [(A + 1) + (A - 1) * cw + s, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - s]
    elif kind == "highshelf":
        s = 2 * np.sqrt(A) * alpha
        b = [A * ((A + 1) + (A - 1) * cw + s), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - s)]
        a = [(A + 1) - (A - 1) * cw + s, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - s]
    else:
        raise ValueError(kind)
    b, a = np.array(b), np.array(a)
    return b / a[0], a / a[0]


def eq(y: np.ndarray, sr: int, bands: list[dict]) -> np.ndarray:
    """bands: [{type: highpass|lowpass|peak|lowshelf|highshelf, freq, gain_db?, q?}]"""
    out = y.astype(np.float64)
    for b in bands or []:
        if b.get("type") in ("peak", "lowshelf", "highshelf") and abs(float(b.get("gain_db", 0))) < 0.01:
            continue
        bb, aa = _biquad(b["type"], float(b["freq"]), sr, float(b.get("gain_db", 0)), float(b.get("q", 0.707)))
        out = signal.lfilter(bb, aa, out, axis=0)
    return out


# ---------- dynamics ------------------------------------------------------------------------------
def _envelope(level: np.ndarray, sr: int, attack_ms: float, release_ms: float) -> np.ndarray:
    """One-pole attack/release follower, block-vectorised (64-sample blocks) for speed."""
    blk = 64
    n = len(level) // blk
    if n == 0:
        return level.copy()
    peaks = level[: n * blk].reshape(n, blk).max(axis=1)
    ca = np.exp(-1.0 / (max(attack_ms, 0.01) * 1e-3 * sr / blk))
    cr = np.exp(-1.0 / (max(release_ms, 0.01) * 1e-3 * sr / blk))
    env = np.empty(n)
    e = 0.0
    for i, p in enumerate(peaks):
        c = ca if p > e else cr
        e = c * e + (1 - c) * p
        env[i] = e
    full = np.repeat(env, blk)
    return np.concatenate([full, np.full(len(level) - len(full), full[-1] if len(full) else 0.0)])


def compress(y: np.ndarray, sr: int, threshold_db: float = -20.0, ratio: float = 2.0,
             attack_ms: float = 20.0, release_ms: float = 250.0, makeup_db: float = 0.0) -> tuple[np.ndarray, float]:
    """Feed-forward stereo-linked compressor. Returns (audio, max gain reduction in dB)."""
    if ratio <= 1.0:
        return y * 10 ** (makeup_db / 20), 0.0
    env = _envelope(np.max(np.abs(y), axis=1), sr, attack_ms, release_ms)
    lvl = 20 * np.log10(np.maximum(env, 1e-9))
    over = np.maximum(lvl - threshold_db, 0.0)
    gr = -over * (1 - 1 / ratio)
    return y * (10 ** ((gr + makeup_db) / 20))[:, None], float(-gr.min()) if gr.size else 0.0


def limit(y: np.ndarray, sr: int, ceiling_db: float = -1.0, lookahead_ms: float = 5.0, release_ms: float = 80.0) -> np.ndarray:
    """Look-ahead brickwall limiter measured on a 4x-oversampled peak estimate, then a final safety trim so
    the true peak is guaranteed at or under the ceiling."""
    ceil = 10 ** (ceiling_db / 20)
    up = signal.resample_poly(y, 4, 1, axis=0)
    pk4 = np.max(np.abs(up), axis=1)
    pk = pk4[: len(y) * 4].reshape(len(y), 4).max(axis=1) if len(pk4) >= len(y) * 4 else np.max(np.abs(y), axis=1)
    need = np.minimum(1.0, ceil / np.maximum(pk, 1e-9))
    la = max(1, int(lookahead_ms * 1e-3 * sr))
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    g = minimum_filter1d(need, size=2 * la + 1, mode="nearest")
    rel = max(1, int(release_ms * 1e-3 * sr))
    g = np.minimum(g, uniform_filter1d(minimum_filter1d(g, size=rel, mode="nearest"), size=la, mode="nearest"))
    out = y * g[:, None]
    tp = true_peak_db(out, sr)
    if tp > ceiling_db:
        out *= 10 ** ((ceiling_db - tp - 0.05) / 20)
    return out


# ---------- space ---------------------------------------------------------------------------------
REVERB_PRESETS = {
    "dry": {"label": "Dry (no added acoustics)", "rt60": 0.0, "predelay_ms": 0, "wet_db": -120, "damping_hz": 8000},
    "small_chapel": {"label": "Small stone chapel", "rt60": 1.4, "predelay_ms": 12, "wet_db": -17, "damping_hz": 6500},
    "parish_church": {"label": "Parish church", "rt60": 2.4, "predelay_ms": 22, "wet_db": -15, "damping_hz": 5200},
    "cathedral": {"label": "Cathedral nave", "rt60": 4.5, "predelay_ms": 35, "wet_db": -16, "damping_hz": 4200},
}


def church_ir(sr: int, rt60: float, predelay_ms: float, damping_hz: float, seed: int = 7) -> np.ndarray:
    """Synthetic stereo impulse response: sparse early reflections + exponentially decaying diffuse tail
    whose high frequencies decay faster (air/stone absorption). This is an algorithmic room model, not a
    measured impulse response of a real church."""
    rng = np.random.default_rng(seed)
    n = int((rt60 * 1.3 + 0.1) * sr)
    t = np.arange(n) / sr
    ir = np.zeros((n, 2))
    for ch in range(2):
        noise = rng.standard_normal(n)
        tail = noise * 10 ** (-3 * t / rt60)
        # frequency-dependent decay: blend with a low-passed tail that survives longer
        b, a = signal.butter(2, min(damping_hz, sr / 2 - 200) / (sr / 2), "low")
        dark = signal.lfilter(b, a, noise) * 10 ** (-3 * t / (rt60 * 1.25))
        mixc = np.clip(t / max(rt60 * 0.6, 1e-3), 0, 1)
        tail = tail * (1 - mixc) + dark * mixc * 1.6
        onset = int(0.03 * sr)
        tail[:onset] *= np.linspace(0, 1, onset) ** 2  # diffuse field builds up
        for _ in range(9):  # early reflections
            d = int(rng.uniform(0.006, 0.07) * sr)
            if d < n:
                tail[d] += rng.uniform(0.25, 0.6) * rng.choice([-1, 1]) * 6.0
        ir[:, ch] = tail
    pd = int(predelay_ms * 1e-3 * sr)
    ir = np.concatenate([np.zeros((pd, 2)), ir])
    b, a = signal.butter(2, 180 / (sr / 2), "high")  # keep low mud out of the reverb
    ir = signal.lfilter(b, a, ir, axis=0)
    return ir / (np.sqrt(np.sum(ir**2, axis=0, keepdims=True)) + 1e-12)


def reverb(y: np.ndarray, sr: int, preset: str = "parish_church", wet_db: float | None = None) -> np.ndarray:
    p = REVERB_PRESETS.get(preset) or REVERB_PRESETS["parish_church"]
    if p["rt60"] <= 0:
        return y
    ir = church_ir(sr, p["rt60"], p["predelay_ms"], p["damping_hz"])
    wet = np.stack([signal.fftconvolve(y[:, c], ir[:, c]) for c in range(2)], axis=1)
    g = 10 ** ((p["wet_db"] if wet_db is None else wet_db) / 20)
    out = np.zeros((len(wet), 2))
    out[: len(y)] = y
    return out + wet * g


def pan(y_mono: np.ndarray, position: float) -> np.ndarray:
    """Constant-power pan. position: -1 (left) .. +1 (right)."""
    a = (float(np.clip(position, -1, 1)) + 1) * np.pi / 4
    return np.stack([y_mono * np.cos(a), y_mono * np.sin(a)], axis=1) * np.sqrt(2) / 1.0


def stereo_width(y: np.ndarray, width: float) -> np.ndarray:
    mid, side = (y[:, 0] + y[:, 1]) / 2, (y[:, 0] - y[:, 1]) / 2
    side = side * float(np.clip(width, 0, 2))
    return np.stack([mid + side, mid - side], axis=1)


# ---------- restoration ---------------------------------------------------------------------------
def spectral_denoise(y: np.ndarray, sr: int, strength: float = 0.6) -> tuple[np.ndarray, dict]:
    """Spectral gating: a per-frequency noise profile is estimated from the quietest 10% of frames and
    attenuated with a smoothed soft mask. `strength` 0..1 scales the maximum attenuation (up to 18 dB).
    Conservative by design: it lowers steady hiss/hum/room tone, it does not remove talking or coughs."""
    strength = float(np.clip(strength, 0, 1))
    nper, hop = 2048, 512
    out = np.zeros_like(y, dtype=np.float64)
    max_att = 10 ** (-(18 * strength) / 20)
    f, _, Z0 = signal.stft(y.mean(axis=1), sr, nperseg=nper, noverlap=nper - hop)
    mag0 = np.abs(Z0)
    frame_e = mag0.sum(axis=0)
    quiet = frame_e <= np.percentile(frame_e, 10)
    profile = np.median(mag0[:, quiet], axis=1, keepdims=True) if quiet.any() else np.min(mag0, axis=1, keepdims=True)
    from scipy.ndimage import uniform_filter

    snr = mag0 / (profile * (1.5 + 1.5 * strength) + 1e-12)
    mask = np.clip((snr - 1) / 1.0, 0, 1)
    mask = uniform_filter(mask, size=(3, 5))
    mask = max_att + (1 - max_att) * mask
    for c in range(y.shape[1]):
        _, _, Z = signal.stft(y[:, c], sr, nperseg=nper, noverlap=nper - hop)
        _, x = signal.istft(Z * mask, sr, nperseg=nper, noverlap=nper - hop)
        m = min(len(x), len(y))
        out[:m, c] = x[:m]
    return out, {"noise_profile_frames": int(quiet.sum()), "max_attenuation_db": round(18 * strength, 1)}


def tonal_tilt_bands(band_energy_db: dict) -> list[dict]:
    """Gentle corrective EQ derived from measured band energies (phone recordings are typically
    boxy at 250 Hz–1 kHz and short of air). Corrections are capped at ±4 dB."""
    bands = [{"type": "highpass", "freq": 60, "q": 0.707}]
    lowmid = band_energy_db.get("lowmid_250_1k", -6)
    high = band_energy_db.get("high_4k_10k", -25)
    if lowmid > -3:
        bands.append({"type": "peak", "freq": 400, "gain_db": -min(4.0, (lowmid + 3) * 1.5 + 1.5), "q": 0.9})
    if high < -22:
        bands.append({"type": "highshelf", "freq": 6000, "gain_db": min(4.0, (-22 - high) * 0.4 + 1.0), "q": 0.707})
    return bands


def normalize_loudness(y: np.ndarray, sr: int, target_lufs: float) -> tuple[np.ndarray, float | None]:
    cur = integrated_lufs(y, sr)
    if cur is None:
        return y, None
    return y * 10 ** ((target_lufs - cur) / 20), cur


# ---------- enhancement (Restore mode) -----------------------------------------------------------
def band_energy_ratio_db(y: np.ndarray, sr: int, hi: tuple[float, float], ref: tuple[float, float]) -> float:
    mono = y.mean(axis=1) if y.ndim > 1 else y
    f, pxx = signal.welch(mono, sr, nperseg=8192)
    e = lambda a, b: float(np.sum(pxx[(f >= a) & (f < b)])) + 1e-20
    return 10 * np.log10(e(*hi) / e(*ref))


def harmonic_exciter(y: np.ndarray, sr: int, amount: float = 0.5, src: tuple[float, float] = (700.0, 2200.0),
                     cutoff: float | None = None, target_db: float = -18.0) -> tuple[np.ndarray, dict]:
    """Rebuild missing brightness from what IS there. A phone mic and lossy codec cut the top octaves, but the
    surviving 0.7-2.2 kHz band carries the pitch of every voice. Driving that band through a soft saturator
    creates its harmonics (exactly the overtones that were lost: integer multiples of the real notes); we keep
    only the part above the original cutoff and add it at a level chosen from the measured spectrum. This is the
    classic 'exciter' / bandwidth-extension technique. It adds plausible overtones of the real performance; it
    cannot recover consonants or noises that were never captured. amount 0..1 scales how close the new top
    octave is brought to the target balance (target_db below the 0.5-2 kHz band)."""
    amount = float(np.clip(amount, 0, 1))
    if amount <= 0:
        return y, {"added_db": None}
    mono_in = y.ndim == 1
    Y = y[:, None] if mono_in else y
    sos_bp = signal.butter(4, [src[0] / (sr / 2), min(src[1], sr / 2 - 100) / (sr / 2)], "bandpass", output="sos")
    cut = cutoff or src[1] * 1.05
    sos_hp = signal.butter(6, min(cut, sr / 2 - 200) / (sr / 2), "highpass", output="sos")
    out = np.zeros_like(Y, dtype=np.float64)
    for c in range(Y.shape[1]):
        band = signal.sosfiltfilt(sos_bp, Y[:, c])
        env = np.sqrt(np.maximum(signal.sosfiltfilt(signal.butter(2, 8 / (sr / 2), output="sos"), band ** 2), 0.0) + 1e-12)
        drive = band / (env * 1.2 + 1e-6)          # level-independent drive: quiet and loud passages get the same colour
        h = np.tanh(2.5 * drive) * env              # soft saturation -> odd harmonics; scaled back to the band's level
        h += 0.35 * (np.abs(np.tanh(2.0 * drive)) * env - np.mean(np.abs(np.tanh(2.0 * drive)) * env))  # a little even-order warmth
        out[:, c] = signal.sosfiltfilt(sos_hp, h)
    before = band_energy_ratio_db(Y, sr, (cut, min(10000, sr / 2 - 1)), (500, 2000))
    # choose gain so that (existing top + added top) reaches the target balance, scaled by `amount`
    f, p_y = signal.welch(Y.mean(axis=1), sr, nperseg=8192)
    _, p_h = signal.welch(out.mean(axis=1), sr, nperseg=8192)
    ref = float(np.sum(p_y[(f >= 500) & (f < 2000)]))
    top_y = float(np.sum(p_y[(f >= cut) & (f < 10000)]))
    top_h = float(np.sum(p_h[(f >= cut) & (f < 10000)])) + 1e-20
    want = ref * 10 ** ((before + amount * (target_db - before)) / 10) if before < target_db else top_y
    g = np.sqrt(max(0.0, want - top_y) / top_h)
    g = float(min(g, 4.0))
    res = Y + g * out
    after = band_energy_ratio_db(res, sr, (cut, min(10000, sr / 2 - 1)), (500, 2000))
    return (res[:, 0] if mono_in else res), {"top_band_before_db": round(before, 1), "top_band_after_db": round(after, 1), "gain": round(g, 3), "cutoff_hz": round(cut)}


def decorrelated_stereo(mono: np.ndarray, sr: int, width: float = 0.6, seed: int = 11) -> np.ndarray:
    """Mono -> stereo without changing the mono sum (L+R = 2*mono, so it stays faithful on a phone speaker).
    The side signal is the mono signal passed through a short random-phase diffusion filter (decorrelation),
    kept out of the bass. width 0..1."""
    rng = np.random.default_rng(seed)
    n = int(0.025 * sr)
    t = np.arange(n) / sr
    ir = rng.standard_normal(n) * np.exp(-t / 0.006)
    ir -= ir.mean()
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-12
    side = signal.fftconvolve(mono, ir)[: len(mono)]
    side = signal.sosfiltfilt(signal.butter(2, 250 / (sr / 2), "highpass", output="sos"), side)
    side *= float(np.clip(width, 0, 1)) * 0.5
    return np.stack([mono + side, mono - side], axis=1)
