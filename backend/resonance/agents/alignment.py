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
    if edge and st[0] > 0 and st[0] <= edge:
        st, at = np.concatenate([[0.0], st]), np.concatenate([[seg_start], at])
    if edge and score_len and seg_end and st[-1] < score_len and score_len - st[-1] <= edge:
        st, at = np.concatenate([st, [score_len]]), np.concatenate([at, [seg_end]])
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


@agent("alignment", 3, "Score Alignment", "Places a score you trust onto the recording's own timing, section by section, in the key actually sung.")
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
    sections = [s for s in ((sec_row.data.get("sections") if sec_row else None) or [])]
    ctx.progress(0.05, "Loading the recording")
    y, sr = aio.load_mono(ctx.path(work), mu.SR)
    dur = len(y) / sr
    if not sections:
        sections = [{"start": 0.0, "end": dur, "label": "Whole recording", "kind": "unknown"}]
    ctx.progress(0.2, "Computing harmonic profiles of the recording and the score")
    ac = audio_chroma(y, sr)
    sdur = max(n["end"] for p in score["parts"] for n in p["notes"])
    base = score_chroma(score["parts"], sdur)
    ranked = best_transposition(base, ac)
    forced = ctx.params.get("transpose")
    t = int(forced) % 12 if forced is not None else ranked[0][0]
    shift = t if t <= 6 else t - 12  # smallest move: e.g. 'down 2' rather than 'up 10'
    sc = score_chroma(score["parts"], sdur, t)
    vocal_roles, inst_roles = {"vocal", "instrument"}, {"instrument"}
    has_inst = any(p.get("role") == "instrument" and p["notes"] for p in score["parts"])

    new_parts = {p["id"]: {**copy.deepcopy({k: v for k, v in p.items() if k != "notes"}), "notes": [], "source": f"aligned:{sv.origin}"} for p in score["parts"]}
    report, costs = [], []
    for i, s in enumerate(sections):
        ctx.progress(0.25 + 0.65 * i / len(sections), f"Aligning '{s['label']}' ({s['start']:.0f}–{s['end']:.0f}s)")
        a0, a1 = int(s["start"] / FRAME), min(ac.shape[1], max(int(s["start"] / FRAME) + 2, int(s["end"] / FRAME)))
        seg = ac[:, a0:a1]
        if seg.shape[1] < 10:
            report.append({**{k: s[k] for k in ("label", "start", "end")}, "status": "skipped", "reason": "too short to align"})
            continue
        kind = s.get("kind", "unknown")
        roles = inst_roles if kind == "instrumental" else vocal_roles
        if kind == "instrumental" and not has_inst:
            # a voice-only score: an instrumental passage most often plays the tune, so align it but place no voices
            roles = set()
        res = align_segment(seg, sc)
        costs.append(res["cost"])
        report.append({"label": s["label"], "start": s["start"], "end": s["end"], "kind": kind,
                       "score_from_s": round(res["score_start"] * FRAME, 2), "score_to_s": round(res["score_end"] * FRAME, 2),
                       "match_cost": round(res["cost"], 3), "path": res["path"], "roles": sorted(roles)})
    if not costs:
        raise Blocked("No section was long enough to align.")
    med = float(np.median(costs))
    placed = 0
    for r in report:
        if "path" not in r:
            continue
        poor = r["match_cost"] > max(0.35, med * 1.6)
        r["status"] = "poor match" if poor else "aligned"
        sbeat = 60.0 / float(score.get("tempo_bpm") or 90.0)
        for pid, note in warp_notes(score["parts"], r.pop("path"), r["start"], set(r.pop("roles")), shift, poor,
                                    seg_end=r["end"], score_len=sdur, edge=2 * sbeat):
            new_parts[pid]["notes"].append(note)
            placed += 1
    # Local refinement. Chroma alignment finds the right place within a fraction of a beat but lags note attacks
    # (measured +0.1..0.3 s on the test fixture). Two data-driven steps, no fixed correction:
    #  1. per section, slide all its placed onsets together (-0.4..+0.4 s) to where they best coincide with the
    #     recording's onset-strength curve;
    #  2. snap each note to the nearest detected attack within a quarter of a beat.
    ctx.progress(0.92, "Refining note starts against attacks heard in the recording")
    import librosa

    tb0 = mu.tempo_and_beats(y, sr)
    beat = 60.0 / (tb0["tempo_bpm"] or 90.0)
    hop = 256
    sources = {"instrument": y}
    vstem = ctx.vocal_stem()
    if vstem:
        sources["vocal"], _ = aio.load_mono(ctx.path(vstem), mu.SR)
    env, ons = {}, {}
    for k, sig in sources.items():
        e = librosa.onset.onset_strength(y=sig, sr=sr, hop_length=hop)
        env[k] = e / (e.max() + 1e-9)
        ons[k] = librosa.onset.onset_detect(onset_envelope=e, sr=sr, hop_length=hop, units="time", backtrack=False)
    env.setdefault("vocal", env["instrument"]); ons.setdefault("vocal", ons["instrument"])
    fps = sr / hop
    lags = np.arange(-0.4, 0.401, 0.01)
    shifts = []
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
            nt["start"] = round(nt["start"] + best, 3)
            nt["end"] = round(nt["end"] + best, 3)
        r["offset_correction_s"] = round(best, 2)
        shifts.append(best)
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
    # beat grid: warp the score's beats through the first well-matched section's tempo is not general; derive from tempo estimate instead
    tb = tb0
    data = validate_score({**{k: v for k, v in score.items() if k not in ("parts", "beats", "duration", "warnings", "chords")},
                           "parts": list(new_parts.values()), "beats": tb["beats"], "tempo_bpm": tb["tempo_bpm"], "duration": dur,
                           "downbeat_offset": tb["downbeat_offset"], "chords": [],
                           "key": _transpose_key(score.get("key"), shift),
                           "warnings": [f"Aligned from '{sv.note or sv.origin}' (score v{sv.version}), transposed {_shift_text(shift)}. "
                                        "Timing comes from matching harmony section by section; notes inside a well-matched section follow the performers' tempo. "
                                        "Check each section by ear, especially any marked 'poor match'."]})
    nv = new_version(ctx.db, ctx.project, m.ScoreVersion, "score", data, origin=f"aligned:{sv.origin}",
                     note=f"Score v{sv.version} aligned to the recording, transposed {_shift_text(shift)}",
                     provenance={"job_id": ctx.job.id, "from_score_version": sv.id, "transpose": shift,
                                 "transposition_ranking": [{"semitones": (a if a <= 6 else a - 12), "score": round(b, 3)} for a, b in ranked[:4]],
                                 "method": "chroma subsequence DTW per section, then onset snapping", "notes_snapped_to_attacks": snapped, "sections": report})
    inv = gates.invalidate(ctx.db, ctx.project, "score", "The aligned score became the current score.")
    ctx.db.commit()
    poor = [r["label"] for r in report if r.get("status") == "poor match"]
    margin = ranked[0][1] - ranked[1][1]
    ctx.output(
        f"Placed {placed} notes from the imported score onto the recording across {len([r for r in report if r.get('status') in ('aligned', 'poor match')])} sections, "
        f"transposed {_shift_text(shift)}." + (f" {len(poor)} section(s) matched poorly and are flagged: {', '.join(poor[:5])}." if poor else " Every section matched well enough to place notes."),
        {"score_version_id": nv.id, "transpose": shift, "transposition_margin": round(margin, 3),
         "transposition_ranking": nv.provenance["transposition_ranking"], "sections": report, "notes_placed": placed, "invalidation": inv,
         "warnings": ([f"The key match is not clear-cut (margin {margin:.2f}). If the result sounds out of tune with the original, choose a different transposition and align again."] if margin < 0.05 else [])
         + (["Sections marked 'instrumental' received only instrumental parts; the imported score has none, so those passages are silent in the reconstruction."] if not has_inst and any(s.get('kind') == 'instrumental' for s in sections) else []),
         "uncertainty": "Alignment assumes the choir performed what the imported score says. Where they sang something different (another verse order, a descant, a changed harmony), "
                        "the notes will be the score's, not the choir's. The Fidelity Inspector and your ears are the check."},
    )


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
