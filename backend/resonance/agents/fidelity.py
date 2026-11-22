"""Agent 11: Fidelity Inspector.

Several independent checks, each reported separately with its own status. There is deliberately no single
'similarity score' and no check can return the word 'exact'.
"""
from __future__ import annotations

import uuid

import numpy as np
from sqlalchemy import select

from .. import models as m
from ..audio import io as aio
from ..music import analysis as mu
from ..score.model import pitch_name
from .base import Blocked, Ctx, agent


def compare_scores(ref: dict, other: dict, onset_tol: float = 0.08) -> dict:
    """Symbolic note comparison (all parts pooled): a note matches if same pitch and onset within tolerance.
    Standard transcription metrics: precision, recall, F1."""
    a = sorted(((n["start"], n["pitch"]) for p in ref.get("parts", []) for n in p["notes"]))
    b = sorted(((n["start"], n["pitch"]) for p in other.get("parts", []) for n in p["notes"]))
    used = [False] * len(b)
    matched, missing = 0, []
    bi = 0
    for (t, p) in a:
        while bi < len(b) and b[bi][0] < t - onset_tol:
            bi += 1
        hit = None
        j = bi
        while j < len(b) and b[j][0] <= t + onset_tol:
            if not used[j] and b[j][1] == p:
                hit = j
                break
            j += 1
        if hit is None:
            missing.append({"time": round(t, 3), "pitch": p})
        else:
            used[hit] = True
            matched += 1
    extra = [{"time": round(t, 3), "pitch": p} for (t, p), u in zip(b, used) if not u]
    prec = matched / len(b) if b else 0.0
    rec = matched / len(a) if a else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"reference_notes": len(a), "other_notes": len(b), "matched": matched, "precision": round(prec, 3),
            "recall": round(rec, 3), "f1": round(f1, 3), "missing": missing, "extra": extra, "onset_tolerance_s": onset_tol}


def melody_vs_audio(notes: list[dict], times: np.ndarray, f0: np.ndarray, prob: np.ndarray) -> dict:
    """For each score melody note, measure the median pitch actually present in the ORIGINAL recording during
    the note. |diff| < 0.5 semitone = agrees; unvoiced = cannot be verified (not counted either way)."""
    rows, agree, octave, differ, unverifiable = [], 0, 0, 0, 0
    for n in notes:
        msk = (times >= n["start"]) & (times < n["end"]) & (f0 > 0) & (prob > 0.5)
        if msk.sum() < 3:
            unverifiable += 1
            continue
        midi = 69 + 12 * np.log2(np.median(f0[msk]) / 440.0)
        d = float(midi - n["pitch"])
        conf = float(np.clip(np.mean(prob[msk]), 0, 1))
        if abs(d) < 0.5:
            agree += 1
        elif abs(abs(d) - 12) < 0.5:
            octave += 1
            rows.append({"note": n, "heard": int(round(midi)), "diff": round(d, 2), "kind": "octave", "confidence": round(conf * 0.6, 2)})
        else:
            differ += 1
            rows.append({"note": n, "heard": int(round(midi)), "diff": round(d, 2), "kind": "pitch", "confidence": round(conf, 2)})
    checked = agree + octave + differ
    return {"checked": checked, "agree": agree, "octave_differences": octave, "differ": differ, "unverifiable": unverifiable,
            "agreement": round(agree / checked, 3) if checked else None, "rows": rows}


def _disc(type_: str, time: float | None, original: str, reconstructed: str, confidence: float | None, suggestion: str, fix: dict | None = None, severity: str = "review") -> dict:
    return {"id": uuid.uuid4().hex[:10], "type": type_, "time": None if time is None else round(float(time), 2), "original": original,
            "reconstructed": reconstructed, "confidence": confidence, "suggestion": suggestion, "fix": fix, "severity": severity, "status": "open"}


@agent("fidelity", 11, "Fidelity Inspector", "Compares the reconstruction with the original recording and lists every difference it can detect.")
def run(ctx: Ctx) -> None:
    rid = ctx.params.get("reconstruction_id")
    rec = ctx.db.get(m.Reconstruction, rid) if rid else ctx.db.execute(
        select(m.Reconstruction).where(m.Reconstruction.project_id == ctx.project.id, m.Reconstruction.status != "stale")
        .order_by(m.Reconstruction.version.desc())).scalars().first()
    if not rec or rec.status == "stale":
        raise Blocked("There is no up-to-date render to inspect. Render (and mix) the arrangement first.")
    recon_asset = ctx.db.get(m.Asset, rec.mix_asset_id or "")
    if not recon_asset:
        raise Blocked("The render has not been mixed yet; the inspector compares the mixed reconstruction with the original.")
    work = ctx.working()
    sv = ctx.db.get(m.ScoreVersion, rec.score_version_id) if rec.score_version_id else None
    sections_row = ctx.head(m.SectionSet, "sections")
    sections = (sections_row.data.get("sections") if sections_row else []) or []
    checks, disc = [], []

    def check(key: str, title: str, status: str, detail: str, **data):
        checks.append({"key": key, "title": title, "status": status, "detail": detail, "data": data})

    ctx.progress(0.05, "Loading both recordings")
    yo, sr = aio.load_mono(ctx.path(work), mu.SR)
    yr, _ = aio.load_mono(ctx.path(recon_asset), mu.SR)
    do, dr = len(yo) / sr, len(yr) / sr

    # 1 ---- duration / ending
    tail = dr - do
    check("duration", "Overall length", "pass" if abs(tail) < 6 else "warn",
          f"Original {do:.1f}s, reconstruction {dr:.1f}s. " + ("The extra length is reverb tail." if 0 < tail < 6 else ""), original_s=round(do, 2), reconstruction_s=round(dr, 2))
    if abs(tail) >= 6:
        disc.append(_disc("ending", min(do, dr), f"ends at {do:.1f}s", f"ends at {dr:.1f}s", 0.9, "Check that the final section of the score covers the whole ending of the recording."))

    # 2 ---- harmonic alignment via chroma DTW
    ctx.progress(0.2, "Aligning the two recordings (chroma dynamic time warping)")
    co, _ = mu.chroma_features(yo, sr)
    cr, _ = mu.chroma_features(yr, sr)
    hop_t = mu.HOP / sr
    step = max(1, int(round(0.2 / hop_t)))  # 0.2 s resolution keeps DTW memory small
    def pool(c):
        k = c.shape[1] // step
        return c[:, : k * step].reshape(12, k, step).mean(axis=2) if k else c
    po, pr = pool(co), pool(cr)
    al = mu.dtw_alignment(po, pr, hop_t * step)
    ref_t, oth_t = al.pop("ref_t"), al.pop("oth_t")
    exact_timeline = al["median_time_deviation_s"] < 0.25
    check("alignment", "Timing alignment", "pass" if al["median_time_deviation_s"] < 0.5 else "warn",
          f"Median timing offset between equivalent musical moments: {al['median_time_deviation_s']:.2f}s (largest {al['max_time_deviation_s']:.2f}s). "
          + ("The reconstruction follows the original's timeline closely, so A/B playback uses the same clock."
             if exact_timeline else "The two are NOT on the same clock; A/B playback uses the alignment map below."),
          **al, shared_timeline=exact_timeline)

    # 3 ---- per-section harmonic similarity
    ctx.progress(0.4, "Comparing harmony section by section")
    oi = np.clip((ref_t / (hop_t * step)).astype(int), 0, po.shape[1] - 1)
    ri = np.clip((oth_t / (hop_t * step)).astype(int), 0, pr.shape[1] - 1)
    a, b = po[:, oi], pr[:, ri]
    sim = np.sum(a * b, axis=0) / (np.linalg.norm(a, axis=0) * np.linalg.norm(b, axis=0) + 1e-9)
    overall = float(np.mean(sim))
    sec_rows = []
    for s in sections or [{"label": "Whole recording", "start": 0.0, "end": do}]:
        msk = (ref_t >= s["start"]) & (ref_t < s["end"])
        if not msk.any():
            continue
        v = float(np.mean(sim[msk]))
        e_o = float(np.sqrt(np.mean(yo[int(s["start"] * sr): int(s["end"] * sr)] ** 2) + 1e-12))
        r0, r1 = mu.map_time(al["path"], s["start"]), mu.map_time(al["path"], s["end"])
        seg = yr[int(r0 * sr): int(r1 * sr)]
        e_r = float(np.sqrt(np.mean(seg**2) + 1e-12)) if seg.size else 0.0
        sec_rows.append({"label": s["label"], "start": s["start"], "end": s["end"], "harmonic_similarity": round(v, 3),
                         "original_has_sound": e_o > 1e-3, "reconstruction_has_sound": e_r > 1e-3})
        if e_o > 1e-3 and e_r <= 1e-3:
            disc.append(_disc("missing_section", s["start"], f"'{s['label']}' contains music", "silent in the reconstruction", 0.95,
                              "The score has no notes here. Transcribe or enter this passage.", severity="error"))
    if sec_rows:
        med = float(np.median([r["harmonic_similarity"] for r in sec_rows]))
        for r in sec_rows:
            if r["harmonic_similarity"] < med - 0.12:
                disc.append(_disc("harmony", r["start"], f"'{r['label']}'", f"harmonic similarity {r['harmonic_similarity']:.2f} vs typical {med:.2f}", 0.5,
                                  "This section's notes/chords differ from the original more than the rest. Listen A/B and check the transcription here."))
    check("harmony", "Harmonic content by section", "info",
          f"Average chroma similarity {overall:.2f} on a 0–1 scale. Read this as a relative guide between sections only: different instruments and removed noise lower the number even when every note is right.",
          overall=round(overall, 3), sections=sec_rows)

    # 4 ---- melody vs what is audible in the original
    ctx.progress(0.55, "Checking score melody against pitches heard in the original")
    mel_part = next((p for p in (sv.data.get("parts", []) if sv else []) if p.get("voice") == "melody" or p.get("role") == "vocal"), None)
    pt = ctx.latest_asset("analysis", what="pitch_track")
    pt_on_stem = bool(pt and bool(np.load(ctx.path(pt))["on_vocal_stem"]))
    if mel_part and mel_part["notes"] and not pt_on_stem:
        check("melody", "Melody notes vs original audio", "not_evaluated",
              "Not evaluated: checking the melody needs the pitch of the voices on their own, which requires the separated voice stem. "
              "Tracking pitch on the full mix follows the loudest instrument and produced only false alarms in testing, so it is not used.")
    elif mel_part and mel_part["notes"]:
        if pt:
            z = np.load(ctx.path(pt))
            times, f0, prob, on_stem = z["times"], z["f0"], z["prob"], bool(z["on_vocal_stem"])
        else:
            tr = mu.pitch_track(yo, sr)
            times, f0, prob, on_stem = tr["times"], np.nan_to_num(tr["f0"]), np.nan_to_num(tr["prob"]), False
        mv = melody_vs_audio(mel_part["notes"], times, f0, prob)
        rows = mv.pop("rows")
        circular = mel_part.get("source") == "pyin" and pt is not None
        for r in sorted(rows, key=lambda r: -r["confidence"])[:60]:
            n = r["note"]
            if r["kind"] == "octave":
                disc.append(_disc("melody_octave", n["start"], f"heard near {pitch_name(r['heard'])}", f"score has {pitch_name(n['pitch'])}", r["confidence"],
                                  "Octave difference. Often a pitch-tracker artefact (men and women singing in octaves). Confirm by ear before changing.",
                                  {"op": "set_pitch", "part_id": mel_part["id"], "note_id": n["id"], "pitch": r["heard"]}))
            else:
                disc.append(_disc("melody_pitch", n["start"], f"heard near {pitch_name(r['heard'])}", f"score has {pitch_name(n['pitch'])}", r["confidence"],
                                  f"Change the note to {pitch_name(r['heard'])} if that is what you hear.",
                                  {"op": "set_pitch", "part_id": mel_part["id"], "note_id": n["id"], "pitch": r["heard"]}))
        agr = mv["agreement"]
        check("melody", "Melody notes vs original audio", "info" if circular else ("pass" if (agr or 0) >= 0.9 else "warn"),
              (f"{mv['agree']} of {mv['checked']} checkable melody notes agree with the pitch heard in the original ({agr:.0%}); "
               f"{mv['differ']} differ, {mv['octave_differences']} differ by an octave, {mv['unverifiable']} could not be checked (no clear pitch). " if agr is not None
               else "No melody note could be checked against a clear pitch in the original. ")
              + ("Caution: this melody was itself produced by the same pitch tracker, so agreement here is expected and proves little. An independent check needs your ears or an imported score. " if circular else "")
              + ("" if on_stem else "Pitch was tracked on the full mix (no separated voice stem), so instruments may be mistaken for the melody."), **mv, circular=circular)
    else:
        check("melody", "Melody notes vs original audio", "not_evaluated", "The score has no melody/vocal part to check.")

    # 5 ---- tempo
    ctx.progress(0.7, "Comparing tempo")
    to, tr_ = mu.tempo_and_beats(yo, sr), mu.tempo_and_beats(yr, sr)
    ratio = tr_["tempo_bpm"] / to["tempo_bpm"] if to["tempo_bpm"] else 0
    near = min(abs(ratio - k) for k in (0.5, 1.0, 2.0, 2 / 3, 1.5))
    check("tempo", "Tempo", "pass" if abs(ratio - 1) < 0.04 else ("info" if near < 0.04 else "warn"),
          f"Beat tracker reports {to['tempo_bpm']:.0f} BPM for the original and {tr_['tempo_bpm']:.0f} BPM for the reconstruction."
          + (" (A simple ratio: the tracker is counting beats at a different level, not a real tempo difference.)" if near < 0.04 and abs(ratio - 1) >= 0.04 else ""),
          original_bpm=to["tempo_bpm"], reconstruction_bpm=tr_["tempo_bpm"])
    if near >= 0.04:
        disc.append(_disc("tempo", None, f"{to['tempo_bpm']:.0f} BPM", f"{tr_['tempo_bpm']:.0f} BPM", 0.5, "Tempo estimates disagree. Check note timing against the original."))

    # 6 ---- entries: first sound, and vocal-section starts
    def first_sound(y):
        n = int(0.05 * sr); k = len(y) // n
        rms = np.sqrt(np.mean(y[: k * n].reshape(k, n) ** 2, axis=1))
        idx = np.where(rms > max(rms.max() * 0.05, 1e-4))[0]
        return float(idx[0] * 0.05) if idx.size else None
    fo_, fr_ = first_sound(yo), first_sound(yr)
    if fo_ is not None and fr_ is not None:
        ok = abs(fo_ - fr_) < 0.75
        check("entries", "First entry", "pass" if ok else "warn", f"Music starts at {fo_:.2f}s in the original and {fr_:.2f}s in the reconstruction.", original_s=fo_, reconstruction_s=fr_)
        if not ok:
            disc.append(_disc("entry", min(fo_, fr_), f"starts at {fo_:.2f}s", f"starts at {fr_:.2f}s", 0.8, "The opening is early/late or missing notes. Check the first bars of the score."))

    # 7 ---- symbolic comparisons
    ctx.progress(0.85, "Comparing symbolic scores")
    if sv:
        imported = ctx.db.execute(select(m.ScoreVersion).where(m.ScoreVersion.project_id == ctx.project.id, m.ScoreVersion.origin.like("import%"))
                                  .order_by(m.ScoreVersion.version.desc())).scalars().first()
        auto = ctx.db.execute(select(m.ScoreVersion).where(m.ScoreVersion.project_id == ctx.project.id, m.ScoreVersion.origin == "auto-transcription")
                              .order_by(m.ScoreVersion.version.desc())).scalars().first()
        if imported and auto:
            cs = compare_scores(imported.data, auto.data, onset_tol=0.15)
            miss, extra = cs.pop("missing"), cs.pop("extra")
            check("score_vs_transcription", "Imported score vs automatic transcription", "info",
                  f"Of {cs['reference_notes']} notes in the imported score, the automatic transcription found {cs['matched']} at the same time and pitch "
                  f"(recall {cs['recall']:.0%}, precision {cs['precision']:.0%}). Low figures usually mean the imported score is not yet time-aligned to the recording, or the transcription is wrong.",
                  **cs, missing_examples=miss[:20], extra_examples=extra[:20])
        else:
            check("score_vs_transcription", "Imported score vs automatic transcription", "not_evaluated",
                  "Needs both an imported reference score and an automatic transcription. Import a verified MIDI/MusicXML to enable note-level checking.")
        low = [n for p in sv.data.get("parts", []) for n in p["notes"] if n.get("uncertain") and not n.get("verified")]
        check("unverified_notes", "Unverified notes used in the render", "warn" if low else "pass",
              f"{len(low)} note(s) in the rendered score are still flagged uncertain and unverified." if low else "No uncertain notes remain in the rendered score.", count=len(low))
    lyr = ctx.head(m.LyricsVersion, "lyrics")
    check("lyrics", "Lyric accuracy", "not_evaluated",
          "Not evaluated: no lyric-recognition engine is available, and the current render contains no sung words. "
          + (f"{sum(1 for l in lyr.data.get('lines', []) if l.get('status') != 'verified')} lyric line(s) are still unverified." if lyr else "No lyrics exist yet."))
    check("section_order", "Section and verse order", "info" if sections else "not_evaluated",
          "The reconstruction is rendered on the original's timeline, so section order is inherited from the score rather than independently detected. "
          "A missing or silent section would be reported above." if sections else "No section timeline exists.")

    summary_counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "info", "not_evaluated")}
    data = {"reconstruction_id": rec.id, "reconstruction_version": rec.version, "score_version_id": rec.score_version_id,
            "original_asset_id": work.id, "reconstruction_asset_id": recon_asset.id, "checks": checks, "discrepancies": disc,
            "alignment_map": al["path"], "shared_timeline": exact_timeline, "summary_counts": summary_counts,
            "verdict": "This report lists detected differences; it does not certify the reconstruction as exact. "
                       "Checks marked 'not evaluated' were not performed, and several checks compare against automatic estimates rather than ground truth. Your listening review is required.",
            "limits": ["Comparisons use the automatic analysis of a noisy recording as reference; errors in that analysis appear as false alarms or are missed.",
                       "Inner-voice (alto/tenor) accuracy cannot be verified from this recording by any check here.",
                       "Timbre differences between the phone recording and new instruments are expected and are not counted as errors."]}
    rep = m.FidelityReport(project_id=ctx.project.id, reconstruction_id=rec.id, data=data)
    for old in ctx.db.execute(select(m.FidelityReport).where(m.FidelityReport.project_id == ctx.project.id, m.FidelityReport.stale.is_(False))).scalars():
        old.stale = True
    ctx.db.add(rep)
    ctx.db.commit()
    ctx.output(f"Inspected reconstruction v{rec.version}: {len(disc)} discrepancy item(s) for review; {summary_counts['pass']} checks passed, "
               f"{summary_counts['warn']} need attention, {summary_counts['not_evaluated']} could not be evaluated. This is not a certificate of exactness.",
               {"fidelity_report_id": rep.id, "summary_counts": summary_counts, "discrepancies": len(disc), "verdict": data["verdict"]})
