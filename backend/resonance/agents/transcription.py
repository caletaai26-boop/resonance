from __future__ import annotations

import numpy as np

from .. import gates
from .. import models as m
from ..audio import io as aio
from ..music import analysis as mu
from ..score.model import Note, Part, Score, validate_score
from .base import Ctx, agent, new_version


def _mk(notes: list[dict], uncertain_below: float = 0.6) -> list[Note]:
    out = []
    for n in notes:
        if n["end"] - n["start"] < 0.03:
            continue
        c = n.get("confidence")
        out.append(Note(start=n["start"], end=n["end"], pitch=n["pitch"], confidence=c,
                        velocity=int(np.clip(50 + 50 * (c or 0.5), 30, 110)),
                        uncertain=bool(n.get("overtone_suspect") or (c is not None and c < uncertain_below))))
    return out


@agent("transcription", 3, "Musical Transcription", "Estimates tempo, key, chords, the melody line and polyphonic notes from the audio.")
def run(ctx: Ctx) -> None:
    work = ctx.working()
    vocals = ctx.latest_asset("stem", stem="vocals")
    accomp = ctx.latest_asset("stem", stem="accompaniment")
    ctx.progress(0.03, "Loading audio")
    y, sr = aio.load_mono(ctx.path(work), mu.SR)
    duration = len(y) / sr
    warnings: list[str] = []

    ctx.progress(0.1, "Tracking tempo and beats")
    tb = mu.tempo_and_beats(y, sr)
    ctx.progress(0.2, "Estimating key and chords")
    chroma, ftimes = mu.chroma_features(y, sr)
    key = mu.estimate_key(chroma)
    chords = mu.estimate_chords(chroma, ftimes, tb["beats"])
    tuning = mu.tuning_offset_cents(y, sr)

    ctx.progress(0.3, "Tracking pitch (pYIN) on " + ("the separated voice stem" if vocals else "the full mix (used for checking only; no voice stem)"))
    if vocals:
        ym, _ = aio.load_mono(ctx.path(vocals), mu.SR)
    else:
        ym = y
        warnings.append("No separated voice stem was available. The 'melody' is therefore only the highest detected line of the full mix: it follows whatever is on top, voices or instruments, and has gaps.")
    track = mu.pitch_track(ym, sr)
    melody = mu.split_at_onsets(mu.notes_from_pitch_track(track), ym, sr) if vocals else []
    voiced = np.nan_to_num(track["prob"]) * (~np.isnan(track["f0"]))
    npz = ctx.tmp / "pitch_track.npz"
    np.savez_compressed(npz, times=track["times"].astype(np.float32), f0=np.nan_to_num(track["f0"]).astype(np.float32),
                        prob=np.nan_to_num(track["prob"]).astype(np.float32), on_vocal_stem=bool(vocals))
    pt_asset = ctx.store(npz, "analysis", "Melody pitch track", parent=vocals or work, method="pYIN fundamental-frequency tracking",
                         engine="librosa.pyin", meta={"what": "pitch_track", "on_vocal_stem": bool(vocals)})

    parts: list[Part] = []
    if melody:
        parts.append(Part(name="Melody (lead vocal line)", role="vocal", voice="melody", program=52, source="pyin", notes=_mk(melody)))
    poly_stats: dict = {}

    ok, why = mu.basic_pitch_available()
    poly_count = 0
    if ok:
        ctx.progress(0.55, "Polyphonic note detection (Basic Pitch neural model) on " + ("the accompaniment stem" if accomp else "the full mix"))
        notes, poly_stats = mu.clean_polyphonic(mu.basic_pitch_notes(ctx.path(accomp or work)))
        poly_count = len(notes)
        if not vocals:
            # No voice stem: a monophonic tracker on the full mix follows whatever is loudest (measured on the test
            # fixture: 0% melody recall). The top line of the polyphonic result is a better, still-rough, candidate.
            top = mu.skyline(notes)
            ids = {id(n) for n in top}
            notes = [n for n in notes if id(n) not in ids]
            if top:
                parts.append(Part(name="Top line (melody candidate)", role="vocal", voice="melody", program=52, source="basic-pitch-skyline", notes=_mk(top)))
        voices = mu.split_voices(notes)
        for key_, name, voice in (("upper", "Upper line (polyphonic estimate)", "soprano"),
                                  ("inner", "Inner harmony (polyphonic estimate)", "harmony"),
                                  ("bass", "Bass line (polyphonic estimate)", "bass")):
            if voices[key_]:
                parts.append(Part(name=name, role="instrument", voice=voice, program=19, source="basic-pitch", notes=_mk(voices[key_])))
        warnings.append("Polyphonic notes come from an automatic model. On live choir-plus-instruments audio, expect wrong, missing and extra notes, "
                        "especially in inner voices. Parts are sorted by register (top / middle / bottom), which is not true voice separation.")
        if not accomp:
            warnings.append("Without separated stems the polyphonic model heard voices and instruments together; sung notes are mixed in with instrumental notes.")
    else:
        warnings.append(f"Polyphonic transcription was skipped: {why}. Only the single melody line was transcribed.")

    score = Score(key=key["key"], key_confidence=key["confidence"], time_signature=tuple(tb["time_signature"]),
                  meter_confidence=tb["meter_confidence"], tempo_bpm=tb["tempo_bpm"], tempo_curve=tb["tempo_curve"],
                  beats=tb["beats"], downbeat_offset=tb["downbeat_offset"], duration=duration, parts=parts,
                  chords=chords, warnings=warnings)
    data = validate_score(score.model_dump())

    ctx.progress(0.9, "Saving the preliminary score as a new version")
    head = ctx.head(m.ScoreVersion, "score")
    protected = head is not None and (head.origin.startswith("import") or head.origin == "user-edit" or head.verification == "approved")
    sv = new_version(ctx.db, ctx.project, m.ScoreVersion, "score", data, origin="auto-transcription",
                     note="Automatic transcription (unverified)", move_head=not protected,
                     provenance={"job_id": ctx.job.id, "source_asset": work.id, "vocal_stem": vocals.id if vocals else None,
                                 "engines": {"melody": "librosa.pyin on voice stem + onset splitting" if vocals else "top line of Basic Pitch notes", "polyphonic": "basic-pitch ICASSP-2022 (onnx)" if ok else None,
                                             "beats": "librosa.beat_track", "key": "Krumhansl-Schmuckler", "chords": "triad template matching"}})
    inv = None
    if not protected and head is not None:
        inv = gates.invalidate(ctx.db, ctx.project, "score", "A new automatic transcription replaced the current score.")
    ctx.db.commit()

    confs = [n["confidence"] for p in data["parts"] for n in p["notes"] if n.get("confidence") is not None]
    low = sum(1 for p in data["parts"] for n in p["notes"] if n.get("uncertain"))
    ctx.output(
        f"Estimated tempo {tb['tempo_bpm']:.0f} BPM, key {key['key']} (confidence {key['confidence']:.0%}), "
        f"{len(melody)} melody notes and {poly_count} polyphonic notes. {low} of {len(confs)} notes are flagged uncertain (low confidence or possible overtone) for review."
        + (" Your existing imported/edited score was kept as the current version; this transcription is stored as an alternative." if protected else ""),
        {"score_version_id": sv.id, "became_current": not protected, "tempo": tb | {"beats": len(tb["beats"])}, "key": key,
         "tuning_offset_cents": tuning, "chords": len(chords), "melody_notes": len(melody), "polyphonic_notes": poly_count,
         "low_confidence_notes": low, "polyphonic_cleanup": poly_stats, "mean_confidence": round(float(np.mean(confs)), 2) if confs else None,
         "voiced_ratio": round(float(np.mean(voiced > 0.5)), 2), "warnings": warnings, "invalidation": inv,
         "artifacts": {"pitch_track": pt_asset.id},
         "uncertainty": "Everything here is an estimate. Meter detection in particular is weak (it only distinguishes 3 from 4 beats). "
                        "Chords are limited to major/minor triads. Nothing is treated as correct until you approve it at Gate 3."},
    )
