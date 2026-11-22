"""Deterministic music-information-retrieval functions used by the Transcription, Structure and
Fidelity agents. Each returns estimates WITH a confidence figure; none of them is ground truth."""
from __future__ import annotations

import numpy as np

from ..score.model import NOTE_NAMES

SR = 22050
HOP = 512


# ---------------------------------------------------------------- tempo / beats / meter
def tempo_and_beats(y: np.ndarray, sr: int, start_bpm: float = 90.0) -> dict:
    import librosa

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    # start_bpm is a prior, and it matters: sustained choir/organ textures have weak onsets, so the tracker can
    # lock onto 2/3x or 3/2x the real pulse. 90 BPM suits hymn tempi; the result is reported with alternatives.
    tempo, frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP, start_bpm=start_bpm)
    tempo = float(np.atleast_1d(tempo)[0])
    beats = librosa.frames_to_time(frames, sr=sr, hop_length=HOP)
    ibi = np.diff(beats)
    stability = float(1.0 - min(1.0, np.std(ibi) / (np.mean(ibi) + 1e-9) * 2)) if len(ibi) > 3 else 0.0
    # local tempo every ~8 beats: this is the expressive timing (rubato / ritardando)
    curve = []
    for i in range(0, max(0, len(ibi) - 7), 4):
        curve.append({"time": round(float(beats[i]), 2), "bpm": round(float(60.0 / np.median(ibi[i : i + 8])), 1)})
    # meter: is beat-synchronous onset strength periodic every 3 or every 4 beats?
    meter, meter_conf, offset = (4, 4), 0.0, 0
    if len(frames) >= 16:
        strength = onset_env[np.clip(frames, 0, len(onset_env) - 1)]
        strength = (strength - strength.mean()) / (strength.std() + 1e-9)
        scores = {}
        for m in (2, 3, 4):
            ac = np.mean(strength[:-m] * strength[m:])
            scores[m] = float(ac)
        best = 3 if scores[3] > max(scores[4], scores[2]) + 0.03 else 4
        meter = (3, 4) if best == 3 else (4, 4)
        other = scores[4] if best == 3 else scores[3]
        meter_conf = float(np.clip((scores[best] - other) * 2 + 0.3, 0.05, 0.9))
        phase = [float(np.mean(strength[k::best])) for k in range(best)]
        offset = int(np.argmax(phase))
    return {"tempo_bpm": round(tempo, 2), "beats": [round(float(b), 4) for b in beats],
            "beat_stability": round(stability, 3), "tempo_curve": curve,
            "tempo_alternatives": [round(tempo * k, 1) for k in (2 / 3, 3 / 2, 0.5, 2.0) if 40 <= tempo * k <= 200],
            "time_signature": meter, "meter_confidence": round(meter_conf, 2), "downbeat_offset": offset}


# ---------------------------------------------------------------- key
_KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def estimate_key(chroma: np.ndarray) -> dict:
    """Krumhansl-Schmuckler profile correlation on the time-averaged chroma."""
    prof = chroma.mean(axis=1)
    cands = []
    for i in range(12):
        cands.append((float(np.corrcoef(prof, np.roll(_KS_MAJOR, i))[0, 1]), f"{NOTE_NAMES[i]} major"))
        cands.append((float(np.corrcoef(prof, np.roll(_KS_MINOR, i))[0, 1]), f"{NOTE_NAMES[i]} minor"))
    cands.sort(reverse=True)
    margin = cands[0][0] - cands[1][0]
    return {"key": cands[0][1], "confidence": round(float(np.clip(0.4 + margin * 4, 0.05, 0.95)), 2),
            "alternatives": [{"key": k, "score": round(s, 3)} for s, k in cands[1:4]]}


def tuning_offset_cents(y: np.ndarray, sr: int) -> float:
    import librosa

    return round(float(librosa.estimate_tuning(y=y, sr=sr)) * 100, 1)


# ---------------------------------------------------------------- chords
def _chord_templates() -> tuple[np.ndarray, list[str]]:
    T, labels = [], []
    for root in range(12):
        for name, ivs in (("", (0, 4, 7)), ("m", (0, 3, 7))):
            v = np.zeros(12)
            v[[(root + i) % 12 for i in ivs]] = 1
            T.append(v / np.linalg.norm(v))
            labels.append(f"{NOTE_NAMES[root]}{name}")
    return np.array(T), labels


def estimate_chords(chroma: np.ndarray, frame_times: np.ndarray, beats: list[float]) -> list[dict]:
    """Beat-synchronous major/minor triad template matching. Only triads are considered: sevenths,
    suspensions and inversions are NOT detected, so treat this as a harmonic sketch."""
    if len(beats) < 2:
        return []
    T, labels = _chord_templates()
    out = []
    for a, b in zip(beats[:-1], beats[1:]):
        seg = chroma[:, (frame_times >= a) & (frame_times < b)]
        if seg.shape[1] == 0:
            continue
        v = seg.mean(axis=1)
        if v.sum() < 1e-6:
            continue
        sims = T @ (v / (np.linalg.norm(v) + 1e-9))
        order = np.argsort(sims)[::-1]
        conf = float(np.clip((sims[order[0]] - sims[order[1]]) * 5 + (sims[order[0]] - 0.6), 0.05, 0.95))
        lab = labels[order[0]]
        if out and out[-1]["label"] == lab:
            out[-1]["end"] = round(float(b), 3)
            out[-1]["confidence"] = round((out[-1]["confidence"] + conf) / 2, 2)
        else:
            out.append({"start": round(float(a), 3), "end": round(float(b), 3), "label": lab, "confidence": round(conf, 2)})
    return out


# ---------------------------------------------------------------- monophonic melody (pYIN)
def pitch_track(y: np.ndarray, sr: int, fmin: float = 98.0, fmax: float = 1046.0) -> dict:
    import librosa

    f0, voiced, prob = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=sr, frame_length=2048, hop_length=HOP, fill_na=np.nan)
    times = librosa.times_like(f0, sr=sr, hop_length=HOP)
    return {"times": times, "f0": f0, "voiced": voiced, "prob": prob}


def split_at_onsets(notes: list[dict], y: np.ndarray, sr: int, min_piece: float = 0.15, drop_db: float = 6.0) -> list[dict]:
    """A pitch contour cannot show a repeated note (same pitch re-articulated). Split a note where the
    level of the same audio dips at least `drop_db` below the note's median level: the gap or consonant
    between two articulations. (A spectral-flux onset detector was tried first and over-split sustained
    notes with vibrato: 59% precision on the test fixture versus 100% for level dips.)"""
    import librosa

    hop = 256
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=hop)[0]
    level = 20 * np.log10(rms + 1e-9)
    t = librosa.times_like(rms, sr=sr, hop_length=hop)
    out = []
    for n in notes:
        m = (t >= n["start"]) & (t < n["end"])
        seg, ts = level[m], t[m]
        cuts: list[float] = []
        if len(seg) >= 8:
            ref = float(np.median(seg))
            i = 1
            while i < len(seg) - 1:
                if seg[i] < ref - drop_db and seg[i] <= seg[i - 1] and seg[i] <= seg[i + 1]:
                    j = i
                    while j < len(seg) - 1 and seg[j] < ref - drop_db / 2:  # the next articulation starts when the level recovers
                        j += 1
                    c = float(ts[j])
                    if c - n["start"] >= min_piece and n["end"] - c >= min_piece and (not cuts or c - cuts[-1] >= min_piece):
                        cuts.append(c)
                    i = j + 1
                else:
                    i += 1
        edges = [n["start"], *cuts, n["end"]]
        for a, b in zip(edges[:-1], edges[1:]):
            out.append({**n, "start": round(a, 3), "end": round(b, 3)})
    return out


def clean_melody(notes: list[dict], scoop_max: float = 0.25, min_dur: float = 0.12) -> tuple[list[dict], dict]:
    """Repair the typical failure of a monophonic pitch tracker on a choir in a reverberant room:
    1. scoops and vibrato show up as runs of short notes a semitone apart (C#-D-C#): merge neighbours that
       are at most a semitone apart when one of them is short, keeping the pitch held longest;
    2. the tracker jumps between voices or locks onto a harmonic: fold notes lying an octave away from
       their neighbourhood back into it and flag them;
    3. drop fragments still shorter than min_dur.
    Returns (notes, stats). Every repaired note is flagged uncertain."""
    stats = {"merged_scoops": 0, "octave_folded": 0, "dropped_fragments": 0}
    ns = [dict(n) for n in sorted(notes, key=lambda n: n["start"])]
    changed = True
    while changed:
        changed = False
        out: list[dict] = []
        for n in ns:
            if out:
                a = out[-1]
                da, dn = a["end"] - a["start"], n["end"] - n["start"]
                if n["start"] - a["end"] <= 0.1 and abs(n["pitch"] - a["pitch"]) <= 1 and min(da, dn) < scoop_max:
                    keep = a if da >= dn else n
                    out[-1] = {**a, "end": max(a["end"], n["end"]), "pitch": keep["pitch"],
                               "confidence": round(((a.get("confidence") or 0.5) * da + (n.get("confidence") or 0.5) * dn) / (da + dn + 1e-9), 2),
                               "repaired": True}
                    stats["merged_scoops"] += 1
                    changed = True
                    continue
            out.append(n)
        ns = out
    for i, n in enumerate(ns):
        neigh = [ns[k]["pitch"] for k in range(max(0, i - 3), min(len(ns), i + 4)) if k != i]
        if len(neigh) >= 2:
            med = float(np.median(neigh))
            for shift in (-12, 12):
                if abs(n["pitch"] - med) >= 9 and abs(n["pitch"] + shift - med) < abs(n["pitch"] - med) - 4:
                    n["pitch"] += shift
                    n["repaired"] = True
                    n["confidence"] = round((n.get("confidence") or 0.5) * 0.7, 2)
                    stats["octave_folded"] += 1
                    break
    kept = [n for n in ns if n["end"] - n["start"] >= min_dur]
    stats["dropped_fragments"] = len(ns) - len(kept)
    return kept, stats


def notes_from_pitch_track(track: dict, min_dur: float = 0.09) -> list[dict]:
    """Segment an f0 contour into notes: consecutive voiced frames whose rounded MIDI pitch is stable
    (after a 5-frame median filter) form one note. Confidence = mean voicing probability."""
    from scipy.signal import medfilt

    f0, times, prob = track["f0"], track["times"], track["prob"]
    midi = np.full(len(f0), np.nan)
    ok = ~np.isnan(f0)
    midi[ok] = 69 + 12 * np.log2(f0[ok] / 440.0) - float(track.get("tuning_cents", 0.0)) / 100.0  # sing-flat/sharp choirs round correctly
    q = np.where(ok, np.round(midi), -1)
    q = medfilt(q, 5)
    notes, start = [], None
    hop_t = float(times[1] - times[0]) if len(times) > 1 else HOP / SR
    for i in range(len(q) + 1):
        cur = q[i] if i < len(q) else -2
        if start is not None and cur != q[start]:
            dur = (i - start) * hop_t
            if q[start] >= 0 and dur >= min_dur:
                seg = slice(start, i)
                dev = float(np.nanmean(np.abs(midi[seg] - q[start]))) if np.any(ok[seg]) else 0.5
                conf = float(np.nanmean(prob[seg])) * float(np.clip(1.0 - dev, 0.3, 1.0))
                notes.append({"start": round(float(times[start]), 3), "end": round(float(times[start]) + dur, 3),
                              "pitch": int(q[start]), "confidence": round(float(np.clip(conf, 0.01, 0.99)), 2)})
            start = None
        if start is None and i < len(q):
            start = i
    return notes


# ---------------------------------------------------------------- polyphonic (Basic Pitch)
def basic_pitch_available() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa: F401
        from basic_pitch import ICASSP_2022_MODEL_PATH
        import pathlib

        p = pathlib.Path(ICASSP_2022_MODEL_PATH).parent / "nmp.onnx"
        return (p.exists(), "" if p.exists() else "Basic Pitch ONNX model file missing")
    except Exception as e:  # pragma: no cover
        return False, f"Basic Pitch not installed ({e.__class__.__name__})"


def basic_pitch_notes(wav_path, onset_threshold: float = 0.5, frame_threshold: float = 0.3,
                      min_note_ms: float = 110.0) -> list[dict]:
    """Polyphonic note events from Spotify's Basic Pitch (ICASSP 2022) via ONNX runtime.
    Confidence is the model's mean frame activation for the note (its 'amplitude')."""
    import pathlib

    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import Model, predict

    model = Model(pathlib.Path(ICASSP_2022_MODEL_PATH).parent / "nmp.onnx")
    _, _, events = predict(str(wav_path), model, onset_threshold=onset_threshold, frame_threshold=frame_threshold,
                           minimum_note_length=min_note_ms, minimum_frequency=60.0, maximum_frequency=2100.0,
                           multiple_pitch_bends=False, melodia_trick=True)
    return [{"start": round(float(s), 3), "end": round(float(e), 3), "pitch": int(p),
             "confidence": round(float(np.clip(a, 0.01, 0.99)), 2)} for s, e, p, a, _ in sorted(events, key=lambda x: x[0])]


def clean_polyphonic(notes: list[dict], gap: float = 0.15, min_dur: float = 0.12, min_conf: float = 0.5,
                     harmonic_margin: float = 0.2) -> tuple[list[dict], dict]:
    """Post-process raw Basic Pitch events (parameters tuned on the synthetic organ fixture in tests/):
    1. merge same-pitch fragments separated by < gap (sustained notes get chopped by the model);
    2. drop notes below min_conf or shorter than min_dur;
    3. FLAG (not delete) notes that sit an octave/twelfth/etc. above a clearly stronger simultaneous note:
       organ mixtures and bright voices produce overtones the model reports as notes. Real octave doublings
       look identical, so the decision is left to the reviewer."""
    by: dict[int, list[dict]] = {}
    for n in sorted(notes, key=lambda n: n["start"]):
        by.setdefault(n["pitch"], []).append(dict(n))
    merged = []
    for lst in by.values():
        cur = lst[0]
        for n in lst[1:]:
            if n["start"] - cur["end"] <= gap:
                d1, d2 = cur["end"] - cur["start"], n["end"] - n["start"]
                cur["confidence"] = round((cur["confidence"] * d1 + n["confidence"] * d2) / (d1 + d2 + 1e-9), 2)
                cur["end"] = max(cur["end"], n["end"])
            else:
                merged.append(cur)
                cur = n
        merged.append(cur)
    kept = [n for n in merged if n["end"] - n["start"] >= min_dur and n["confidence"] >= min_conf]
    suspects = 0
    for n in kept:
        d = n["end"] - n["start"]
        for o in kept:
            if (n["pitch"] - o["pitch"]) in (12, 19, 24, 28, 31, 36) and o["confidence"] >= n["confidence"] + harmonic_margin:
                if min(n["end"], o["end"]) - max(n["start"], o["start"]) >= 0.6 * d:
                    n["overtone_suspect"] = True
                    suspects += 1
                    break
    kept.sort(key=lambda n: (n["start"], n["pitch"]))
    return kept, {"raw_events": len(notes), "after_merge": len(merged), "kept": len(kept), "dropped_low_confidence_or_short": len(merged) - len(kept),
                  "overtone_suspects": suspects, "thresholds": {"min_conf": min_conf, "min_dur": min_dur, "merge_gap": gap}}


def skyline(notes: list[dict], floor: int = 55) -> list[dict]:
    """Top-line extraction: notes that are the highest sounding pitch at their midpoint. A crude stand-in for
    a melody when no separated voice stem exists; it follows whatever is on top, voice or instrument."""
    out = []
    for n in notes:
        if n["pitch"] < floor or n.get("overtone_suspect"):
            continue
        mid = (n["start"] + n["end"]) / 2
        if not any(o["pitch"] > n["pitch"] and o["start"] <= mid < o["end"] and not o.get("overtone_suspect") for o in notes):
            out.append(n)
    return out


def split_voices(notes: list[dict], melody: list[dict] | None = None) -> dict[str, list[dict]]:
    """Heuristic voice assignment for polyphonic note events. At each note the set of simultaneously
    sounding notes decides: top = upper line, bottom (if below C3 or clearly lowest) = bass, rest = inner.
    This is register-based sorting, NOT true voice separation; crossing voices will be mis-assigned."""
    if not notes:
        return {"upper": [], "inner": [], "bass": []}
    out = {"upper": [], "inner": [], "bass": []}
    arr = sorted(notes, key=lambda n: n["start"])
    for n in arr:
        mid = (n["start"] + n["end"]) / 2
        sounding = [m["pitch"] for m in arr if m["start"] <= mid < m["end"]]
        hi, lo = max(sounding), min(sounding)
        if len(sounding) == 1:
            key = "bass" if n["pitch"] < 52 else "upper"
        elif n["pitch"] == hi:
            key = "upper"
        elif n["pitch"] == lo and (lo < 55 or hi - lo >= 12):
            key = "bass"
        else:
            key = "inner"
        out[key].append(n)
    return out


# ---------------------------------------------------------------- features / structure
def chroma_features(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    import librosa

    c = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
    return c, librosa.times_like(c, sr=sr, hop_length=HOP)


def novelty_boundaries(y: np.ndarray, sr: int, kernel_s: float = 20.0, min_gap_s: float = 6.0) -> tuple[list[float], np.ndarray, np.ndarray]:
    """Foote novelty: slide a checkerboard kernel along the self-similarity matrix of timbre+harmony+level
    features; peaks are moments where what comes after sounds different from what came before."""
    import librosa
    from scipy.signal import find_peaks

    step = int(round(0.5 * sr / HOP))  # 0.5 s feature frames
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=HOP)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
    contrast = librosa.feature.spectral_contrast(y=y, sr=sr, hop_length=HOP)
    rms = librosa.amplitude_to_db(librosa.feature.rms(y=y, hop_length=HOP), ref=np.max)
    n = min(mfcc.shape[1], chroma.shape[1], contrast.shape[1], rms.shape[1])
    k = n // step
    if k < 8:
        return [], np.zeros(0), np.zeros(0)

    def pool(f):
        return f[:, : k * step].reshape(f.shape[0], k, step).mean(axis=2)

    def z(f):
        return (f - f.mean(axis=1, keepdims=True)) / (f.std(axis=1, keepdims=True) + 1e-9)

    # Harmony (chroma) is deliberately left out: chords change every bar, sections do not. Features are smoothed
    # over ~4 s so bar-level changes do not register. Tuned on the synthetic fixture (boundaries within ~2 s).
    from scipy.ndimage import uniform_filter1d

    F = uniform_filter1d(np.vstack([z(pool(mfcc[1:])), z(pool(contrast)), z(pool(rms))]), size=8, axis=1, mode="nearest")
    Fn = F / (np.linalg.norm(F, axis=0, keepdims=True) + 1e-9)
    S = Fn.T @ Fn
    L = max(4, int(kernel_s / 0.5) // 2)
    g = np.outer(np.hanning(2 * L + 1)[1:-1], np.hanning(2 * L + 1)[1:-1]) if L > 1 else np.ones((2 * L - 1, 2 * L - 1))
    half = g.shape[0] // 2
    sign = np.ones_like(g)
    sign[:half, half + 1:] = -1
    sign[half + 1:, :half] = -1
    K = g * sign
    pad = np.pad(S, half, mode="edge")
    nov = np.array([np.sum(pad[i: i + K.shape[0], i: i + K.shape[1]] * K) for i in range(k)])
    nov = np.maximum(nov, 0)
    nov = nov / (nov.max() + 1e-9)
    peaks, _ = find_peaks(nov, distance=int(min_gap_s / 0.5), prominence=0.12, height=0.15)
    times = np.arange(k) * 0.5
    return [float(times[p]) for p in peaks if 5.0 < times[p] < len(y) / sr - 5.0], nov, times


def segment_structure(y: np.ndarray, sr: int, beats: list[float], vocal_activity: np.ndarray | None,
                      va_times: np.ndarray | None, target_len: float = 16.0) -> list[dict]:
    """Section boundaries from novelty peaks (plus vocal entries/exits when a voice-activity curve exists),
    snapped to the nearest beat; then repetition grouping by chroma similarity and vocal/instrumental tagging."""
    import librosa

    dur = len(y) / sr
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
    bounds, _, _ = novelty_boundaries(y, sr)
    if vocal_activity is not None and va_times is not None and len(vocal_activity) > 20:
        from scipy.ndimage import median_filter

        hop_va = float(np.median(np.diff(va_times))) or 0.1
        sm = median_filter(vocal_activity.astype(float), size=max(3, int(3.0 / hop_va) | 1)) > 0.5
        for i in np.where(np.diff(sm.astype(int)) != 0)[0]:
            t = float(va_times[i + 1])
            if 3.0 < t < dur - 3.0 and all(abs(t - b) > 3.0 for b in bounds):
                bounds.append(t)
            elif 3.0 < t < dur - 3.0:  # a voice entry is a better boundary than a nearby timbre peak
                j = int(np.argmin([abs(t - b) for b in bounds]))
                bounds[j] = t
    b_arr = np.array(beats)
    snapped = []
    for t in sorted(bounds):
        if len(b_arr):
            nb = float(b_arr[np.argmin(np.abs(b_arr - t))])
            t = nb if abs(nb - t) < 0.6 else t
        if not snapped or t - snapped[-1] >= 4.0:
            snapped.append(t)
    merged = [0.0] + [t for t in snapped if t >= 3.0 and dur - t >= 3.0] + [dur]
    segs = []
    for a, b in zip(merged[:-1], merged[1:]):
        fa, fb = librosa.time_to_frames([a, b], sr=sr, hop_length=HOP)
        prof = chroma[:, fa:max(fb, fa + 1)].mean(axis=1)
        vocal_ratio = None
        if vocal_activity is not None and va_times is not None:
            m = (va_times >= a) & (va_times < b)
            vocal_ratio = float(np.mean(vocal_activity[m])) if m.any() else 0.0
        segs.append({"start": round(a, 2), "end": round(b, 2), "profile": prof, "vocal_ratio": vocal_ratio})
    # repetition groups
    letters, reps = [], []
    for s in segs:
        best, best_sim = None, 0.0
        for j, r in enumerate(reps):
            sim = float(np.dot(s["profile"], r) / (np.linalg.norm(s["profile"]) * np.linalg.norm(r) + 1e-9))
            if sim > best_sim:
                best, best_sim = j, sim
        if best is not None and best_sim > 0.93:
            letters.append((best, best_sim))
        else:
            reps.append(s["profile"])
            letters.append((len(reps) - 1, None))
    out = []
    for i, (s, (g, sim)) in enumerate(zip(segs, letters)):
        vr = s["vocal_ratio"]
        if vr is None:
            kind, label = "unknown", f"Section {i + 1}"
        elif vr >= 0.45:
            kind, label = "vocal", "Vocal section"
        else:
            kind = "instrumental"
            label = "Instrumental introduction" if i == 0 else ("Instrumental ending" if i == len(segs) - 1 else "Instrumental passage")
        out.append({"start": s["start"], "end": s["end"], "label": label, "kind": kind,
                    "group": chr(ord("A") + g % 26), "repeat_similarity": round(sim, 3) if sim else None,
                    "vocal_ratio": round(vr, 2) if vr is not None else None, "status": "detected",
                    "confidence": round(float(np.clip(0.35 + (abs((vr or 0.5) - 0.45)), 0.2, 0.8)), 2)})
    return out


# ---------------------------------------------------------------- alignment (DTW)
def dtw_alignment(chroma_ref: np.ndarray, chroma_other: np.ndarray, hop_t: float) -> dict:
    """Chroma DTW between reference (original) and other (reconstruction). Returns the warping path as
    time pairs plus mean local cost. The path IS the alignment map used for A/B seeking."""
    import librosa

    # Digital silence gives all-zero chroma frames, for which cosine distance is undefined (NaN). A tiny uniform
    # floor makes silent frames compare as 'no pitch content' instead of crashing the alignment.
    D, wp = librosa.sequence.dtw(X=chroma_ref + 1e-6, Y=chroma_other + 1e-6, metric="cosine", subseq=False)
    wp = wp[::-1]
    ref_t, oth_t = wp[:, 0] * hop_t, wp[:, 1] * hop_t
    cost = float(D[-1, -1] / len(wp))
    # thin the path to ~1 point per 0.5 s of reference time for the UI
    keep, last = [], -1e9
    for a, b in zip(ref_t, oth_t):
        if a - last >= 0.5:
            keep.append([round(float(a), 2), round(float(b), 2)])
            last = a
    keep.append([round(float(ref_t[-1]), 2), round(float(oth_t[-1]), 2)])
    dev = np.abs(ref_t - oth_t)
    return {"path": keep, "mean_cost": round(cost, 4), "max_time_deviation_s": round(float(dev.max()), 3),
            "median_time_deviation_s": round(float(np.median(dev)), 3), "ref_t": ref_t, "oth_t": oth_t}


def map_time(path: list[list[float]], t: float, reverse: bool = False) -> float:
    a = np.array(path)
    src, dst = (a[:, 1], a[:, 0]) if reverse else (a[:, 0], a[:, 1])
    order = np.argsort(src)
    return float(np.interp(t, src[order], dst[order]))
