"""Canonical score document.

Notes are stored in *seconds on the source recording's timeline*, not in quantised beats. That is the
whole point of a faithful reconstruction: the original performers' expressive timing is the reference.
A beat grid (`beats`, also in seconds) lets us quantise for notation export without losing the timing.
"""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

GM_PROGRAMS = {
    "Church Organ": 19, "Reed Organ": 20, "Choir Aahs (guide tone)": 52, "Voice Oohs (guide tone)": 53,
    "Acoustic Grand Piano": 0, "Harpsichord": 6, "Nylon Guitar": 24, "Steel Guitar": 25,
    "Violin": 40, "Viola": 41, "Cello": 42, "Contrabass": 43, "String Ensemble": 48, "Harp": 46,
    "Trumpet": 56, "Trombone": 57, "French Horn": 60, "Brass Section": 61,
    "Oboe": 68, "Clarinet": 71, "Flute": 73, "Recorder": 74, "Bassoon": 70, "Tubular Bells": 14,
    "Acoustic Bass": 32, "Timpani": 47,
}


def nid() -> str:
    return uuid.uuid4().hex[:10]


class Note(BaseModel):
    id: str = Field(default_factory=nid)
    start: float = Field(ge=0)
    end: float
    pitch: int = Field(ge=0, le=127)
    velocity: int = Field(default=80, ge=1, le=127)
    confidence: float | None = Field(default=None, ge=0, le=1)  # None = not machine-estimated
    lyric: str = ""
    uncertain: bool = False
    verified: bool = False

    @model_validator(mode="after")
    def _order(self):
        if self.end <= self.start:
            raise ValueError("note end must be after start")
        return self


class Part(BaseModel):
    id: str = Field(default_factory=nid)
    name: str
    role: Literal["vocal", "instrument"] = "instrument"
    voice: str = "melody"  # melody | soprano | alto | tenor | bass | harmony | accompaniment
    program: int = Field(default=19, ge=0, le=127)
    source: str = "user"  # basic-pitch | pyin | import:midi | import:musicxml | user | chord-inferred
    notes: list[Note] = []


class Chord(BaseModel):
    start: float
    end: float
    label: str
    confidence: float | None = None


class Score(BaseModel):
    key: str | None = None
    key_confidence: float | None = None
    time_signature: tuple[int, int] = (4, 4)
    meter_confidence: float | None = None
    tempo_bpm: float | None = None
    tempo_curve: list[dict] = []  # [{time, bpm}] local tempo, for expressive-timing display
    beats: list[float] = []
    downbeat_offset: int = 0
    duration: float = 0.0
    parts: list[Part] = []
    chords: list[Chord] = []
    warnings: list[str] = []

    @field_validator("beats")
    @classmethod
    def _sorted(cls, v):
        return sorted(float(x) for x in v)

    def note_count(self) -> int:
        return sum(len(p.notes) for p in self.parts)


def validate_score(data: dict) -> dict:
    """Validate + normalise (sort notes, clamp). Raises pydantic.ValidationError."""
    s = Score.model_validate(data)
    for p in s.parts:
        p.notes.sort(key=lambda n: (n.start, n.pitch))
    if s.parts:
        s.duration = max(s.duration, max((n.end for p in s.parts for n in p.notes), default=0.0))
    return s.model_dump()


NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def pitch_name(p: int) -> str:
    return f"{NOTE_NAMES[p % 12]}{p // 12 - 1}"
