"""Score alignment: a one-pass reference score, written in a different key at a flat tempo, is placed onto a
performance with rubato. Asserts the key is found and notes land near where they were actually played."""
import copy
import subprocess

import numpy as np
import soundfile as sf

import fixtures
from conftest import drain, new_project
from resonance.agents.fidelity import compare_scores
from resonance.audio import synth
from resonance.music import analysis as mu
from resonance.score.io import score_to_midi


def warp(t):
    return t + 0.9 * np.sin(t / 6.0) + 0.012 * t ** 1.3


def test_alignment_finds_key_and_follows_rubato(session_client, tmp_path):
    c = session_client
    truth = fixtures.truth_score().model_dump()
    perf = copy.deepcopy(truth)
    for p in perf["parts"]:
        for n in p["notes"]:
            n["start"], n["end"] = float(warp(n["start"])), float(warp(n["end"]))
    score_to_midi(perf, tmp_path / "perf.mid", program_override={perf["parts"][0]["id"]: 73})
    synth.render_midi(tmp_path / "perf.mid", tmp_path / "perf.wav")
    y, sr = sf.read(str(tmp_path / "perf.wav"), always_2d=True)
    y = y.mean(axis=1, keepdims=True) / np.abs(y).max() * 0.5 + np.random.default_rng(1).standard_normal((len(y), 1)) * 0.004
    sf.write(str(tmp_path / "p2.wav"), y, sr)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "p2.wav"), "-b:a", "96k", str(tmp_path / "perf.mp3")], check=True)
    s0, s1 = 9.6, 28.8
    ref = copy.deepcopy(truth)
    for p in ref["parts"]:
        p["notes"] = [{**n, "start": n["start"] - s0, "end": n["end"] - s0, "pitch": n["pitch"] - 2} for n in p["notes"] if s0 <= n["start"] < s1]
    score_to_midi(ref, tmp_path / "ref.mid")

    pid = new_project(c, "alignment")["id"]
    c.post(f"/api/projects/{pid}/source", files={"file": ("perf.mp3", open(tmp_path / "perf.mp3", "rb"), "audio/mpeg")})
    c.post(f"/api/projects/{pid}/agents/forensics/run"); drain()
    assert c.post(f"/api/projects/{pid}/score/import", files={"file": ("ref.mid", open(tmp_path / "ref.mid", "rb"), "audio/midi")}).status_code == 201
    secs = [{"start": round(float(warp(a)), 2), "end": round(float(warp(b)), 2), "label": f"S{i}", "kind": k, "group": "A", "status": "verified"}
            for i, (a, b, k) in enumerate(fixtures.SECTIONS)]
    secs[-1]["end"] = round(len(y) / sr, 2)
    assert c.put(f"/api/projects/{pid}/doc/sections", json={"data": {"sections": secs}}).status_code == 200
    c.post(f"/api/projects/{pid}/agents/alignment/run"); drain()
    a = next(x for x in c.get(f"/api/projects/{pid}/agents").json() if x["key"] == "alignment")
    assert a["status"] == "succeeded", a["job"]
    assert a["output"]["data"]["transpose"] == 2
    doc = c.get(f"/api/projects/{pid}/doc/score").json()["current"]
    assert doc["origin"].startswith("aligned:")
    sc = doc["data"]
    # sung melody goes only into the sections marked as voices
    for n in sc["parts"][0]["notes"]:
        assert any(s["start"] - 0.5 <= n["start"] < s["end"] and s["kind"] == "vocal" for s in secs)
    S = lambda parts: {"parts": [{"notes": [{"start": n["start"], "end": n["end"], "pitch": n["pitch"]} for n in p["notes"]]} for p in parts]}
    mel = compare_scores(S([perf["parts"][0]]), S([sc["parts"][0]]), 0.2)
    allp = compare_scores(S(perf["parts"]), S(sc["parts"]), 0.2)
    print(f"\n[accuracy] alignment: melody P {mel['precision']} R {mel['recall']}, all parts P {allp['precision']} R {allp['recall']} (0.2 s tolerance)")
    assert mel["recall"] >= 0.7 and mel["precision"] >= 0.7
    assert allp["recall"] >= 0.6


def test_clean_melody_merges_scoops_and_folds_octaves():
    notes = [{"start": 0.0, "end": 0.1, "pitch": 61, "confidence": 0.6}, {"start": 0.1, "end": 0.6, "pitch": 62, "confidence": 0.8},
             {"start": 0.6, "end": 0.7, "pitch": 61, "confidence": 0.5}, {"start": 0.8, "end": 1.3, "pitch": 64, "confidence": 0.8},
             {"start": 1.3, "end": 1.8, "pitch": 78, "confidence": 0.8}, {"start": 1.8, "end": 2.3, "pitch": 67, "confidence": 0.8},
             {"start": 2.3, "end": 2.8, "pitch": 65, "confidence": 0.8}]
    out, stats = mu.clean_melody(notes)
    assert [n["pitch"] for n in out] == [62, 64, 66, 67, 65]
    assert stats["merged_scoops"] == 2 and stats["octave_folded"] == 1


def test_instrumental_needs_voice_stem(session_client, tmp_path):
    """Removing the choir without a separated voice stem must block with an explanation, never fake it."""
    import fixtures as fx
    c = session_client
    out = fx.build(tmp_path / "fx")
    pid = new_project(c, "instrumental", mode="restore")["id"]
    c.post(f"/api/projects/{pid}/source", files={"file": ("phone.mp3", open(out["mp3"], "rb"), "audio/mpeg")})
    c.post(f"/api/projects/{pid}/agents/forensics/run"); drain()
    c.post(f"/api/projects/{pid}/agents/restore/run", json={"params": {"remove_voices": True}}); drain()
    a = next(x for x in c.get(f"/api/projects/{pid}/agents").json() if x["key"] == "restore")
    assert a["status"] == "blocked" and "separated voice stem" in a["job"]["error"]
