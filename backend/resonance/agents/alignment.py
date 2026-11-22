"""Agent 3b: Score Alignment.

Takes a score you trust (imported MusicXML/MIDI, usually one pass through the hymn: verse + refrain) and
places it onto the recording's own timeline:
  1. finds the transposition (the choir may sing in a different key from the printed score);
  2. for every section of the verified timeline, finds the stretch of the score that section performs
     (subsequence dynamic time warping on chroma) and warps the notes to the performers' timing;
  3. sung parts go only into sections marked 'voices'; instrumental parts go everywhere.
The result is a new score version on the recording's clock. It is a hypothesis to review, not a fact: the
local match quality is reported per section and poorly matched sections are flagged.
"""
from __future__ import annotations

import copy
import uuid

import numpy as np

from .. import gates
from .. import models as m
from ..audio import io as aio
from ..music import analysis as mu
from ..score.model import validate_score
from .base import Blocked, Ctx, agent, new_version

FRAME = 0.1  # seconds per chroma frame on both sides


def score_chroma(parts: list[dict], duration: float, transpose: int = 0, roles: set[str] | None = None) -> np.ndarray:
    n = max(2, int(np.ceil(duration / FRAME)) + 1)
    C = np.zeros((12, n))
    for p in parts:
        if roles and p.get("role") not in roles:
            continue
        for nt in p["notes"]:
            a, b = int(nt["start"] / FRAME), max(int(nt["start"] / FRAME) + 1, int(nt["end"] / FRAME))
            C[(nt["pitch"] + transpose) % 12, a:b] += nt.get("velocity", 80) / 100.0
    C += 1e-4
    return C / np.linalg.norm(C, axis=0, keepdims=True)


def audio_chroma(y: np.ndarray, sr: int) -> np.ndarray:
    c, times = mu.chroma_features(y, sr)
    # Resample onto an exact FRAME grid. (Pooling whole hops gave 0.0929 s frames treated as 0.1 s: a 7% clock
    # error that put every later section seconds away from where it really is.)
    grid = np.arange(0, len(y) / sr, FRAME)
    from scipy.ndimage import uniform_filter1d

    c = uniform_filter1d(c, size=max(1, int(round(FRAME * sr / mu.HOP))), axis=1, mode="nearest")
    c = np.stack([np.interp(grid, times, c[k]) for k in range(12)]) + 1e-4
    return c / np.linalg.norm(c, axis=0, keepdims=True)


def best_transposition(score_c: np.ndarray, audio_c: np.ndarray) -> list[tuple[int, float]]:
    """Rank the 12 transpositions by correlation of the overall pitch-class profiles."""
    sp, ap = score_c.mean(axis=1), audio_c.mean(axis=1)
    out = []
    for t in range(12):
        out.append((t, float(np.corrcoef(np.roll(sp, t), ap)[0, 1])))
    return sorted(out, key=lambda x: -x[1])


def stretch(C: np.ndarray, factor: float) -> np.ndarray:
    """Resample a chroma sequence in time: factor 2 = the performance is twice as slow as the score's marking."""
    n = max(2, int(round(C.shape[1] * factor)))
    src = np.linspace(0, C.shape[1] - 1, n)
    out = np.stack([np.interp(src, np.arange(C.shape[1]), C[k]) for k in range(C.shape[0])])
    return out / (np.linalg.norm(out, axis=0, keepdims=True) + 1e-9)


def best_tempo_factor(seg_c: np.ndarray, score_c: np.ndarray) -> tuple[float, list]:
    """Printed tempo markings are often far from what a choir actually sings. Try a range of overall tempo
    ratios on the longest section and keep the one whose alignment fits best."""
    trials = []
    for f in np.geomspace(0.5, 3.0, 15):
        r = align_segment(seg_c, stretch(score_c, f))
        trials.append((float(f), r["cost"]))
    best = min(trials, key=lambda x: x[1])[0]
    return best, trials


def align_segment(seg_c: np.ndarray, score_c: np.ndarray) -> dict:
    """Subsequence DTW: the whole section (query) against the best-matching stretch of the score."""
    import librosa

    # Steps (1,1), (1,2), (2,1): the performance may run between half and double the score's tempo locally, but
    # cannot stall on one score position or skip through it, which is what made unconstrained warping wander.
    D, wp = librosa.sequence.dtw(X=seg_c, Y=score_c, metric="cosine", subseq=True,
                                 step_sizes_sigma=np.array([[1, 1], [1, 2], [2, 1]]), weights_add=np.array([0, 0, 0]),
                                 weights_mul=np.array([1, 1, 1]))
    wp = wp[::-1]
    cost = float(D[-1, wp[-1, 1]] / len(wp))
    return {"path": wp, "cost": cost, "score_start": int(wp[0, 1]), "score_end": int(wp[-1, 1])}


def warp_notes(parts: list[dict], path: np.ndarray, seg_start: float, roles: set[str], transpose: int, flag: bool,
               seg_end: float | None = None, score_len: float | None = None, edge: float = 0.0) -> list[tuple[str, dict]]:
    """Map every note whose onset lies in the matched stretch of the score to recording time. Subsequence
    matching tends to trim a beat or two at either end; when the match starts/ends within `edge` seconds of the
    score's start/end, the score edge is pinned to the section edge so the first and last notes are kept."""
    st = path[:, 1] * FRAME  # score time
    at = seg_start + path[:, 0] * FRAME  # audio time
    order = np.argsort(st, kind="stable")
    st, at = st[order], at[order]
    if edge and score_len:
        b0 = np.floor(st[0] / score_len) * score_len  # start of the pass the match begins in
        if 0 < st[0] - b0 <= edge:
            st, at = np.concatenate([[b0], st]), np.concatenate([[seg_start], at])
        b1 = np.ceil(st[-1] / score_len) * score_len
        if seg_end and 0 < b1 - st[-1] <= edge:
            st, at = np.concatenate([st, [b1]]), np.concatenate([at, [seg_end]])
    lo, hi = st[0], st[-1] + FRAME
    out = []
    for p in parts:
        if p.get("role") not in roles:
            continue
        for nt in p["notes"]:
            if not (lo <= nt["start"] < hi):
                continue
            a = float(np.interp(nt["start"], st, at))
            b = float(np.interp(min(nt["end"], hi), st, at))
            if b - a < 0.05:
                b = a + max(0.05, (nt["end"] - nt["start"]) * 0.5)
            out.append((p["id"], {**nt, "id": uuid.uuid4().hex[:10], "start": round(a, 3), "end": round(b, 3),
                                  "pitch": int(nt["pitch"] + transpose), "confidence": None, "uncertain": flag, "verified": False}))
    return out


def repeat_period(ac: np.ndarray, lo_s: float = 15.0, hi_s: float = 110.0) -> tuple[float | None, float]:
    """How long one pass of the music lasts, from the lag at which the recording's harmony best repeats itself
    (every verse of a hymn has the same music). Returns (seconds, strength); strength < 0.02 means no clear repeat."""
    from scipy.ndimage import uniform_filter1d

    Cs = uniform_filter1d(ac, size=20, axis=1)
    Cs = Cs / (np.linalg.norm(Cs, axis=0, keepdims=True) + 1e-9)
    n = Cs.shape[1]
    lags = np.arange(int(lo_s / FRAME), min(int(hi_s / FRAME), n // 2))
    if len(lags) < 20:
        return None, 0.0
    prof = np.array([np.mean(np.sum(Cs[:, : n - l] * Cs[:, l:], axis=0)) for l in lags])
    detr = prof - uniform_filter1d(prof, size=150, mode="nearest")
    i = int(np.argmax(detr))
    return float(lags[i] * FRAME), float(detr[i])


NAMES_ = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def recording_final_chord(y: np.ndarray, sr: int) -> dict | None:
    """Hymns end on their home chord. Read the last held chord of the recording (major/minor triad fit over the
    final seconds of music) and the chord before it. Returns root pitch class, quality, and a confidence margin."""
    import librosa

    C = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
    t = librosa.times_like(C, sr=sr, hop_length=512)
    rms = librosa.feature.rms(y=y, hop_length=512)[0][: len(t)]
    loud = t[rms > rms.max() * 0.05]
    if len(loud) == 0:
        return None
    end = float(loud.max())

    def triads(a: float, b: float):
        m_ = (t >= a) & (t < b)
        if not m_.any():
            return []
        v = C[:, m_].mean(axis=1)
        v = v / (np.linalg.norm(v) + 1e-9)
        out = []
        for r in range(12):
            for q, iv in (("major", (0, 4, 7)), ("minor", (0, 3, 7))):
                tpl = np.zeros(12)
                tpl[[(r + i) % 12 for i in iv]] = 1
                out.append((float(v @ (tpl / np.linalg.norm(tpl))), r, q))
        return sorted(out, reverse=True)

    fin, before = triads(end - 6, end - 0.5), triads(end - 12, end - 6)
    if not fin:
        return None
    root, qual = fin[0][1], fin[0][2]
    dominant_before = bool(before) and before[0][1] == (root + 7) % 12  # V -> I: the strongest sign of a home key
    return {"root": root, "quality": qual, "name": f"{NAMES_[root]} {qual}", "margin": round(fin[0][0] - fin[1][0], 3),
            "preceded_by": f"{NAMES_[before[0][1]]} {before[0][2]}" if before else None, "perfect_cadence": dominant_before, "at_s": round(end, 1)}


def score_final_root(parts: list[dict]) -> int | None:
    """Root of the score's final chord = its lowest note at the last onset."""
    notes = [n for p in parts for n in p["notes"]]
    if not notes:
        return None
    last = max(n["start"] for n in notes)
    final = [n for n in notes if n["end"] > last - 0.01 and n["start"] <= last + 0.01]
    return min(final, key=lambda n: n["pitch"])["pitch"] % 12 if final else None


def sung_pitch_evidence(ctx: Ctx) -> tuple[np.ndarray, np.ndarray] | None:
    """Frames where the separated voices have a clear pitch: (times, pitch classes). Used to judge candidate
    alignments against what was actually sung. None when there is no real voice stem."""
    pt = ctx.latest_asset("analysis", what="pitch_track")
    if not pt or not ctx.vocal_stem():
        return None
    z = np.load(ctx.path(pt))
    if not bool(z["on_vocal_stem"]):
        return None
    ok = (z["f0"] > 0) & (z["prob"] > 0.5)
    if ok.sum() < 50:
        return None
    return z["times"][ok], (np.round(69 + 12 * np.log2(z["f0"][ok] / 440.0)).astype(int) % 12)


def agreement(notes: list[dict], ev: tuple[np.ndarray, np.ndarray], offset: float = 0.0, pc_shift: int = 0) -> float:
    """Share of sung-pitch frames whose pitch class belongs to a score note sounding at that moment."""
    if not notes:
        return 0.0
    st = np.array([n["start"] for n in notes]) + offset
    en = np.array([n["end"] for n in notes]) + offset
    pc = (np.array([n["pitch"] for n in notes]) + pc_shift) % 12
    order = np.argsort(st)
    st, en, pc = st[order], en[order], pc[order]
    hit = tot = 0
    maxlen = float(np.max(en - st)) if len(st) else 0
    for t, q in zip(*ev):
        a = np.searchsorted(st, t - maxlen)
        b = np.searchsorted(st, t, side="right")
        live = pc[a:b][en[a:b] > t]
        if live.size:
            tot += 1
            hit += int(q in live)
    return hit / tot if tot else 0.0


def section_kind_at(sections: list[dict], t: float) -> str:
    for s_ in sections:
        if s_["start"] - 0.5 <= t < s_["end"]:
            return s_.get("kind", "unknown")
    return "unknown"


def global_candidate(ac: np.ndarray, parts: list[dict], sdur: float, k: int, period: float, transpose: int, dur: float, sections: list[dict]) -> dict:
    """Align the whole recording to the score tiled end to end (pass after pass), at the tempo implied by the
    measured repeat period. Monotone warping naturally covers an introduction that plays the last line, then
    verse after verse, then an interlude: each simply maps to the next stretch of the tiled hymn."""
    import librosa

    pass_len = sdur / k
    fac = period / pass_len
    tiles = int(np.ceil((dur / period + 2) / k)) + 1
    tiled = [{**p, "notes": [{**n, "start": n["start"] + j * sdur, "end": n["end"] + j * sdur} for j in range(tiles) for n in p["notes"]]} for p in parts]
    C = stretch(score_chroma(tiled, sdur * tiles, transpose), fac)
    D, wp = librosa.sequence.dtw(X=ac, Y=C, metric="cosine", subseq=True, step_sizes_sigma=np.array([[1, 1], [1, 2], [2, 1]]),
                                 weights_add=np.zeros(3), weights_mul=np.ones(3))
    wp = wp[::-1]
    cost = float(D[-1, wp[-1, 1]] / len(wp))
    path = np.column_stack([wp[:, 0], wp[:, 1] / fac])
    shift = transpose if transpose <= 6 else transpose - 12
    placed = []
    for pid, note in warp_notes(tiled, path, 0.0, {"vocal", "instrument"}, shift, False):
        role = next(p.get("role") for p in parts if p["id"] == pid)
        if role == "vocal" and section_kind_at(sections, note["start"]) == "instrumental":
            continue  # voices only where the timeline says voices
        placed.append((pid, note))
    return {"cost": cost, "factor": fac, "k": k, "transpose": transpose, "shift": shift, "placed": placed, "path": path, "pass_len": pass_len}


@agent("alignment", 3, "Score Alignment", "Places a score you trust onto the recording's own timing, in the key actually sung.")
def run(ctx: Ctx) -> None:
    work = ctx.working()
    sid = ctx.params.get("score_version_id")
    sv = ctx.db.get(m.ScoreVersion, sid) if sid else None
    if not sv:
        sv = ctx.db.query(m.ScoreVersion).filter(m.ScoreVersion.project_id == ctx.project.id, m.ScoreVersion.origin.like("import%")) \
            .order_by(m.ScoreVersion.version.desc()).first()
    if not sv:
        raise Blocked("There is no imported score to align. Import the score the choir sang from (MusicXML or MIDI) on the Score tab first.")
    score = sv.data
    if not any(p["notes"] for p in score["parts"]):
        raise Blocked("The imported score contains no notes.")
    sec_row = ctx.head(m.SectionSet, "sections")
    sections = [dict(s_) for s_ in ((sec_row.data.get("sections") if sec_row else None) or [])]
    ctx.progress(0.05, "Loading the recording")
    y, sr = aio.load_mono(ctx.path(work), mu.SR)
    dur = len(y) / sr
    if not sections:
        sections = [{"start": 0.0, "end": dur, "label": "Whole recording", "kind": "unknown"}]
    merged: list[dict] = []
    for s_ in sections:  # consecutive sections of the same kind form one block (verse + refrain, or detector fragments)
        if merged and merged[-1].get("kind") == s_.get("kind") and abs(merged[-1]["end"] - s_["start"]) < 0.5:
            merged[-1]["end"] = s_["end"]
            merged[-1]["label"] = merged[-1]["label"].split(" … ")[0] + " … " + s_["label"]
        else:
            merged.append(dict(s_))
    sections = merged

    parts = copy.deepcopy(score["parts"])
    has_inst = any(p.get("role") == "instrument" and p["notes"] for p in parts)
    made_accomp = False
    if not has_inst and ctx.params.get("accompaniment", True):
        # Voices-only score, but the recording has an accompaniment. In hymn practice the organ plays the same
        # four-part harmony as the choir, including introductions and interludes. An ASSUMPTION, labelled as such.
        seen, acc = set(), []
        for p in parts:
            if p.get("role") == "vocal":
                for n in p["notes"]:
                    key = (round(n["start"], 3), n["pitch"])
                    if key not in seen:
                        seen.add(key)
                        acc.append({**n, "lyric": ""})
        parts.append({"id": uuid.uuid4().hex[:10], "name": "Accompaniment (assumed: plays the hymn harmony)", "role": "instrument",
                      "voice": "accompaniment", "program": 19, "source": "derived-from-score", "notes": sorted(acc, key=lambda n: n["start"])})
        made_accomp = True
    sdur = max(float(score.get("duration") or 0), max(n["end"] for p in parts for n in p["notes"]))

    ctx.progress(0.12, "Measuring the recording's harmony and how long one verse lasts")
    ac = audio_chroma(y, sr)
    ranked = best_transposition(score_chroma(parts, sdur), ac)
    ev = sung_pitch_evidence(ctx)
    period, strength = repeat_period(ac)
    forced = ctx.params.get("transpose")
    cadence = recording_final_chord(y, sr)
    sroot = score_final_root(parts)
    cadence_t = None
    if forced is None and cadence and sroot is not None and (cadence["perfect_cadence"] or cadence["margin"] >= 0.08):
        cadence_t = (cadence["root"] - sroot) % 12
        forced = cadence_t  # the final cadence fixes the key; alignment then only has to find the timing
    diag: dict = {"final_chord_of_recording": cadence, "score_final_root": NAMES_[sroot] if sroot is not None else None,
                  "key_from_final_cadence": cadence_t is not None,"repeat_period_s": round(period, 1) if period else None, "repeat_strength": round(strength, 3),
                  "judged_by": "agreement with the pitches the separated voices sing" if ev is not None else "harmonic match cost (no voice stem)"}

    def run_whole():
        new_parts = {p["id"]: {**copy.deepcopy({k_: v for k_, v in p.items() if k_ != "notes"}), "notes": [],
                               "source": p.get("source") if p.get("source") == "derived-from-score" else f"aligned:{sv.origin}"} for p in parts}
        report: list[dict] = []
        diag: dict = {}
        keys = [int(forced) % 12] if forced is not None else ([t for t in range(12)] if ev is not None else [t for t, _ in ranked[:3]])
        cands = []
        combos = [(t, k) for t in keys for k in (1, 2, 3) if 0.4 <= period / (sdur / k) <= 3.5]
        for i, (t, k) in enumerate(combos):
            ctx.progress(0.15 + 0.6 * i / max(1, len(combos)), f"Trying key {_shift_text(t if t <= 6 else t - 12)}, {k} pass(es) per file ({i + 1} of {len(combos)})")
            c = global_candidate(ac, parts, sdur, k, period, t, dur, sections)
            # judged on every placed note sounding at that moment: the voices may be singing any of the parts
            vocal_notes = [n for pid, n in c["placed"] if next(p.get("source") for p in parts if p["id"] == pid) != "derived-from-score"]
            c["score"] = agreement(vocal_notes, ev) if ev is not None else -c["cost"]
            c["vocal_notes"] = vocal_notes
            cands.append(c)
        if not cands:
            raise Blocked(f"The verse length measured in the recording ({period:.0f} s) is incompatible with this score's length; align section by section instead.")
        best = max(cands, key=lambda c: c["score"])
        shift, factor = best["shift"], best["factor"]
        for pid, note in best["placed"]:
            new_parts[pid]["notes"].append(note)
        if ev is not None:
            chance = float(np.mean([agreement(best["vocal_notes"], ev, offset=o) for o in (-9.0, -5.0, 6.0, 11.0)]))
            by_key: dict[int, float] = {}
            for c in cands:
                by_key[c["shift"]] = max(by_key.get(c["shift"], 0), c["score"])
            ordered = sorted(by_key.items(), key=lambda x: -x[1])
            diag.update({"sung_pitch_agreement": round(best["score"], 3), "chance_level": round(chance, 3),
                         "key_ranking": [{"semitones": s_, "agreement": round(v, 3)} for s_, v in ordered[:4]],
                         "key_margin": round(ordered[0][1] - ordered[1][1], 3) if len(ordered) > 1 else None})
        diag.update({"tempo_factor": round(factor, 3), "passes_in_file": best["k"], "match_cost": round(best["cost"], 3)})
        # per-section summary from the global path
        path = best["path"]
        for s_ in sections:
            m_ = (path[:, 0] * FRAME >= s_["start"]) & (path[:, 0] * FRAME < s_["end"])
            if m_.any():
                st_ = path[m_, 1] * FRAME
                report.append({"label": s_["label"], "start": s_["start"], "end": s_["end"], "kind": s_.get("kind"), "status": "aligned",
                               "score_from_s": round(float(st_[0] % best["pass_len"]), 1), "score_to_s": round(float(st_[-1] % best["pass_len"]), 1),
                               "passes_of_score": round(float((st_[-1] - st_[0]) / best["pass_len"]), 2)})
        return {"new_parts": new_parts, "report": report, "shift": shift, "factor": factor, "diag": diag}

    def run_sections():
        new_parts = {p["id"]: {**copy.deepcopy({k_: v for k_, v in p.items() if k_ != "notes"}), "notes": [],
                               "source": p.get("source") if p.get("source") == "derived-from-score" else f"aligned:{sv.origin}"} for p in parts}
        report: list[dict] = []
        diag: dict = {}
        t = int(forced) % 12 if forced is not None else ranked[0][0]
        shift = t if t <= 6 else t - 12
        TILES = 3
        tiled = [{**p, "notes": [{**n, "start": n["start"] + j * sdur, "end": n["end"] + j * sdur} for j in range(TILES) for n in p["notes"]]} for p in parts]
        sc = score_chroma(tiled, sdur * TILES, t)
        longest = max(sections, key=lambda s_: (s_.get("kind") != "instrumental", s_["end"] - s_["start"]))
        l0, l1 = int(longest["start"] / FRAME), min(ac.shape[1], int(longest["end"] / FRAME))
        factor = float(ctx.params.get("tempo_factor") or best_tempo_factor(ac[:, l0:l1], sc)[0])
        sc = stretch(sc, factor)
        costs = []
        for i, s_ in enumerate(sections):
            ctx.progress(0.25 + 0.5 * i / len(sections), f"Aligning '{s_['label']}' ({s_['start']:.0f}–{s_['end']:.0f}s)")
            a0, a1 = int(s_["start"] / FRAME), min(ac.shape[1], max(int(s_["start"] / FRAME) + 2, int(s_["end"] / FRAME)))
            seg = ac[:, a0:a1]
            if seg.shape[1] < 10:
                report.append({**{k_: s_[k_] for k_ in ("label", "start", "end")}, "status": "skipped", "reason": "too short to align"})
                continue
            kind = s_.get("kind", "unknown")
            roles = {"instrument"} if kind == "instrumental" else {"vocal", "instrument"}
            res = align_segment(seg, sc)
            costs.append(res["cost"])
            pth = np.column_stack([res["path"][:, 0], res["path"][:, 1] / factor])
            report.append({"label": s_["label"], "start": s_["start"], "end": s_["end"], "kind": kind, "match_cost": round(res["cost"], 3),
                           "_path": pth, "_roles": roles})
        if not costs:
            raise Blocked("No section was long enough to align.")
        med = float(np.median(costs))
        sbeat = 60.0 / float(score.get("tempo_bpm") or 90.0)
        for r in report:
            if "_path" not in r:
                continue
            poor = r["match_cost"] > max(0.35, med * 1.6)
            r["status"] = "poor match" if poor else "aligned"
            pth, roles = r.pop("_path"), r.pop("_roles")
            st_ = pth[:, 1] * FRAME
            r.update({"score_from_s": round(float(st_.min() % sdur), 1), "score_to_s": round(float(st_.max() % sdur), 1), "passes_of_score": round(float((st_.max() - st_.min()) / sdur), 2)})
            for pid, note in warp_notes(tiled, pth, r["start"], roles, shift, poor, seg_end=r["end"], score_len=sdur, edge=2 * sbeat):
                new_parts[pid]["notes"].append(note)
        diag.update({"tempo_factor": round(factor, 3), "transposition_ranking": [{"semitones": (a if a <= 6 else a - 12), "score": round(b, 3)} for a, b in ranked[:4]]})

        return {"new_parts": new_parts, "report": report, "shift": shift, "factor": factor, "diag": diag}

    def fit(res: dict) -> float:
        """Method-independent judge. With a voice stem: agreement with the sung pitches. Without one: mean cosine
        similarity between the recording's harmony and the harmony of the placed notes, frame by frame."""
        notes_all = [n for p_ in res["new_parts"].values() for n in p_["notes"]]
        if ev is not None:
            scored = [n for p_ in res["new_parts"].values() if p_.get("source") != "derived-from-score" for n in p_["notes"]] or notes_all
            return agreement(scored, ev)
        if not notes_all:
            return 0.0
        C = score_chroma([{"role": "x", "notes": notes_all}], dur)[:, : ac.shape[1]]
        A = ac[:, : C.shape[1]]
        live = np.zeros(C.shape[1], bool)
        for n in notes_all:
            live[int(n["start"] / FRAME): max(int(n["start"] / FRAME) + 1, int(n["end"] / FRAME))] = True
        harmony = float(np.mean(np.sum(A[:, live] * C[:, live], axis=0))) * float(np.mean(live))
        # Harmony alone cannot tell apart placements shifted by a repeated chord pattern; rhythm can: do the placed
        # note starts fall on attacks heard in the recording? (allowing the +-0.4 s lag that refinement removes)
        starts = np.unique(np.round([n["start"] for n in notes_all], 2))
        best_rhythm = 0.0
        for lag in np.arange(-0.4, 0.41, 0.05):
            idx = np.round((starts + lag) * onset_fps).astype(int)
            idx = idx[(idx >= 0) & (idx < len(onset_env))]
            if len(idx):
                best_rhythm = max(best_rhythm, float(np.mean(onset_env_max[idx])))
        return harmony * best_rhythm

    import librosa
    from scipy.ndimage import maximum_filter1d

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=256)
    onset_env = onset_env / (onset_env.max() + 1e-9)
    onset_fps = sr / 256
    onset_env_max = maximum_filter1d(onset_env, size=5)  # +-2 frames (about 0.05 s)
    results = {}
    want = ctx.params.get("method")
    # Evidence order: (1) with a voice stem, the sung pitches judge both strategies; (2) without one, a section
    # timeline the user has checked is better information than automatic repeat detection, so it is used as is.
    verified = bool(sec_row and (sec_row.origin == "user-edit" or sec_row.verification == "approved"))
    skip_whole = want is None and verified and ev is None
    if want in (None, "whole") and period and strength >= 0.02 and not skip_whole:
        try:
            results["whole"] = run_whole()
        except Blocked:
            if want == "whole":
                raise
    if want in (None, "sections") or not results:
        results["sections"] = run_sections()
    for k_, r_ in results.items():
        r_["fit"] = fit(r_)
    method = max(results, key=lambda k_: results[k_]["fit"])
    # Near-tie: prefer the whole-recording alignment. It must explain the performance as consecutive passes of the
    # hymn, whereas section-by-section matching may pick whichever fragment of the score flatters each section.
    if "whole" in results and method == "sections" and results["whole"]["fit"] >= results["sections"]["fit"] - 0.03:
        method = "whole"
    chosen = results[method]
    new_parts, report, shift, factor = chosen["new_parts"], chosen["report"], chosen["shift"], chosen["factor"]
    diag.update(chosen["diag"])
    diag["method"] = method
    diag["method_fit"] = {k_: round(r_["fit"], 3) for k_, r_ in results.items()}
    diag["sections_verified_by_user"] = verified

    snapped, tb0 = refine_onsets(ctx, y, sr, new_parts, report)
    placed = sum(len(p["notes"]) for p in new_parts.values())
    data = validate_score({**{k_: v for k_, v in score.items() if k_ not in ("parts", "beats", "duration", "warnings", "chords")},
                           "parts": list(new_parts.values()), "beats": tb0["beats"], "tempo_bpm": tb0["tempo_bpm"], "duration": dur,
                           "downbeat_offset": tb0["downbeat_offset"], "chords": [], "key": _transpose_key(score.get("key"), shift),
                           "warnings": [f"Aligned from '{sv.note or sv.origin}' (score v{sv.version}), transposed {_shift_text(shift)}. "
                                        "Every note's pitch comes from the imported score; its timing comes from matching the recording. Check by ear, section by section."]})
    nv = new_version(ctx.db, ctx.project, m.ScoreVersion, "score", data, origin=f"aligned:{sv.origin}",
                     note=f"Score v{sv.version} aligned to the recording, transposed {_shift_text(shift)}",
                     provenance={"job_id": ctx.job.id, "from_score_version": sv.id, "transpose": shift, "notes_snapped_to_attacks": snapped,
                                 "diagnostics": diag, "sections": report})
    inv = gates.invalidate(ctx.db, ctx.project, "score", "The aligned score became the current score.")
    ctx.db.commit()

    warnings: list[str] = []
    if diag.get("key_margin") is not None and diag["key_margin"] < 0.02:
        alt = ", ".join(_shift_text(k_["semitones"]) for k_ in diag["key_ranking"][1:3])
        warnings.append(f"The key is not clear-cut: the next candidates ({alt}) fit almost as well. If it sounds out of tune against the original, align again with another key.")
    if made_accomp:
        warnings.append("The imported score has voices only. An accompaniment track was added that plays the same four-part harmony, including in "
                        "introductions and interludes, because that is normal hymn practice. This is an assumption: switch it off on the Arrangement tab if the instruments played something else.")
    if method == "sections" and any(r.get("status") == "poor match" for r in report):
        warnings.append("Some sections matched poorly and their notes are flagged uncertain.")
    if diag.get("sung_pitch_agreement") is not None:
        conf = (f"The pitches the choir sings agree with the aligned score {diag['sung_pitch_agreement']:.0%} of the time, against {diag['chance_level']:.0%} for a "
                "deliberately misplaced copy of the same notes. ")
    else:
        conf = ""
    ctx.output(
        f"Placed {placed} notes from the imported score onto the recording, transposed {_shift_text(shift)}"
        + (f", treating the performance as passes of about {period:.0f} s each" if method == "whole" else ", section by section") + ". " + conf
        + "Pitches come from the score; timing is an estimate to check by ear.",
        {"score_version_id": nv.id, "transpose": shift, "diagnostics": diag, "sections": report, "notes_placed": placed, "invalidation": inv, "warnings": warnings,
         "uncertainty": "Alignment assumes the choir performed what the imported score says. Where they sang something else (another harmony, a descant, "
                        "a different verse order) the notes will be the score's, not the choir's. Agreement figures are measured against an automatic pitch track "
                        "of the voices, which itself makes errors, so even a perfect alignment would not reach 100%."},
    )


def refine_onsets(ctx: Ctx, y: np.ndarray, sr: int, new_parts: dict, report: list[dict]) -> tuple[int, dict]:
    """Chroma alignment lags note attacks (measured +0.1..0.3 s on the test fixture). Per section, slide the
    placed onsets together (-0.4..+0.4 s) to where they best coincide with the onset-strength curve, then snap
    each note to the nearest detected attack within a quarter of a beat."""
    import librosa

    ctx.progress(0.9, "Refining note starts against attacks heard in the recording")
    tb0 = mu.tempo_and_beats(y, sr)
    beat = 60.0 / (tb0["tempo_bpm"] or 90.0)
    hop = 256
    sources = {"instrument": y}
    vstem = ctx.vocal_stem()
    if vstem:
        sources["vocal"], _ = aio.load_mono(ctx.path(vstem), mu.SR)
    env, ons = {}, {}
    for k_, sig in sources.items():
        e = librosa.onset.onset_strength(y=sig, sr=sr, hop_length=hop)
        env[k_] = e / (e.max() + 1e-9)
        ons[k_] = librosa.onset.onset_detect(onset_envelope=e, sr=sr, hop_length=hop, units="time", backtrack=False)
    env.setdefault("vocal", env["instrument"]); ons.setdefault("vocal", ons["instrument"])
    fps = sr / hop
    lags = np.arange(-0.4, 0.401, 0.01)
    for r in report:
        if r.get("status") not in ("aligned", "poor match"):
            continue
        notes = [(part.get("role"), nt) for part in new_parts.values() for nt in part["notes"] if r["start"] <= nt["start"] < r["end"]]
        if len(notes) < 4:
            continue
        best, best_score = 0.0, -1.0
        for lag in lags:
            sc_ = 0.0
            for role, nt in notes:
                e = env.get(role, env["instrument"])
                i = int(round((nt["start"] + lag) * fps))
                if 0 <= i < len(e):
                    sc_ += float(e[max(0, i - 1): i + 2].max())
            if sc_ > best_score:
                best, best_score = float(lag), sc_
        for _, nt in notes:
            nt["start"] = round(max(0.0, nt["start"] + best), 3)
            nt["end"] = round(nt["end"] + best, 3)
        r["offset_correction_s"] = round(best, 2)
    snapped = 0
    win = min(0.15, beat / 4)
    for part in new_parts.values():
        o = np.asarray(ons.get(part.get("role"), ons["instrument"]))
        if not len(o):
            continue
        for nt in part["notes"]:
            j = int(np.argmin(np.abs(o - nt["start"])))
            d = float(o[j]) - nt["start"]
            if abs(d) <= win:
                nt["start"] = round(nt["start"] + d, 3)
                nt["end"] = round(max(nt["start"] + 0.05, nt["end"]), 3)
                snapped += 1
    return snapped, tb0


def _shift_text(s: int) -> str:
    return "not at all (same key)" if s == 0 else f"{'up' if s > 0 else 'down'} {abs(s)} semitone{'s' if abs(s) != 1 else ''}"


NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def _transpose_key(key: str | None, s: int) -> str | None:
    if not key:
        return key
    try:
        tonic, mode = key.split()[:2]
        alias = {"Db": "C#", "D#": "Eb", "Gb": "F#", "G#": "Ab", "A#": "Bb", "C-": "B", "D-": "C#", "E-": "Eb", "G-": "F#", "A-": "Ab", "B-": "Bb"}
        i = NAMES.index(alias.get(tonic, tonic))
        return f"{NAMES[(i + s) % 12]} {mode}"
    except Exception:
        return key
