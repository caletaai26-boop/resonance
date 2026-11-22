"""Shared helpers for building projects in known states quickly (MIDI import instead of re-analysis)."""
from __future__ import annotations

import io
import json
from pathlib import Path

from sqlalchemy import select

TRUE_BOUNDS = [9.6, 28.8, 38.4]
TRUE_SECTIONS = [
    {"start": 0.0, "end": 9.6, "label": "Instrumental introduction", "kind": "instrumental"},
    {"start": 9.6, "end": 28.8, "label": "Verse 1", "kind": "vocal"},
    {"start": 28.8, "end": 38.4, "label": "Interlude", "kind": "instrumental"},
    {"start": 38.4, "end": 57.6, "label": "Verse 2", "kind": "vocal"},
]


def drain(max_jobs=60):
    from resonance.jobs import worker
    n = 0
    while n < max_jobs and worker.run_one():
        n += 1
    return n


def ok(r, codes=(200, 201, 202)):
    assert r.status_code in codes, (r.status_code, r.text[:600])
    return r.json()


def upload_source(client, pid, path: Path, name="phone.mp3"):
    with open(path, "rb") as f:
        return client.post(f"/api/projects/{pid}/source", files={"file": (name, f, "audio/mpeg")})


def run_agent(client, pid, agent, params=None):
    ok(client.post(f"/api/projects/{pid}/agents/{agent}/run", json={"params": params or {}}))
    drain()
    return agent_state(client, pid, agent)


def agent_state(client, pid, agent):
    return next(a for a in ok(client.get(f"/api/projects/{pid}/agents")) if a["key"] == agent)


def truth_midi_bytes(tmp: Path, drop_after: float | None = None) -> bytes:
    """Ground-truth score as MIDI with the melody on a choir patch (so it imports as a vocal part)."""
    import fixtures
    from resonance.score.io import score_to_midi
    score = fixtures.truth_score().model_dump()
    if drop_after is not None:
        for p in score["parts"]:
            p["notes"] = [n for n in p["notes"] if n["start"] < drop_after - 0.01]
    out = tmp / f"truth_{'full' if drop_after is None else int(drop_after)}.mid"
    score_to_midi(score, out)
    return out.read_bytes()


def import_midi(client, pid, data: bytes, name="truth.mid"):
    return client.post(f"/api/projects/{pid}/score/import", files={"file": (name, io.BytesIO(data), "audio/midi")})


def put_doc(client, pid, kind, data, note="test edit"):
    return client.put(f"/api/projects/{pid}/doc/{kind}", json={"data": data, "note": note})


def get_doc(client, pid, kind, version_id=None):
    q = f"?version_id={version_id}" if version_id else ""
    return ok(client.get(f"/api/projects/{pid}/doc/{kind}{q}"))


def gate_states(client, pid):
    return {g["gate"]: g["state"] for g in ok(client.get(f"/api/projects/{pid}/status"))["gates"]}


def approve(client, pid, gate, decision="approved"):
    return client.post(f"/api/projects/{pid}/gates/{gate}", json={"decision": decision, "comment": "test"})


def build_scored_project(client, fx, title, mode="faithful", drop_after=None):
    """Upload + forensics + imported ground-truth score + true section timeline (no slow transcription)."""
    r = client.post("/api/projects", json={"title": title, "mode": mode, "language": "la", "rights_acknowledged": True})
    pid = ok(r)["id"]
    ok(upload_source(client, pid, fx["mp3"]))
    assert run_agent(client, pid, "forensics")["status"] == "succeeded"
    ok(import_midi(client, pid, truth_midi_bytes(Path(fx["mp3"]).parent, drop_after)))
    ok(put_doc(client, pid, "sections", {"sections": TRUE_SECTIONS}))
    return pid


def build_rendered_project(client, fx, title, drop_after=None):
    pid = build_scored_project(client, fx, title, drop_after=drop_after)
    assert run_agent(client, pid, "arrangement")["status"] == "succeeded"
    ok(client.post(f"/api/projects/{pid}/pipeline/produce"))
    drain()
    for a in ("instrumental", "mixing", "mastering", "fidelity"):
        st = agent_state(client, pid, a)
        assert st["status"] == "succeeded", (a, st["job"]["error"])
    return pid


def melody_stem_wav(fx) -> Path:
    """Melody-only render = what an ideal voice-separation stem would contain."""
    from resonance.audio import synth
    from resonance.score.io import score_to_midi
    d = Path(fx["mp3"]).parent
    wav = d / "melody_stem.wav"
    if not wav.exists():
        s = fx["score"]
        mid = d / "melody_stem.mid"
        score_to_midi(s, mid, part_ids=[s["parts"][0]["id"]], program_override={s["parts"][0]["id"]: 73})
        synth.render_midi(mid, wav, 44100)
    return wav


def inject_vocal_stem(pid: str, wav: Path):
    """Simulate Demucs output by storing a stem asset directly."""
    from resonance import models as m
    from resonance.db import SessionLocal
    from resonance.storage import sha256_file, storage
    with SessionLocal() as db:
        a = m.Asset(project_id=pid, kind="stem", label="Voices (simulated for tests)", filename="vocals.wav", storage_key="",
                    mime="audio/wav", size_bytes=wav.stat().st_size, sha256=sha256_file(wav), method="test fixture", engine="test",
                    meta={"stem": "vocals"})
        db.add(a)
        db.flush()
        a.storage_key = f"{pid}/stem/{a.id}.wav"
        storage.put_file(a.storage_key, wav)
        db.commit()
        return a.id
