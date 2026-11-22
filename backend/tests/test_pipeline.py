"""End-to-end and accuracy tests on the synthetic recording with known ground truth."""
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import select

import fixtures
import helpers as h
from conftest import new_project
from resonance import models as m
from resonance.agents import AGENTS
from resonance.agents.fidelity import compare_scores
from resonance.audio import analysis as an
from resonance.audio import io as aio
from resonance.db import SessionLocal
from resonance.music import analysis as mu
from resonance.storage import storage

ALL_GATES = [1, 2, 3, 4, 5, 6, 7]


# ------------------------------------------------------------------ 2. decoding + forensics
def test_forensics_measures_the_real_file(session_client, analysed):
    a = h.agent_state(session_client, analysed, "forensics")
    assert a["status"] == "succeeded" and a["job"]["progress"] == 1.0
    mt = a["output"]["data"]["metrics"]
    assert mt["duration_s"] == pytest.approx(58.5, abs=0.3)
    assert mt["channels"] == 1 and mt["sample_rate"] == 32000 and mt["codec"] == "mp3"
    assert mt["stereo"]["is_stereo"] is False
    assert -30 < mt["integrated_lufs"] < -10 and mt["true_peak_db"] >= mt["sample_peak_db"] - 0.01 and mt["true_peak_db"] < 0
    assert mt["clipping"]["events"] == 0
    assert mt["spectrum"]["rolloff_99_hz"] < 16000                     # 32 kHz MP3: nothing above 16 kHz
    assert len(mt["loudness_profile"]["level_db"]) > 20
    obs = " ".join(o["text"] for o in a["output"]["data"]["observations"])
    assert "mono" in obs
    assert "estimate" in a["output"]["data"]["uncertainty"]
    # progress steps are the ones the agent really went through
    steps = [e["msg"] for e in a["job"]["log"]]
    assert any("Measuring loudness" in s for s in steps) and any("spectrogram" in s for s in steps)


def test_forensics_artifacts_have_provenance(session_client, analysed):
    assets = h.ok(session_client.get(f"/api/projects/{analysed}/assets"))
    by = {a["kind"]: a for a in assets if a["kind"] in ("source", "working", "waveform", "spectrogram")}
    assert set(by) == {"source", "working", "waveform", "spectrogram"}
    job_id = h.agent_state(session_client, analysed, "forensics")["job"]["id"]
    for kind in ("working", "waveform", "spectrogram"):
        a = by[kind]
        assert a["source_asset_id"] == by["source"]["id"] and a["job_id"] == job_id
        assert a["method"] and a["engine"] and len(a["sha256"]) == 64 and a["size_bytes"] > 0 and a["created_at"]
    assert by["working"]["parent_asset_id"] == by["source"]["id"]
    y, sr = sf.read(io.BytesIO(session_client.get(by["working"]["url"]).content))
    assert sr == 44100 and len(y) / sr == pytest.approx(58.5, abs=0.3)
    peaks = session_client.get(by["waveform"]["url"]).json()
    assert len(peaks["peaks"]) > 1000 and 0 < max(peaks["peaks"]) <= 1.0
    assert session_client.get(by["spectrogram"]["url"]).content[:8] == b"\x89PNG\r\n\x1a\n"
    # the original is untouched by analysis
    assert by["source"]["sha256"] == hashlib.sha256(session_client.get(by["source"]["url"]).content).hexdigest()


def test_separation_is_honest_about_its_state(session_client, analysed):
    a = h.agent_state(session_client, analysed, "separation")
    assert a["status"] in ("blocked", "succeeded")
    if a["status"] == "blocked":
        assert "Demucs" in a["job"]["error"] and a["output"] is None
        stems = h.ok(session_client.get(f"/api/projects/{analysed}/assets?kind=stem"))
        assert stems == []                                             # no fake stems were produced
        st = h.ok(session_client.get(f"/api/projects/{analysed}/status"))
        assert any(b["agent"] == "Source Separation" and b["status"] == "blocked" for b in st["blockers"])
    else:
        assert {s["stem"] for s in a["output"]["data"]["stems"]} >= {"vocals", "accompaniment"}


# ------------------------------------------------------------------ 4. transcription accuracy vs ground truth
def test_tempo_key_and_meter_match_ground_truth(session_client, analysed, capsys):
    sc = h.get_doc(session_client, analysed, "score")["current"]
    d = sc["data"]
    with capsys.disabled():
        print(f"\n[accuracy] tempo {d['tempo_bpm']} BPM (truth 100), key {d['key']} conf {d['key_confidence']}, meter {d['time_signature']}")
    assert d["tempo_bpm"] == pytest.approx(100.0, rel=0.03)
    assert d["key"] == "C major"
    assert tuple(d["time_signature"]) == (4, 4)
    assert sc["origin"] == "auto-transcription" and sc["verification"] == "unverified"
    assert sc["provenance"]["engines"]["polyphonic"] and sc["provenance"]["source_asset"]
    beats = np.array(d["beats"])
    assert np.median(np.diff(beats)) == pytest.approx(0.6, abs=0.02)


def test_polyphonic_transcription_floor(session_client, analysed, fixture_audio, capsys):
    d = h.get_doc(session_client, analysed, "score")["current"]["data"]
    est = {"parts": [p for p in d["parts"] if p["source"].startswith("basic-pitch")]}
    r = compare_scores(fixture_audio["score"], est, onset_tol=0.2)
    with capsys.disabled():
        print(f"\n[accuracy] polyphonic notes: {r['other_notes']} detected vs {r['reference_notes']} true; "
              f"precision {r['precision']}, recall {r['recall']}, F1 {r['f1']}")
    assert r["f1"] >= 0.45 and r["precision"] >= 0.5
    # every machine note carries a confidence, and the transcription says it is not ground truth
    assert all(n["confidence"] is not None and not n["verified"] for p in est["parts"] for n in p["notes"])
    out = h.agent_state(session_client, analysed, "transcription")["output"]["data"]
    assert out["polyphonic_cleanup"]["raw_events"] > out["polyphonic_cleanup"]["kept"]
    assert any("automatic model" in w for w in out["warnings"])


def test_chords_follow_the_progression(session_client, analysed):
    d = h.get_doc(session_client, analysed, "score")["current"]["data"]
    truth = ["C", "F", "G", "C"]
    hits = total = 0
    for bar in range(24):
        mid = bar * 2.4 + 1.2
        lab = next((c["label"] for c in d["chords"] if c["start"] <= mid < c["end"]), None)
        total += 1
        hits += lab == truth[bar % 4]
    assert hits / total >= 0.7, f"{hits}/{total} bars correct"


def test_melody_tracking_on_a_voice_stem(fixture_audio, capsys):
    """What the melody tracker achieves when it gets what separation is supposed to give it: the voice alone."""
    y, sr = aio.load_mono(h.melody_stem_wav(fixture_audio), mu.SR)
    track = mu.pitch_track(y, sr)
    raw = mu.notes_from_pitch_track(track)
    notes = mu.split_at_onsets(raw, y, sr)
    truth = {"parts": [fixture_audio["score"]["parts"][0]]}
    r0 = compare_scores(truth, {"parts": [{"notes": raw}]}, onset_tol=0.2)
    r = compare_scores(truth, {"parts": [{"notes": notes}]}, onset_tol=0.2)
    with capsys.disabled():
        print(f"\n[accuracy] melody on voice stem: precision {r['precision']}, recall {r['recall']} "
              f"(before onset splitting: precision {r0['precision']}, recall {r0['recall']})")
    assert r["precision"] >= 0.9 and r["recall"] >= 0.8
    assert r["recall"] >= r0["recall"]
    assert all(0 < n["confidence"] < 1 for n in notes)


def test_without_a_voice_stem_the_melody_is_labelled_as_a_candidate(session_client, analysed):
    if h.agent_state(session_client, analysed, "separation")["status"] == "succeeded":
        pytest.skip("stems available")
    d = h.get_doc(session_client, analysed, "score")["current"]["data"]
    mel = [p for p in d["parts"] if p["voice"] == "melody"]
    assert len(mel) == 1 and mel[0]["source"] == "basic-pitch-skyline" and "candidate" in mel[0]["name"]
    assert any("No separated voice stem" in w for w in d["warnings"])


# ------------------------------------------------------------------ 5. structure
def test_novelty_boundaries_near_true_section_changes(fixture_audio, capsys):
    y, sr = aio.load_mono(fixture_audio["wav"], mu.SR)
    bounds, nov, _ = mu.novelty_boundaries(y, sr)
    errs = [min(abs(b - t) for b in bounds) for t in h.TRUE_BOUNDS]
    with capsys.disabled():
        print(f"\n[accuracy] section boundaries without stems: detected {[round(b, 1) for b in bounds]}, truth {h.TRUE_BOUNDS}, errors {[round(e, 1) for e in errs]} s")
    assert max(errs) <= 3.5
    assert len(bounds) <= 5                      # not one boundary per chord change


def test_structure_without_stems_does_not_guess_vocal_sections(session_client, analysed):
    if h.agent_state(session_client, analysed, "separation")["status"] == "succeeded":
        pytest.skip("stems available")
    a = h.agent_state(session_client, analysed, "structure")
    secs = a["output"]["data"]["sections"]
    assert all(s["kind"] == "unknown" and s["status"] == "detected" for s in secs)
    assert secs[0]["start"] == 0 and secs[-1]["end"] == pytest.approx(58.5, abs=0.3)
    assert all(x["end"] == pytest.approx(y_["start"], abs=0.02) for x, y_ in zip(secs, secs[1:]))      # contiguous
    assert "cannot tell a verse from a refrain" in a["output"]["data"]["uncertainty"]


@pytest.fixture(scope="module")
def stem_project(session_client, fixture_audio):
    """A project with a (simulated) separated voice stem: structure + stem-based melody transcription."""
    c = session_client
    pid = new_project(c, "With voice stem")["id"]
    h.ok(h.upload_source(c, pid, fixture_audio["mp3"]))
    assert h.run_agent(c, pid, "forensics")["status"] == "succeeded"
    h.inject_vocal_stem(pid, h.melody_stem_wav(fixture_audio))
    assert h.run_agent(c, pid, "structure")["status"] == "succeeded"
    return pid


def test_structure_with_voice_stem_tags_sections(session_client, stem_project, capsys):
    secs = h.get_doc(session_client, stem_project, "sections")["current"]["data"]["sections"]
    starts = [s["start"] for s in secs[1:]]
    errs = [min(abs(b - t) for b in starts) for t in h.TRUE_BOUNDS]
    with capsys.disabled():
        print(f"\n[accuracy] section boundaries with voice stem: {starts}, errors {[round(e, 2) for e in errs]} s; kinds {[s['kind'] for s in secs]}")
    assert max(errs) <= 1.5
    for s in secs:
        mid = (s["start"] + s["end"]) / 2
        truth_kind = next(k for a, b, k in fixtures.SECTIONS + [(57.6, 99, "vocal")] if a <= mid < b)
        if s["end"] - s["start"] > 5 and mid < 57:
            assert s["kind"] == truth_kind, s
    assert secs[0]["label"] == "Instrumental introduction" and secs[0]["kind"] == "instrumental"
    assert sum(1 for s in secs if s["kind"] == "vocal") >= 2


# ------------------------------------------------------------------ 6. score import endpoint
def test_imported_score_takes_priority_over_auto_transcription(client, fixture_audio, monkeypatch):
    pid = new_project(client, "import priority")["id"]
    h.ok(h.upload_source(client, pid, fixture_audio["mp3"]))
    assert h.run_agent(client, pid, "forensics")["status"] == "succeeded"
    r = h.ok(h.import_midi(client, pid, h.truth_midi_bytes(Path(fixture_audio["mp3"]).parent)))
    imported = r["score_version"]
    assert imported["origin"] == "import:midi" and imported["version"] == 1 and "priority" in r["message"]
    assert sum(len(p["notes"]) for p in imported["data"]["parts"]) == sum(len(p["notes"]) for p in fixture_audio["score"]["parts"])
    assert r["asset"]["kind"] == "score_import" and imported["provenance"]["sha256"] == r["asset"]["sha256"]
    monkeypatch.setattr(mu, "basic_pitch_available", lambda: (False, "disabled to keep the test fast"))
    t = h.run_agent(client, pid, "transcription")
    assert t["status"] == "succeeded" and t["output"]["data"]["became_current"] is False
    doc = h.get_doc(client, pid, "score")
    assert doc["head_id"] == imported["id"]                                    # still the imported score
    assert [v["origin"] for v in doc["versions"]] == ["auto-transcription", "import:midi"]   # the estimate is kept as an alternative
    assert any("skipped" in w for w in t["output"]["data"]["warnings"])


def test_pdf_is_stored_as_reference_only_and_garbage_is_rejected(client):
    pid = new_project(client, "pdf + garbage")["id"]
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    r = client.post(f"/api/projects/{pid}/score/import", files={"file": ("score.pdf", io.BytesIO(pdf), "application/pdf")})
    body = h.ok(r)
    assert body["score_version"] is None and body["asset"]["kind"] == "score_reference_pdf" and "NOT converted" in body["message"]
    assert h.get_doc(client, pid, "score")["versions"] == []
    r = client.post(f"/api/projects/{pid}/score/import", files={"file": ("bad.mid", io.BytesIO(b"not a midi file at all" * 20), "audio/midi")})
    assert r.status_code == 422 and "could not be read" in r.json()["detail"]
    r = client.post(f"/api/projects/{pid}/score/import", files={"file": ("bad.musicxml", io.BytesIO(b"<nope"), "text/xml")})
    assert r.status_code == 422
    r = client.post(f"/api/projects/{pid}/score/import", files={"file": ("score.docx", io.BytesIO(b"xx"), "application/octet-stream")})
    assert r.status_code == 415
    assert h.get_doc(client, pid, "score")["versions"] == []
    kinds = [a["kind"] for a in h.ok(client.get(f"/api/projects/{pid}/assets"))]
    assert kinds == ["score_reference_pdf"]                                    # failed imports left nothing behind


def test_musicxml_import_endpoint(client, tmp_path):
    from resonance.score.io import score_to_musicxml
    pid = new_project(client, "xml import")["id"]
    xml = tmp_path / "t.musicxml"
    score_to_musicxml(fixtures.truth_score().model_dump(), xml, "Fixture")
    r = h.ok(client.post(f"/api/projects/{pid}/score/import", files={"file": ("t.musicxml", xml.open("rb"), "application/xml")}))
    assert r["score_version"]["origin"] == "import:musicxml" and len(r["score_version"]["data"]["parts"]) == 3
    assert client.get(f"/api/projects/{pid}/score.musicxml").text.lstrip().startswith("<?xml")


# ------------------------------------------------------------------ 11. rendering + export
def test_rendered_stems_mix_and_master_are_real_audio(session_client, produced):
    recs = h.ok(session_client.get(f"/api/projects/{produced}/reconstructions"))
    assert len(recs) == 1
    rec = recs[0]
    assert rec["status"] == "mastered" and len(rec["stems"]) == 3 and rec["mix"] and rec["master"]
    with SessionLocal() as db:
        for s in rec["stems"]:
            row = db.get(m.Asset, s["id"])
            y, sr = sf.read(str(storage.local_path(row.storage_key)), always_2d=True)
            assert sr == 44100 and len(y) / sr > 55
            assert float(np.sqrt(np.mean(y ** 2))) > 1e-3, s["label"]             # not silence
            assert row.engine == "fluidsynth" and row.settings["soundfont"].endswith(".sf2") and row.meta["score_version_id"] == rec["score_version_id"]
    assert rec["notes"]["guide_tone_vocals"] == ["Melody"]                          # vocal part flagged as guide tone, not choir
    out = h.agent_state(session_client, produced, "instrumental")["output"]
    assert "NOT a choir" in out["summary"]


def test_master_meets_its_loudness_and_peak_target(session_client, produced):
    r = session_client.get(f"/api/projects/{produced}/export/master_wav")
    assert r.status_code == 200 and "DRAFT" in r.headers["content-disposition"]
    y, sr = sf.read(io.BytesIO(r.content), always_2d=True)
    assert y.shape[1] == 2 and sr == 44100
    assert an.integrated_lufs(y, sr) == pytest.approx(-20.0, abs=1.0)              # 'sacred_choral' target
    assert an.true_peak_db(y, sr) <= -1.0 + 0.1
    assert an.stereo_summary(y)["correlation"] < 0.999                             # a real stereo image was created
    reported = h.agent_state(session_client, produced, "mastering")["output"]["data"]["after"]
    assert reported["integrated_lufs"] == pytest.approx(an.integrated_lufs(y, sr), abs=0.3)   # report matches the file


def test_mp3_stems_midi_xml_and_archive_exports(session_client, produced, fixture_audio, tmp_path):
    c = session_client
    opts = {o["key"]: o for o in h.ok(c.get(f"/api/projects/{produced}/exports"))["options"]}
    assert opts["pdf"]["available"] is False and "MuseScore" in opts["pdf"]["reason"]
    assert c.get(f"/api/projects/{produced}/export/pdf").status_code == 409        # says why instead of a fake file
    mp3 = tmp_path / "m.mp3"
    mp3.write_bytes(c.get(opts["master_mp3"]["url"]).content)
    probe = aio.ffprobe(mp3)
    assert probe["codec"] == "mp3" and probe["channels"] == 2 and probe["duration"] > 55
    z = zipfile.ZipFile(io.BytesIO(c.get(opts["stems"]["url"]).content))
    assert len([n for n in z.namelist() if n.endswith(".wav")]) == 3
    import pretty_midi
    mid = tmp_path / "s.mid"
    mid.write_bytes(c.get(opts["midi"]["url"]).content)
    pm = pretty_midi.PrettyMIDI(str(mid))
    assert sum(len(i.notes) for i in pm.instruments) == sum(len(p["notes"]) for p in fixture_audio["score"]["parts"])
    assert b"<score-partwise" in c.get(opts["musicxml"]["url"]).content
    rep = c.get(opts["fidelity"]["url"]).json()
    assert rep["checks"] and "verdict" in rep
    arc = zipfile.ZipFile(io.BytesIO(c.get(opts["archive"]["url"]).content))
    names = arc.namelist()
    manifest = json.loads(arc.read("manifest.json"))
    assert manifest["status"] == "DRAFT" and manifest["project"]["title"] == "Produced fixture"
    src = next(n for n in names if n.startswith("source/"))
    assert hashlib.sha256(arc.read(src)).hexdigest() == hashlib.sha256(fixture_audio["mp3"].read_bytes()).hexdigest()
    assert any(n.startswith("score/") and "CURRENT" in n for n in names) and any(n.startswith("master/") for n in names)
    assert all(a["sha256"] and a["method"] for a in manifest["assets"])
    recent = h.ok(c.get(f"/api/projects/{produced}/exports"))["recent"]
    assert len(recent) >= 5                                                        # exports are logged


def test_exports_unavailable_before_the_work_exists(client):
    pid = new_project(client, "nothing yet")["id"]
    opts = {o["key"]: o for o in h.ok(client.get(f"/api/projects/{pid}/exports"))["options"]}
    for key in ("master_wav", "master_mp3", "stems", "midi", "musicxml", "lyrics", "fidelity", "pdf"):
        assert opts[key]["available"] is False and opts[key]["reason"] and opts[key]["url"] is None
        r = client.get(f"/api/projects/{pid}/export/{key}")
        assert r.status_code == 409 and r.json()["detail"] == opts[key]["reason"]
    assert client.get(f"/api/projects/{pid}/export/unknown").status_code == 404


# ------------------------------------------------------------------ 12. fidelity
def test_fidelity_report_is_itemised_and_never_certifies(session_client, produced):
    reps = h.ok(session_client.get(f"/api/projects/{produced}/fidelity"))
    d = reps[0]["data"]
    checks = {c["key"]: c for c in d["checks"]}
    assert {"duration", "alignment", "harmony", "melody", "tempo", "lyrics", "section_order", "unverified_notes"} <= set(checks)
    assert all(c["status"] in ("pass", "warn", "info", "not_evaluated") and c["detail"] for c in d["checks"])
    assert "does not certify" in d["verdict"] and "listening review is required" in d["verdict"]
    assert checks["lyrics"]["status"] == "not_evaluated"                 # no lyric engine: says so
    assert checks["melody"]["status"] == "not_evaluated"                 # no voice stem: says so, no false alarms
    assert checks["duration"]["status"] == "pass" and checks["tempo"]["status"] == "pass"
    assert checks["alignment"]["data"]["median_time_deviation_s"] < 0.5 and d["shared_timeline"] is True
    assert d["alignment_map"][0][0] == 0 and d["alignment_map"][-1][0] > 55
    assert not [x for x in d["discrepancies"] if x["type"] == "missing_section"]
    secs = checks["harmony"]["data"]["sections"]
    assert len(secs) == 4 and all(s["reconstruction_has_sound"] and s["harmonic_similarity"] > 0.7 for s in secs)
    summary = h.agent_state(session_client, produced, "fidelity")["output"]["summary"]
    assert "not a certificate of exactness" in summary
    text = json.dumps(d).lower()
    for claim in ("is exact", "exact match", "identical to the original", "100% faithful"):
        assert claim not in text


def test_fidelity_catches_a_missing_final_section(session_client, fixture_audio):
    pid = h.build_rendered_project(session_client, fixture_audio, "Missing last section", drop_after=38.4)
    d = h.ok(session_client.get(f"/api/projects/{pid}/fidelity"))[0]["data"]
    missing = [x for x in d["discrepancies"] if x["type"] == "missing_section"]
    assert len(missing) == 1, [(x["type"], x["time"]) for x in d["discrepancies"]]
    assert missing[0]["time"] == pytest.approx(38.4, abs=0.1) and missing[0]["severity"] == "error" and "Verse 2" in missing[0]["original"]
    assert any(x["type"] == "ending" for x in d["discrepancies"])
    assert next(c for c in d["checks"] if c["key"] == "duration")["status"] == "warn"


def test_stem_based_melody_check_finds_a_wrong_note_and_correction_is_versioned(session_client, stem_project, fixture_audio, monkeypatch):
    c, pid = session_client, stem_project
    monkeypatch.setattr(mu, "basic_pitch_available", lambda: (False, "disabled to keep the test fast"))
    t = h.run_agent(c, pid, "transcription")
    assert t["status"] == "succeeded"
    score = h.get_doc(c, pid, "score")["current"]
    mel = score["data"]["parts"][0]
    assert mel["source"] == "pyin" and mel["role"] == "vocal"
    r = compare_scores({"parts": [fixture_audio["score"]["parts"][0]]}, {"parts": [mel]}, onset_tol=0.2)
    assert r["precision"] >= 0.9 and r["recall"] >= 0.8                   # the agent, not just the function
    # corrupt one confidently-heard long note by a third, as a transcription slip would
    target = max(mel["notes"][3:20], key=lambda n: n["end"] - n["start"])
    true_pitch = target["pitch"]
    target["pitch"] = true_pitch + 4
    v2 = h.ok(h.put_doc(c, pid, "score", score["data"], "introduce a wrong note"))["current"]
    assert h.run_agent(c, pid, "arrangement")["status"] == "succeeded"
    h.ok(c.post(f"/api/projects/{pid}/pipeline/produce"))
    h.drain()
    rep = h.ok(c.get(f"/api/projects/{pid}/fidelity"))[0]
    melody = next(x for x in rep["data"]["checks"] if x["key"] == "melody")
    assert melody["status"] in ("pass", "warn", "info") and melody["data"]["checked"] > 20 and melody["data"]["differ"] >= 1
    hits = [x for x in rep["data"]["discrepancies"] if x["type"] == "melody_pitch" and (x["fix"] or {}).get("note_id") == target["id"]]
    assert len(hits) == 1, [(x["type"], x["time"]) for x in rep["data"]["discrepancies"]]
    disc = hits[0]
    assert disc["fix"]["pitch"] == true_pitch and disc["status"] == "open" and disc["time"] == pytest.approx(target["start"], abs=0.01)
    # rejecting changes nothing musical
    rj = h.ok(c.post(f"/api/projects/{pid}/fidelity/{rep['id']}/discrepancies/{disc['id']}", json={"action": "reject"}))
    assert rj["applied"] is False and rj["discrepancy"]["status"] == "rejected"
    assert h.get_doc(c, pid, "score")["head_id"] == v2["id"]
    h.ok(c.post(f"/api/projects/{pid}/fidelity/{rep['id']}/discrepancies/{disc['id']}", json={"action": "reopen"}))
    # approving applies the fix as a NEW score version and invalidates what depended on the old one
    for g in ALL_GATES[:6]:
        assert h.approve(c, pid, g).status_code == 200, g
    ap = h.ok(c.post(f"/api/projects/{pid}/fidelity/{rep['id']}/discrepancies/{disc['id']}", json={"action": "approve"}))
    assert ap["applied"] is True and ap["score_version"]["version"] == v2["version"] + 1
    assert set(ap["invalidation"]["gates_invalidated"]) == {3, 4, 5, 6}
    new = h.get_doc(c, pid, "score")["current"]
    fixed = next(n for n in new["data"]["parts"][0]["notes"] if n["id"] == target["id"])
    assert fixed["pitch"] == true_pitch and fixed["verified"] is True and new["parent_id"] == v2["id"]
    old = h.get_doc(c, pid, "score", v2["id"])["current"]
    assert next(n for n in old["data"]["parts"][0]["notes"] if n["id"] == target["id"])["pitch"] == true_pitch + 4   # old version untouched
    stale = h.ok(c.get(f"/api/projects/{pid}/fidelity"))[0]
    assert stale["stale"] is True
    # a stale report's remaining corrections cannot be applied blindly
    other = next((x for x in stale["data"]["discrepancies"] if x["fix"] and x["id"] != disc["id"]), None)
    if other:
        assert c.post(f"/api/projects/{pid}/fidelity/{rep['id']}/discrepancies/{other['id']}", json={"action": "approve"}).status_code == 409


# ------------------------------------------------------------------ 9. gates + invalidation
def test_gates_require_real_work_and_order(client, fixture_audio):
    pid = new_project(client, "gate order")["id"]
    r = h.approve(client, pid, 1)
    assert r.status_code == 409 and "nothing to approve" in r.json()["detail"]
    h.ok(h.upload_source(client, pid, fixture_audio["mp3"]))
    assert h.run_agent(client, pid, "forensics")["status"] == "succeeded"
    r = h.approve(client, pid, 2)
    assert r.status_code == 409 and "no section timeline" in r.json()["detail"]
    h.ok(h.put_doc(client, pid, "sections", {"sections": h.TRUE_SECTIONS}))
    r = h.approve(client, pid, 2)
    assert r.status_code == 409 and "Gate 1" in r.json()["detail"]             # earlier gate still open
    assert h.approve(client, pid, 1).status_code == 200
    assert h.approve(client, pid, 2).status_code == 200
    assert h.approve(client, pid, 3).status_code == 409                         # no score
    assert h.approve(client, pid, 5).status_code == 409 and h.approve(client, pid, 7).status_code == 409
    assert h.approve(client, pid, 9).status_code == 404
    assert client.post(f"/api/projects/{pid}/gates/1", json={"decision": "maybe"}).status_code == 422
    st = h.gate_states(client, pid)
    assert st == {1: "approved", 2: "approved", 3: "pending", 4: "pending", 5: "pending", 6: "pending", 7: "pending"}
    assert h.get_doc(client, pid, "sections")["current"]["verification"] == "approved"
    hist = [e["kind"] for e in h.ok(client.get(f"/api/projects/{pid}/history"))]
    assert hist.count("gate.approved") == 2


def test_full_review_flow_and_dependency_invalidation(client, fixture_audio):
    c = client
    pid = h.build_rendered_project(c, fixture_audio, "Gates and invalidation")
    name = lambda: c.get(f"/api/projects/{pid}/export/master_wav").headers["content-disposition"]

    for g in (1, 2, 3, 4):
        assert h.approve(c, pid, g).status_code == 200, g
    # rejecting an earlier gate withdraws the approvals that were built on it
    assert h.approve(c, pid, 2, "rejected").status_code == 200
    assert h.gate_states(c, pid) == {1: "approved", 2: "rejected", 3: "invalidated", 4: "invalidated", 5: "pending", 6: "pending", 7: "pending"}
    assert h.approve(c, pid, 3).status_code == 409
    for g in (2, 3, 4, 5, 6):
        assert h.approve(c, pid, g).status_code == 200, g
    assert "DRAFT" in name()
    ok_final, why = [o for o in h.ok(c.get(f"/api/projects/{pid}/exports"))["options"] if o["key"] == "master_wav"][0]["note"], None
    assert "DRAFT" in ok_final and "Gate 7" in ok_final
    assert h.approve(c, pid, 7).status_code == 200
    assert all(v == "approved" for v in h.gate_states(c, pid).values())
    assert "FINAL" in name() and "DRAFT" not in name()
    rec = h.ok(c.get(f"/api/projects/{pid}/reconstructions"))[0]
    assert rec["status"] == "final" and rec["master"]["verification"] == "final"
    assert h.ok(c.get(f"/api/projects/{pid}/status"))["stage"] == "Final master authorised"

    # --- editing the section timeline: only structure-dependent approvals fall
    secs = [dict(s) for s in h.TRUE_SECTIONS]
    secs[1]["label"] = "Verse 1 (Adeste fideles)"
    inv = h.ok(h.put_doc(c, pid, "sections", {"sections": secs}))["invalidation"]
    assert sorted(inv["gates_invalidated"]) == [2, 6, 7]
    assert h.gate_states(c, pid) == {1: "approved", 2: "invalidated", 3: "approved", 4: "approved", 5: "approved", 6: "invalidated", 7: "invalidated"}
    assert h.ok(c.get(f"/api/projects/{pid}/fidelity"))[0]["stale"] is True
    assert h.ok(c.get(f"/api/projects/{pid}/reconstructions"))[0]["status"] == "final"          # audio itself is not stale
    assert "DRAFT" in name()                                                                     # but it is no longer exportable as final
    assert h.approve(c, pid, 2).status_code == 200
    assert h.approve(c, pid, 6).status_code == 409                                               # needs a fresh fidelity run

    # --- editing the score: everything downstream of the score is invalidated and marked stale
    score = h.get_doc(c, pid, "score")["current"]
    old_arr = h.get_doc(c, pid, "arrangement")["current"]
    score["data"]["parts"][0]["notes"][0]["pitch"] += 2
    res = h.ok(h.put_doc(c, pid, "score", score["data"], "changed the first melody note"))
    assert sorted(res["invalidation"]["gates_invalidated"]) == [3, 4, 5]
    assert res["invalidation"]["stale"]["reconstructions"] == 1 and res["invalidation"]["stale"]["arrangements"] >= 1 and res["invalidation"]["stale"]["assets"] >= 5
    st = h.gate_states(c, pid)
    assert st[1] == st[2] == "approved" and all(st[g] == "invalidated" for g in (3, 4, 5, 6, 7))
    assert h.get_doc(c, pid, "arrangement", old_arr["id"])["current"]["verification"] == "stale"
    rec = h.ok(c.get(f"/api/projects/{pid}/reconstructions"))[0]
    assert rec["status"] == "stale" and rec["master"]["verification"] == "stale" and rec["mix"]["verification"] == "stale"
    assert all(s["verification"] == "stale" for s in rec["stems"])
    assert c.get(f"/api/projects/{pid}/export/master_wav").status_code == 409                    # a stale master is not exportable
    assert c.get(f"/api/projects/{pid}/export/stems").status_code == 409
    assert h.get_doc(c, pid, "score", score["id"])["current"]["data"]["parts"][0]["notes"][0]["pitch"] == score["data"]["parts"][0]["notes"][0]["pitch"] - 2
    for g in (5, 6, 7):
        assert h.approve(c, pid, g).status_code == 409
    assert h.approve(c, pid, 3).status_code == 200
    r = h.approve(c, pid, 4)
    assert r.status_code == 409 and "older score" in r.json()["detail"]                         # stale arrangement cannot be approved

    # --- rendering refuses to use the outdated arrangement until it is rebuilt
    blocked = h.run_agent(c, pid, "instrumental")
    assert blocked["status"] == "blocked" and "Rebuild the arrangement" in blocked["job"]["error"]
    assert h.run_agent(c, pid, "mixing")["status"] == "blocked"
    assert h.run_agent(c, pid, "arrangement")["status"] == "succeeded"
    assert h.approve(c, pid, 4).status_code == 200
    h.ok(c.post(f"/api/projects/{pid}/pipeline/produce"))
    h.drain()
    recs = h.ok(c.get(f"/api/projects/{pid}/reconstructions"))
    assert [r_["version"] for r_ in recs] == [2, 1] and recs[0]["status"] == "mastered" and recs[1]["status"] == "stale"
    assert recs[0]["score_version_id"] == res["current"]["id"]
    for g in (5, 6, 7):
        assert h.approve(c, pid, g).status_code == 200, g
    assert "FINAL" in name()
    hist = h.ok(c.get(f"/api/projects/{pid}/history"))
    assert sum(1 for e in hist if e["kind"] == "invalidation") >= 2


# ------------------------------------------------------------------ 10. faithful-mode guard
def test_faithful_mode_refuses_added_material(client, fixture_audio):
    pid = h.build_scored_project(client, fixture_audio, "Faithful guard")
    assert h.run_agent(client, pid, "arrangement")["status"] == "succeeded"
    arr = h.get_doc(client, pid, "arrangement")["current"]["data"]
    assert len(arr["tracks"]) == 3 and arr["additions"] == []
    out = h.agent_state(client, pid, "arrangement")["output"]
    assert "No parts were added" in out["summary"] and all(not t["instrument_confirmed"] for t in arr["tracks"])
    extra = {**arr["tracks"][1], "id": "newtrack", "part_id": "not-in-score", "name": "Added strings", "program": 48}
    bad = {**arr, "tracks": arr["tracks"] + [extra]}
    r = h.put_doc(client, pid, "arrangement", bad)
    assert r.status_code == 409 and "Added strings" in r.json()["detail"] and "Creative" in r.json()["detail"]
    assert h.put_doc(client, pid, "arrangement", {**arr, "additions": [{"what": "descant"}]}).status_code == 409
    assert h.get_doc(client, pid, "arrangement")["current"]["version"] == 1
    # changing an existing track's instrument is allowed (it is a choice of sound, not of notes)
    arr["tracks"][1].update(program=48, instrument="String Ensemble", instrument_confirmed=True)
    assert h.put_doc(client, pid, "arrangement", arr).status_code == 200
    assert h.put_doc(client, pid, "arrangement", {**arr, "tracks": [{**arr["tracks"][0], "program": 300}]}).status_code == 422
    # the same addition is accepted once the project is explicitly switched to creative mode
    h.ok(client.patch(f"/api/projects/{pid}", json={"mode": "creative"}))
    assert h.put_doc(client, pid, "arrangement", bad).status_code == 200


# ------------------------------------------------------------------ 14. lyrics agent + underlay
def test_lyrics_agent_reference_text_is_draft_and_labelled(client, fixture_audio):
    pid = h.build_scored_project(client, fixture_audio, "Lyrics")
    empty = h.run_agent(client, pid, "lyrics")
    assert empty["status"] == "succeeded" and empty["output"]["data"]["lines"] == 0 and empty["output"]["data"]["source"] == "empty"
    assert empty["output"]["data"]["asr"]["available"] is False
    assert "recognition" in " ".join(empty["output"]["data"]["notes"]) and "Paste the words" in empty["output"]["summary"]
    assert h.get_doc(client, pid, "lyrics")["current"]["data"]["lines"] == []

    ref = h.run_agent(client, pid, "lyrics", {"reference": "adeste_fideles_la"})
    assert ref["status"] == "succeeded" and "not from the recording" in ref["output"]["summary"]
    doc = h.get_doc(client, pid, "lyrics")["current"]
    lines = doc["data"]["lines"]
    assert len(lines) == 15 and doc["version"] == 2 and doc["verification"] == "unverified"
    assert all(l["status"] == "draft" and l["uncertain"] is True for l in lines)
    assert lines[0]["text"].startswith("Adeste fideles") and lines[5]["text"] == "Venite adoremus Dominum."
    assert any("PUBLISHED REFERENCE TEXT" in n for n in doc["data"]["notes"])
    # only two vocal sections exist, so only the first two blocks get (arithmetic) timings, inside those sections
    timed = [l for l in lines if l["start"] is not None]
    assert len(timed) == 6 and all(l["start"] is None for l in lines[6:])
    assert timed[0]["start"] == pytest.approx(9.6) and timed[2]["end"] == pytest.approx(28.8) and timed[3]["start"] == pytest.approx(38.4)
    assert any("5 blocks" in n and "2 vocal sections" in n for n in doc["data"]["notes"])
    words = {p["word"].lower(): p for p in doc["data"]["pronunciation"]}
    assert words["adoremus"]["syllables"] == ["a", "do", "re", "mus"] and words["caelestium"]["guide"].startswith("ch")

    # underlay: syllables go only on vocal-part notes inside the timed line windows
    res = h.ok(client.post(f"/api/projects/{pid}/lyrics/apply-to-score"))
    assert res["stats"]["lines_aligned"] == 6 and res["stats"]["syllables_placed"] > 20 and "draft underlay" in res["message"]
    score = res["current"]["data"]
    vocal = next(p for p in score["parts"] if p["role"] == "vocal")
    with_lyrics = [n for n in vocal["notes"] if n["lyric"]]
    assert with_lyrics and all(any(l["start"] - 0.05 <= n["start"] < l["end"] for l in timed) for n in with_lyrics)
    assert all(n["uncertain"] for n in with_lyrics)
    assert vocal["notes"][0]["lyric"] == "A-" and vocal["notes"][1]["lyric"] == "des-"
    assert all(not n["lyric"] for p in score["parts"] if p["role"] != "vocal" for n in p["notes"])
    assert res["current"]["version"] == 2 and res["current"]["origin"] == "user-edit"

    typed = h.run_agent(client, pid, "lyrics", {"text": "Adeste fideles\nlaeti triumphantes\n\nVenite adoremus"})
    d = h.get_doc(client, pid, "lyrics")["current"]["data"]
    assert d["source"] == "user-typed" and [l["text"] for l in d["lines"]] == ["Adeste fideles", "laeti triumphantes", "Venite adoremus"]
    assert h.agent_state(client, pid, "lyrics")["output"]["data"]["source"] == "user-typed"
    # user edits to lyrics are versioned and validated
    d["lines"][0]["status"] = "verified"
    assert h.ok(h.put_doc(client, pid, "lyrics", d))["current"]["version"] == 4
    opts = {o["key"]: o for o in h.ok(client.get(f"/api/projects/{pid}/exports"))["options"]}
    txt = client.get(opts["lyrics"]["url"]).text
    assert "Adeste fideles" in txt and "(unverified)" in txt and "Pronunciation guide" in txt


def test_choir_agent_never_claims_a_choir(client, fixture_audio):
    pid = h.build_scored_project(client, fixture_audio, "Choir pack")
    assert h.run_agent(client, pid, "choir")["status"] == "blocked"            # needs an arrangement first
    assert h.run_agent(client, pid, "arrangement")["status"] == "succeeded"
    a = h.run_agent(client, pid, "choir")
    d = a["output"]["data"]
    assert a["status"] == "succeeded" and d["choir_generated"] is False and "No automatic choir was produced" in a["output"]["summary"]
    assert [e["available"] for e in d["engines"]] == [False, True]
    assert len(d["rehearsal_tracks"]) == 1 and d["vocal_tracks"] == ["Melody"]
    reh = h.ok(client.get(f"/api/projects/{pid}/assets?kind=rehearsal"))
    y, sr = sf.read(io.BytesIO(client.get(reh[0]["url"]).content), always_2d=True)
    assert float(np.sqrt(np.mean(y ** 2))) > 1e-3 and "piano" in reh[0]["label"].lower()


def test_human_stem_replaces_guide_tone(client, fixture_audio):
    pid = h.build_scored_project(client, fixture_audio, "Human stem")
    assert h.run_agent(client, pid, "arrangement")["status"] == "succeeded"
    arr = h.get_doc(client, pid, "arrangement")["current"]["data"]
    vocal = next(t for t in arr["tracks"] if t["role"] == "vocal")
    assert vocal["rendering"] == "guide-tone"
    wav = h.melody_stem_wav(fixture_audio)
    r = client.post(f"/api/projects/{pid}/human-stem", data={"track_id": "nope"}, files={"file": ("take.wav", wav.open("rb"), "audio/wav")})
    assert r.status_code == 404
    r = h.ok(client.post(f"/api/projects/{pid}/human-stem", data={"track_id": vocal["id"]}, files={"file": ("take.wav", wav.open("rb"), "audio/wav")}))
    t = next(t for t in r["arrangement"]["data"]["tracks"] if t["id"] == vocal["id"])
    assert t["rendering"] == "human-stem" and t["human_stem_asset_id"] == r["asset"]["id"] and r["arrangement"]["version"] == 2
    out = h.run_agent(client, pid, "instrumental")
    assert out["status"] == "succeeded" and out["output"]["data"]["guide_tone_vocals"] == []
    assert "NOT a choir" not in out["output"]["summary"]
    stems = h.ok(client.get(f"/api/projects/{pid}/reconstructions"))[0]["stems"]
    assert any(s["meta"]["rendering"] == "human-stem" and s["engine"] == "import" for s in stems)


# ------------------------------------------------------------------ 15. restore mode
def test_restore_mode_end_to_end(client, fixture_audio):
    c = client
    pid = new_project(c, "Restore only", mode="restore")["id"]
    h.ok(h.upload_source(c, pid, fixture_audio["mp3"]))
    assert h.run_agent(c, pid, "forensics")["status"] == "succeeded"
    assert list(h.gate_states(c, pid)) == [1, 5, 7]
    assert "restore" in [a["key"] for a in h.ok(c.get(f"/api/projects/{pid}/agents"))]
    for g in (2, 3, 4, 6):
        assert h.approve(c, pid, g).status_code == 409                         # not applicable / nothing to approve
    started = h.ok(c.post(f"/api/projects/{pid}/pipeline/produce", json={"params": {"restore": {"strength": 0.7}}}))
    assert [j["agent"] for j in started] == ["restore", "mastering"]
    h.drain()
    rs = h.agent_state(c, pid, "restore")
    assert rs["status"] == "succeeded" and rs["output"]["data"]["denoise"]["max_attenuation_db"] == pytest.approx(12.6)
    assert "performance itself is untouched" in rs["output"]["summary"]
    assert h.agent_state(c, pid, "mastering")["status"] == "succeeded"
    assert h.run_agent(c, pid, "mixing")["status"] == "blocked"                # nothing to mix in restore mode
    rec = h.ok(c.get(f"/api/projects/{pid}/reconstructions"))[0]
    assert rec["mode"] == "restore" and rec["stems"] == [] and rec["status"] == "mastered"
    for g in (1, 5):
        assert h.approve(c, pid, g).status_code == 200
    r = c.get(f"/api/projects/{pid}/export/master_wav")
    assert "DRAFT" in r.headers["content-disposition"]
    assert h.approve(c, pid, 7).status_code == 200
    r = c.get(f"/api/projects/{pid}/export/master_wav")
    assert "FINAL" in r.headers["content-disposition"]
    y, sr = sf.read(io.BytesIO(r.content), always_2d=True)
    assert len(y) / sr == pytest.approx(58.5, abs=0.3)                          # same performance, same length
    assert an.integrated_lufs(y, sr) == pytest.approx(-20.0, abs=1.0) and an.true_peak_db(y, sr) <= -0.9
    # the restored audio is still the original performance: sample-aligned and correlated with the working copy
    with SessionLocal() as db:
        work = db.execute(select(m.Asset).where(m.Asset.project_id == pid, m.Asset.kind == "working")).scalars().one()
        w, _ = sf.read(str(storage.local_path(work.storage_key)), always_2d=True)
    n = min(len(w), len(y))
    a, b = w[:n, 0], y[:n].mean(axis=1)
    assert float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))) > 0.7     # (EQ phase shift keeps this below 1)
    # the noise-only tail got quieter relative to the music
    def gap_db(x):
        music = np.sqrt(np.mean(x[10 * sr: 50 * sr] ** 2)); tail = np.sqrt(np.mean(x[int(58.0 * sr): int(58.4 * sr)] ** 2))
        return 20 * np.log10(music / (tail + 1e-12))
    assert gap_db(b) > gap_db(a) + 3


def test_dashboard_and_capabilities_reflect_real_state(session_client, produced, analysed):
    d = h.ok(session_client.get("/api/dashboard"))
    titles = [p["title"] for p in d["projects"]]
    assert "Produced fixture" in titles and "Analysed fixture" in titles
    assert any(a["title"] == "Produced fixture" and a["gate"] == 1 for a in d["pending_approvals"])
    assert all(j["status"] in ("queued", "running", "succeeded", "failed", "blocked", "cancelled") for j in d["processing"])
    caps = {e["name"]: e for e in h.ok(session_client.get("/api/capabilities"))["engines"]}
    assert caps["Latin SATB choir engine"]["status"] == "needs_setup" and "none is simulated" in caps["Latin SATB choir engine"]["detail"]
    assert caps["Suno or similar"]["status"] == "needs_setup"
    assert next(v for k, v in caps.items() if k.startswith("FluidSynth"))["status"] == "ready"
    assert caps["Basic Pitch (Spotify, ONNX)"]["status"] == "ready"
