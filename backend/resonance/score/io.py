"""Score import/export: MIDI (pretty_midi) and MusicXML (music21)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pretty_midi

from .model import Note, Part, Score, validate_score

VOCAL_HINTS = ("sopran", "alto", "tenor", "bass voice", "bajo", "choir", "coro", "voice", "voz", "vocal", "cantus", "satb")


INSTRUMENT_HINTS = ("organ", "organo", "orgel", "piano", "pno", "keyboard", "kbd", "harmonium", "guitar", "violin", "viola", "cello",
                    "flute", "oboe", "clarinet", "trumpet", "horn", "trombone", "harp", "accomp", "continuo", "strings")


def _is_instrument(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in INSTRUMENT_HINTS)


def _is_vocal(name: str, program: int | None = None) -> bool:
    n = (name or "").lower()
    return any(h in n for h in VOCAL_HINTS) or program in (52, 53, 54)


# ---------------------------------------------------------------- MIDI
def score_to_midi(score: dict, path: Path, part_ids: list[str] | None = None,
                  program_override: dict[str, int] | None = None) -> int:
    """Writes a Standard MIDI File preserving note times in seconds exactly. Returns the note count."""
    bpm = float(score.get("tempo_bpm") or 120.0)
    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm, resolution=960)
    num, den = score.get("time_signature") or (4, 4)
    pm.time_signature_changes.append(pretty_midi.TimeSignature(int(num), int(den), 0))
    count = 0
    for part in score.get("parts", []):
        if part_ids is not None and part["id"] not in part_ids:
            continue
        program = (program_override or {}).get(part["id"], part.get("program", 0))
        inst = pretty_midi.Instrument(program=int(program), name=part.get("name", "Part"))
        for n in part.get("notes", []):
            inst.notes.append(pretty_midi.Note(velocity=int(n.get("velocity", 80)), pitch=int(n["pitch"]),
                                               start=float(n["start"]), end=float(n["end"])))
            count += 1
        pm.instruments.append(inst)
    path.parent.mkdir(parents=True, exist_ok=True)
    pm.write(str(path))
    return count


def midi_to_score(path: Path) -> dict:
    pm = pretty_midi.PrettyMIDI(str(path))
    parts = []
    for i, inst in enumerate(pm.instruments):
        if inst.is_drum:
            continue
        name = inst.name.strip() or pretty_midi.program_to_instrument_name(inst.program)
        vocal = _is_vocal(name, inst.program)
        parts.append(Part(
            name=name or f"Track {i + 1}", role="vocal" if vocal else "instrument",
            voice="melody" if i == 0 else "harmony", program=inst.program, source="import:midi",
            notes=[Note(start=max(0.0, n.start), end=max(n.end, n.start + 0.02), pitch=n.pitch,
                        velocity=max(1, n.velocity)) for n in inst.notes],
        ))
    _, tempi = pm.get_tempo_changes()
    ts = pm.time_signature_changes[0] if pm.time_signature_changes else None
    beats = [float(b) for b in pm.get_beats()]
    s = Score(tempo_bpm=float(tempi[0]) if len(tempi) else None,
              time_signature=(ts.numerator, ts.denominator) if ts else (4, 4),
              beats=beats, duration=float(pm.get_end_time()), parts=parts)
    return validate_score(s.model_dump())


# ---------------------------------------------------------------- MusicXML
def musicxml_to_score(path: Path) -> dict:
    from music21 import converter, meter, tempo
    from music21 import note as m21note
    from music21 import chord as m21chord

    sc = converter.parse(str(path))
    # Unfold repeats so section order matches what is actually performed from the score.
    try:
        sc = sc.expandRepeats()
    except Exception:
        pass
    mm = next(iter(sc.recurse().getElementsByClass(tempo.MetronomeMark)), None)
    bpm = float(mm.getQuarterBPM()) if mm and mm.getQuarterBPM() else 90.0
    spq = 60.0 / bpm  # seconds per quarter. MusicXML carries no performance timing: constant tempo.
    ts = next(iter(sc.recurse().getElementsByClass(meter.TimeSignature)), None)
    parts, warnings = [], []
    if not mm:
        warnings.append("The MusicXML file has no tempo marking; 90 BPM was assumed. Align the score to the recording before rendering.")
    for i, p in enumerate(sc.parts):
        name = (p.partName or f"Part {i + 1}").strip()
        notes = []
        flat = p.flatten()
        tied: dict[int, Note] = {}
        for el in flat.notes:
            pitches = [el.pitch] if isinstance(el, m21note.Note) else list(el.pitches) if isinstance(el, m21chord.Chord) else []
            start, dur = float(el.offset) * spq, float(el.duration.quarterLength) * spq
            if dur <= 0:
                continue
            lyric = (el.lyric or "") if isinstance(el, m21note.Note) else ""
            for pt in pitches:
                midi = int(pt.midi)
                t = el.tie.type if el.tie is not None else None
                if t in ("stop", "continue") and midi in tied:
                    tied[midi].end = start + dur
                    if t == "stop":
                        tied.pop(midi)
                    continue
                n = Note(start=start, end=start + dur, pitch=midi, velocity=80, lyric=lyric)
                notes.append(n)
                if t == "start":
                    tied[midi] = n
        vocal = _is_vocal(name) or any(n.lyric for n in notes)
        low = name.lower()
        voice = next((v for v in ("soprano", "alto", "tenor", "bass") if v in low), "melody" if i == 0 else "harmony")
        parts.append(Part(name=name, role="vocal" if vocal else "instrument", voice=voice,
                          program=52 if vocal else 19, source="import:musicxml", notes=notes))
    # Choir scores often print the words only under the upper stave. If any part carries lyrics, every part that
    # is not explicitly an instrument (organ, piano...) is treated as sung: otherwise tenor and bass vanish.
    if any(p.role == "vocal" for p in parts):
        for p in parts:
            if p.role == "instrument" and p.notes and not _is_instrument(p.name):
                p.role, p.program = "vocal", 52
    total_q = float(sc.highestTime)
    s = Score(tempo_bpm=bpm, time_signature=(ts.numerator, ts.denominator) if ts else (4, 4),
              beats=[round(k * spq, 4) for k in range(int(total_q) + 1)], duration=total_q * spq,
              parts=parts, warnings=warnings)
    return validate_score(s.model_dump())


def _beat_position(t: float, beats: np.ndarray) -> float:
    """Seconds -> fractional beat index using the (possibly irregular) beat grid."""
    if len(beats) < 2:
        return t
    i = int(np.clip(np.searchsorted(beats, t, side="right") - 1, 0, len(beats) - 2))
    span = beats[i + 1] - beats[i]
    return i + (t - beats[i]) / span if span > 0 else float(i)


def score_to_musicxml(score: dict, path: Path, title: str = "") -> dict:
    """Quantises note times to 16ths against the beat grid and writes MusicXML via music21.
    Quantisation is lossy by nature; the exact timing stays in the score JSON / MIDI export."""
    from music21 import clef, key, metadata, meter, note, stream, tempo

    bpm = float(score.get("tempo_bpm") or 90.0)
    beats = np.array(score.get("beats") or [], dtype=float)
    if len(beats) < 2:
        beats = np.arange(0, float(score.get("duration") or 1.0) + 60.0 / bpm, 60.0 / bpm)
    else:  # extend grid past the last note
        step = float(np.median(np.diff(beats)))
        last = max([n["end"] for p in score.get("parts", []) for n in p["notes"]] + [beats[-1]])
        extra = np.arange(beats[-1] + step, last + 2 * step, step)
        beats = np.concatenate([beats, extra])
    shift = int(score.get("downbeat_offset") or 0)
    num, den = score.get("time_signature") or (4, 4)
    sc = stream.Score()
    sc.metadata = metadata.Metadata(title=title or "Untitled", composer="Transcribed with RESONANCE")
    stats = {"notes": 0, "parts": 0}
    for part in score.get("parts", []):
        if not part.get("notes"):
            continue
        p = stream.Part()
        p.partName = part.get("name", "Part")
        p.insert(0, meter.TimeSignature(f"{num}/{den}"))
        p.insert(0, tempo.MetronomeMark(number=round(bpm)))
        if score.get("key"):
            try:
                tonic, mode = score["key"].split()[:2]
                p.insert(0, key.Key(tonic.replace("b", "-"), mode))
            except Exception:
                pass
        pitches = [n["pitch"] for n in part["notes"]]
        p.insert(0, clef.BassClef() if np.median(pitches) < 55 else clef.TrebleClef())
        for n in part["notes"]:
            b0 = round((_beat_position(n["start"], beats) - shift) * 4) / 4
            b1 = round((_beat_position(n["end"], beats) - shift) * 4) / 4
            if b0 < 0:
                continue
            m = note.Note(int(n["pitch"]))
            m.duration.quarterLength = max(0.25, b1 - b0)
            if n.get("lyric"):
                m.lyric = n["lyric"]
            p.insert(b0, m)
            stats["notes"] += 1
        p.makeVoices(inPlace=True)
        try:
            p.makeNotation(inPlace=True)
        except Exception:
            pass
        sc.insert(0, p)
        stats["parts"] += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    sc.write("musicxml", fp=str(path))
    return stats
