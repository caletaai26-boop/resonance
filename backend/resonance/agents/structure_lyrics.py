from __future__ import annotations

import shutil

import numpy as np

from .. import gates
from .. import models as m
from ..audio import io as aio
from ..music import analysis as mu
from ..music import language as lang
from .base import Ctx, agent, new_version


def vocal_activity(ctx: Ctx) -> tuple[np.ndarray | None, np.ndarray | None, str]:
    """Per-frame 0/1 'someone is singing' estimate and how it was obtained."""
    vocals = ctx.vocal_stem()
    if vocals:
        yv, sr = aio.load_mono(ctx.path(vocals), mu.SR)
        n = int(0.1 * sr)
        k = len(yv) // n
        rms = np.sqrt(np.mean(yv[: k * n].reshape(k, n) ** 2, axis=1))
        thr = max(np.percentile(rms, 95) * 0.12, 1e-4)
        return (rms > thr).astype(float), np.arange(k) * 0.1, "energy of the separated voice stem"
    # Without a separated voice stem there is no trustworthy way to tell singing from instruments.
    return None, None, "not available (needs the separated voice stem)"


@agent("structure", 5, "Structural Analysis", "Finds section boundaries, repetitions and vocal/instrumental passages.")
def run_structure(ctx: Ctx) -> None:
    work = ctx.working()
    ctx.progress(0.1, "Loading audio")
    y, sr = aio.load_mono(ctx.path(work), mu.SR)
    score = ctx.head(m.ScoreVersion, "score")
    beats = (score.data.get("beats") if score else None) or mu.tempo_and_beats(y, sr)["beats"]
    ctx.progress(0.35, "Estimating where voices are active")
    va, vt, how = vocal_activity(ctx)
    ctx.progress(0.55, "Clustering harmonic and timbral features into sections")
    sections = mu.segment_structure(y, sr, beats, va, vt)
    v = 0
    for s in sections:  # number vocal sections; we do NOT guess verse vs refrain from audio alone
        if s["kind"] == "vocal":
            v += 1
            s["label"] = f"Vocal section {v}"
    head = ctx.head(m.SectionSet, "sections")
    protected = head is not None and (head.origin == "user-edit" or head.verification == "approved")
    ss = new_version(ctx.db, ctx.project, m.SectionSet, "sections", {"sections": sections, "duration": len(y) / sr},
                     origin="auto-structure", note="Detected sections (unverified)", move_head=not protected,
                     provenance={"job_id": ctx.job.id, "method": "librosa agglomerative segmentation on beat-synchronous chroma+MFCC", "vocal_activity": how})
    inv = gates.invalidate(ctx.db, ctx.project, "structure", "Sections were re-detected.") if (head is not None and not protected) else None
    ctx.db.commit()
    ctx.output(
        f"Detected {len(sections)} sections: " + ", ".join(f"{s['label']} ({s['start']:.0f}–{s['end']:.0f}s)" for s in sections[:8]) + ("…" if len(sections) > 8 else "")
        + (" Your edited/approved timeline was kept as current; this detection is stored as an alternative." if protected else ""),
        {"section_set_id": ss.id, "became_current": not protected, "sections": sections, "vocal_activity_method": how, "invalidation": inv,
         "uncertainty": "Boundaries are placed where harmony and timbre change and are typically accurate to within a bar, not a beat. "
                        "The system can tell 'voices present' from 'instruments only' (reliably only with separated stems). It cannot tell a verse from a refrain: "
                        "sections with the same group letter sound alike harmonically. Please rename and adjust them."},
    )


def _asr_status() -> dict:
    try:
        import faster_whisper  # noqa: F401

        return {"available": True, "engine": "faster-whisper"}
    except Exception:
        return {"available": False, "reason": "No speech/lyrics recognition engine is installed (faster-whisper + a downloaded Whisper model are required). "
                                              "Note that even with Whisper, sung Latin in a reverberant church is recognised poorly; a human check is always needed."}


def distribute_lines(blocks: list[dict], sections: list[dict], language: str) -> list[dict]:
    """Draft timing: text blocks are assigned to vocal sections in order; within a section lines are spread
    in proportion to their syllable counts. This is arithmetic, not listening; every line is 'draft'."""
    vocal = [s for s in sections if s.get("kind") == "vocal"] or [s for s in sections if s.get("kind") != "instrumental"]
    lines = []
    for bi, b in enumerate(blocks):
        sec = vocal[bi] if bi < len(vocal) else None
        syl = [max(1, len(lang.line_syllables(t, language))) for t in b["lines"]]
        total = sum(syl)
        t0 = sec["start"] if sec else None
        for text, n in zip(b["lines"], syl):
            if sec:
                t1 = t0 + (sec["end"] - sec["start"]) * n / total
                lines.append({"text": text, "start": round(t0, 2), "end": round(t1, 2), "kind": b.get("kind", "verse"),
                              "block": b.get("label", f"Block {bi + 1}"), "status": "draft", "uncertain": True})
                t0 = t1
            else:
                lines.append({"text": text, "start": None, "end": None, "kind": b.get("kind", "verse"),
                              "block": b.get("label", f"Block {bi + 1}"), "status": "draft", "uncertain": True})
    return lines


def align_syllables_to_notes(lines: list[dict], notes: list[dict], language: str) -> tuple[list[dict], dict]:
    """Assign syllables to melody notes inside each line's time window, one syllable per note in order.
    Extra notes become melisma continuations ('_'); extra syllables are reported, not squeezed in."""
    notes = [dict(n) for n in sorted(notes, key=lambda n: n["start"])]
    stats = {"lines_aligned": 0, "syllables_placed": 0, "syllables_unplaced": 0}
    for ln in lines:
        if ln.get("start") is None or ln.get("end") is None:
            continue
        syl = lang.line_syllables(ln["text"], language)
        window = [n for n in notes if ln["start"] - 0.05 <= n["start"] < ln["end"]]
        if not window:
            stats["syllables_unplaced"] += len(syl)
            continue
        for k, n in enumerate(window):
            n["lyric"] = syl[k] if k < len(syl) else "_"
            n["uncertain"] = True
        stats["lines_aligned"] += 1
        stats["syllables_placed"] += min(len(syl), len(window))
        stats["syllables_unplaced"] += max(0, len(syl) - len(window))
    return notes, stats


@agent("lyrics", 4, "Lyrics and Language", "Prepares a time-aligned lyric draft, pronunciation guide and syllable underlay.")
def run_lyrics(ctx: Ctx) -> None:
    language = ctx.params.get("language") or ctx.project.language or "la"
    text = (ctx.params.get("text") or "").strip()
    ref_key = ctx.params.get("reference")
    asr = _asr_status()
    sections_row = ctx.head(m.SectionSet, "sections")
    sections = (sections_row.data.get("sections") if sections_row else []) or []
    ctx.progress(0.2, "Preparing text")
    notes_out: list[str] = []
    if text:
        blocks = [{"kind": "verse", "label": f"Block {i + 1}", "lines": [l.strip() for l in blk.splitlines() if l.strip()]}
                  for i, blk in enumerate(t for t in text.replace("\r", "").split("\n\n") if t.strip())]
        source = "user-typed"
    elif ref_key:
        ref = lang.REFERENCE_TEXTS.get(ref_key)
        if not ref:
            raise ValueError(f"unknown reference text '{ref_key}'")
        blocks, language, source = ref["blocks"], ref["language"], f"reference:{ref_key}"
        notes_out.append("This draft is a PUBLISHED REFERENCE TEXT, not a transcription of your recording. " + ref["note"])
    else:
        blocks, source = [], "empty"
        notes_out.append("No lyrics were produced. " + (asr.get("reason") or "") + " Type or paste the words you hear, or load a reference text to compare against and correct.")
    ctx.progress(0.6, "Drafting line timings from the section timeline")
    lines = distribute_lines(blocks, sections, language)
    if blocks and not sections:
        notes_out.append("No section timeline exists yet, so lines have no timings. Run Structural Analysis, then re-run this step.")
    n_vocal = len([s for s in sections if s.get("kind") == "vocal"])
    if blocks and n_vocal and len(blocks) != n_vocal:
        notes_out.append(f"The text has {len(blocks)} blocks but the timeline has {n_vocal} vocal sections. Lines beyond the matched sections have no timing; adjust sections or text.")
    guide = lang.pronunciation_guide([l["text"] for l in lines], language)
    data = {"language": language, "source": source, "lines": lines, "pronunciation": guide, "notes": notes_out}
    head = ctx.head(m.LyricsVersion, "lyrics")
    lv = new_version(ctx.db, ctx.project, m.LyricsVersion, "lyrics", data, origin=f"lyrics-draft:{source}", note="Lyric draft (unverified)",
                     provenance={"job_id": ctx.job.id, "asr": asr, "timing": "proportional distribution over vocal sections (not audio-aligned)"})
    inv = gates.invalidate(ctx.db, ctx.project, "lyrics", "A new lyric draft was created.") if head is not None else None
    ctx.db.commit()
    if not blocks:
        summary = "No lyric draft could be generated automatically: no lyric-recognition engine is available. Paste the words or load a reference text."
    else:
        summary = (f"Drafted {len(lines)} lines in {len(blocks)} blocks from {'your text' if source == 'user-typed' else 'a published reference text (not from the recording)'}; "
                   f"timings are arithmetic estimates and every line is flagged for review. Pronunciation guide covers {len(guide)} words.")
    ctx.output(summary, {"lyrics_version_id": lv.id, "lines": len(lines), "source": source, "asr": asr, "notes": notes_out, "invalidation": inv,
                         "uncertainty": "Automatic recognition of the words actually sung was not performed. Line timings are not derived from listening."})
