"""Synthetic test recording with KNOWN ground truth: notes, tempo, key and section boundaries.

Structure (100 BPM, C major, 4/4):
  0.0 –  9.6 s  instrumental introduction (organ chords + bass)
  9.6 – 28.8 s  'vocal' section (melody on a voice-like patch over the organ)
 28.8 – 38.4 s  instrumental interlude
 38.4 – 57.6 s  'vocal' section (same melody again)
Rendered with FluidSynth, then degraded (noise, band-limiting, MP3) to resemble a phone recording.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from resonance.audio import synth
from resonance.score.io import score_to_midi
from resonance.score.model import Note, Part, Score

BPM = 100.0
BEAT = 60.0 / BPM
SECTIONS = [(0.0, 9.6, "instrumental"), (9.6, 28.8, "vocal"), (28.8, 38.4, "instrumental"), (38.4, 57.6, "vocal")]
# one bar per chord: C  F  G  C  (triads, root in bass)
PROG = [(48, [60, 64, 67]), (53, [60, 65, 69]), (55, [62, 67, 71]), (48, [60, 64, 67])]
# 8-bar melody, (pitch, beats)
MELODY = [(67, 1), (67, 1), (62, 1), (67, 1), (69, 2), (62, 2), (71, 1), (69, 1), (71, 1), (72, 1), (74, 2), (71, 2),
          (72, 1), (71, 1), (69, 1), (67, 1), (65, 2), (64, 2), (62, 1), (64, 1), (62, 1), (60, 1), (60, 4)]


def truth_score() -> Score:
    organ, bass, mel = [], [], []
    total_bars = int(round(SECTIONS[-1][1] / (4 * BEAT)))
    for bar in range(total_bars):
        root, triad = PROG[bar % 4]
        t0, t1 = bar * 4 * BEAT, (bar + 1) * 4 * BEAT
        bass.append(Note(start=t0, end=t1 - 0.05, pitch=root - 12, velocity=90))
        organ += [Note(start=t0, end=t1 - 0.05, pitch=p, velocity=70) for p in triad]
    for s0, _, kind in SECTIONS:
        if kind != "vocal":
            continue
        t = s0
        for pitch, beats in MELODY:
            mel.append(Note(start=t, end=t + beats * BEAT - 0.06, pitch=pitch, velocity=105))
            t += beats * BEAT
    return Score(key="C major", tempo_bpm=BPM, time_signature=(4, 4), beats=[round(i * BEAT, 4) for i in range(int(57.6 / BEAT) + 1)],
                 duration=57.6, parts=[Part(name="Melody", role="vocal", voice="melody", program=52, notes=mel),
                                       Part(name="Organ", role="instrument", voice="harmony", program=19, notes=organ),
                                       Part(name="Bass", role="instrument", voice="bass", program=19, notes=bass)])


def build(out_dir: Path, degrade: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    score = truth_score().model_dump()
    mid, clean = out_dir / "truth.mid", out_dir / "clean.wav"
    # melody on Flute (73): a clear monophonic voice-like timbre the pitch tracker can follow
    score_to_midi(score, mid, program_override={score["parts"][0]["id"]: 73})
    synth.render_midi(mid, clean, 44100)
    y, sr = sf.read(str(clean), dtype="float32", always_2d=True)
    y = y[: int(58.5 * sr)]
    if degrade:
        rng = np.random.default_rng(3)
        y = y.mean(axis=1, keepdims=True)                      # phone = mono
        y = y / (np.max(np.abs(y)) + 1e-9) * 0.5
        y = y + rng.standard_normal(y.shape).astype(np.float32) * 0.004   # room noise
    wav, mp3 = out_dir / "phone.wav", out_dir / "phone.mp3"
    sf.write(str(wav), y, sr)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-ar", "32000", "-c:a", "libmp3lame", "-b:a", "96k", str(mp3)], check=True)
    return {"mp3": mp3, "wav": wav, "clean": clean, "midi": mid, "score": score, "sections": SECTIONS, "bpm": BPM}
