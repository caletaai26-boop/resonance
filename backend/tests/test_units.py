"""Pure-function tests: signal measurements, DSP, score I/O, comparison metrics, language helpers."""
import numpy as np
import pytest

from resonance.audio import analysis as an
from resonance.audio import dsp
from resonance.agents.fidelity import compare_scores, melody_vs_audio
from resonance.music import language as lang
from resonance.score import io as sio
from resonance.score.model import validate_score

SR = 44100


def sine(freq=440.0, dur=3.0, amp=0.5, sr=SR):
    t = np.arange(int(dur * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float64)


# ------------------------------------------------------------------ measurements
def test_clipping_detected_only_when_present():
    clean = sine(amp=0.5)[:, None]
    assert an.clipping(clean, SR)["events"] == 0
    clipped = np.clip(sine(amp=1.6), -1.0, 1.0)[:, None]
    c = an.clipping(clipped, SR)
    assert c["events"] > 100 and c["clipped_samples"] > c["events"] and c["times"][0] < 0.01


def test_silent_regions_finds_gap():
    y = sine(dur=4.0)
    y[int(1.5 * SR): int(2.5 * SR)] = 0.0
    regions = an.silent_regions(y, SR)
    assert len(regions) == 1
    assert abs(regions[0]["start"] - 1.5) < 0.1 and abs(regions[0]["end"] - 2.5) < 0.1


def test_true_peak_at_least_sample_peak_and_catches_intersample_overs():
    # fs/4 sine sampled at 45° phase: every sample is 0.707 of the real peak
    n = np.arange(SR)
    y = np.sin(2 * np.pi * (SR / 4) * n / SR + np.pi / 4)[:, None]
    sp, tp = an.sample_peak_db(y), an.true_peak_db(y, SR)
    assert tp >= sp
    assert sp == pytest.approx(-3.01, abs=0.1) and tp > -0.6


def test_integrated_lufs_of_known_sine():
    # BS.1770: a 0 dBFS 997 Hz mono sine reads -3.01 LKFS, so -20 dBFS reads about -23.0
    y = sine(997.0, dur=5.0, amp=0.1)[:, None]
    assert an.integrated_lufs(y, SR) == pytest.approx(-23.0, abs=0.3)
    assert an.integrated_lufs(np.zeros((1000, 1)), SR) is None


def test_stereo_summary_mono_and_wide():
    a = sine()
    assert an.stereo_summary(a[:, None])["is_stereo"] is False
    assert an.stereo_summary(np.stack([a, a], axis=1))["effectively_mono"] is True
    assert an.stereo_summary(np.stack([a, -a], axis=1))["correlation"] < -0.99


def test_noise_floor_estimate_tracks_added_noise():
    rng = np.random.default_rng(0)
    y = np.concatenate([np.zeros(SR), sine(dur=3.0)])
    quiet = an.noise_floor(y + rng.standard_normal(len(y)) * 0.0005, SR)
    loud = an.noise_floor(y + rng.standard_normal(len(y)) * 0.02, SR)
    assert loud["noise_floor_db"] > quiet["noise_floor_db"] + 20
    assert loud["snr_estimate_db"] < quiet["snr_estimate_db"]


# ------------------------------------------------------------------ DSP
def test_limiter_respects_true_peak_ceiling():
    rng = np.random.default_rng(1)
    y = dsp.to_stereo(sine(220, 2.0, 1.5) + rng.standard_normal(2 * SR) * 0.3)
    out = dsp.limit(y, SR, ceiling_db=-1.0)
    assert an.true_peak_db(out, SR) <= -1.0 + 0.05
    assert out.shape == y.shape and np.isfinite(out).all()


def test_limiter_leaves_quiet_audio_untouched():
    y = dsp.to_stereo(sine(amp=0.1))
    assert np.allclose(dsp.limit(y, SR, -1.0), y, atol=1e-6)


def test_normalize_loudness_hits_target():
    y = dsp.to_stereo(sine(440, 5.0, 0.05))
    out, before = dsp.normalize_loudness(y, SR, -20.0)
    assert before is not None
    assert an.integrated_lufs(out, SR) == pytest.approx(-20.0, abs=0.5)


def test_highpass_attenuates_rumble_not_midrange():
    bands = [{"type": "highpass", "freq": 80, "q": 0.707}]
    rms = lambda x: float(np.sqrt(np.mean(x[SR // 2:] ** 2)))
    low, mid = dsp.to_stereo(sine(30, 2.0)), dsp.to_stereo(sine(1000, 2.0))
    low_att = 20 * np.log10(rms(dsp.eq(low, SR, bands)) / rms(low))
    mid_att = 20 * np.log10(rms(dsp.eq(mid, SR, bands)) / rms(mid))
    assert low_att < -12 and abs(mid_att) < 0.5


def test_peak_eq_boosts_only_its_band():
    bands = [{"type": "peak", "freq": 3200, "gain_db": 6.0, "q": 0.8}]
    rms = lambda x: float(np.sqrt(np.mean(x[SR // 2:] ** 2)))
    hit, miss = dsp.to_stereo(sine(3200, 2.0)), dsp.to_stereo(sine(200, 2.0))
    assert 20 * np.log10(rms(dsp.eq(hit, SR, bands)) / rms(hit)) == pytest.approx(6.0, abs=0.5)
    assert abs(20 * np.log10(rms(dsp.eq(miss, SR, bands)) / rms(miss))) < 0.5


def test_reverb_dry_is_identity_and_cathedral_rings_on():
    y = dsp.to_stereo(np.concatenate([sine(440, 0.5), np.zeros(SR // 10)]))
    assert dsp.reverb(y, SR, "dry") is y
    wet = dsp.reverb(y, SR, "cathedral")
    chapel = dsp.reverb(y, SR, "small_chapel")
    assert len(wet) > len(y) + 3 * SR and len(wet) > len(chapel)
    tail = wet[len(y) + SR // 2:]
    assert np.sqrt(np.mean(tail ** 2)) > 1e-5           # there is an audible tail
    assert np.allclose(wet[: len(y)] - dsp.reverb(np.zeros_like(y), SR, "cathedral")[: len(y)], wet[: len(y)])


def test_pan_law():
    m = sine(dur=0.5)
    c, l, r = dsp.pan(m, 0.0), dsp.pan(m, -1.0), dsp.pan(m, 1.0)
    assert np.allclose(c[:, 0], c[:, 1]) and np.allclose(c[:, 0], m, atol=1e-9)   # centre is unity
    assert np.max(np.abs(l[:, 1])) < 1e-9 and np.max(np.abs(r[:, 0])) < 1e-9       # hard pans are one-sided
    power = [float(np.sum(dsp.pan(m, p) ** 2)) for p in (-1, -0.5, 0, 0.5, 1)]
    assert max(power) / min(power) == pytest.approx(1.0, abs=1e-6)                 # constant power


def test_compressor_reduces_loud_passages_only():
    y = dsp.to_stereo(np.concatenate([sine(440, 1.0, 0.02), sine(440, 1.0, 0.8)]))
    out, gr = dsp.compress(y, SR, threshold_db=-20, ratio=4.0)
    assert gr > 6
    quiet_gain = np.max(np.abs(out[: SR // 2])) / np.max(np.abs(y[: SR // 2]))
    loud_gain = np.max(np.abs(out[-SR // 2:])) / np.max(np.abs(y[-SR // 2:]))
    assert quiet_gain == pytest.approx(1.0, abs=0.05) and loud_gain < 0.6


def test_spectral_denoise_lowers_noise_but_keeps_music():
    rng = np.random.default_rng(2)
    n = 6 * SR
    tone = np.zeros(n)
    tone[2 * SR:] = sine(440, 4.0, 0.3)                    # 2 s of room noise only, then music
    y = dsp.to_stereo(tone + rng.standard_normal(n) * 0.01)
    out, info = dsp.spectral_denoise(y, SR, strength=0.8)
    rms = lambda x: float(np.sqrt(np.mean(x ** 2)))
    noise_drop = 20 * np.log10(rms(out[SR // 2: SR + SR // 2]) / rms(y[SR // 2: SR + SR // 2]))
    music_drop = 20 * np.log10(rms(out[3 * SR: 5 * SR]) / rms(y[3 * SR: 5 * SR]))
    assert noise_drop < -8, noise_drop
    assert music_drop > -1.0, music_drop
    assert info["max_attenuation_db"] == pytest.approx(14.4)


# ------------------------------------------------------------------ comparison metrics
def _score(notes):
    return {"parts": [{"notes": [{"start": s, "end": s + 0.4, "pitch": p} for s, p in notes]}]}


def test_compare_scores_identical_and_shifted():
    ref = _score([(0.0, 60), (0.5, 62), (1.0, 64), (1.5, 65)])
    same = compare_scores(ref, ref)
    assert same["precision"] == same["recall"] == same["f1"] == 1.0 and not same["missing"] and not same["extra"]
    wrong = _score([(0.0, 60), (0.5, 62), (1.0, 66), (1.5, 65)])          # one wrong pitch
    r = compare_scores(ref, wrong)
    assert r["matched"] == 3 and r["missing"] == [{"time": 1.0, "pitch": 64}] and r["extra"] == [{"time": 1.0, "pitch": 66}]
    late = _score([(0.3, 60), (0.8, 62), (1.3, 64), (1.8, 65)])           # right notes, 300 ms late
    assert compare_scores(ref, late, onset_tol=0.08)["matched"] == 0
    assert compare_scores(ref, late, onset_tol=0.35)["matched"] == 4
    assert compare_scores(ref, _score([]))["recall"] == 0.0


def test_compare_scores_does_not_double_count():
    ref = _score([(0.0, 60)])
    other = _score([(0.0, 60), (0.02, 60)])
    r = compare_scores(ref, other)
    assert r["matched"] == 1 and len(r["extra"]) == 1 and r["precision"] == 0.5


def test_melody_vs_audio_classifies_agree_differ_octave_unverifiable():
    times = np.arange(0, 4.0, 0.01)
    hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)
    f0 = np.zeros_like(times)
    prob = np.full_like(times, 0.9)
    f0[(times >= 0) & (times < 1)] = hz(60)        # note 1 heard as written
    f0[(times >= 1) & (times < 2)] = hz(64)        # note 2 written 62, heard 64
    f0[(times >= 2) & (times < 3)] = hz(55)        # note 3 written 67, heard an octave lower
    # 3-4 s: unvoiced
    notes = [{"id": "a", "start": 0.0, "end": 1.0, "pitch": 60}, {"id": "b", "start": 1.0, "end": 2.0, "pitch": 62},
             {"id": "c", "start": 2.0, "end": 3.0, "pitch": 67}, {"id": "d", "start": 3.0, "end": 4.0, "pitch": 60}]
    r = melody_vs_audio(notes, times, f0, prob)
    assert (r["agree"], r["differ"], r["octave_differences"], r["unverifiable"]) == (1, 1, 1, 1)
    assert r["agreement"] == pytest.approx(1 / 3, abs=0.01)
    kinds = {row["note"]["id"]: (row["kind"], row["heard"]) for row in r["rows"]}
    assert kinds == {"b": ("pitch", 64), "c": ("octave", 55)}


# ------------------------------------------------------------------ score I/O
def test_score_validation_rejects_impossible_notes():
    from pydantic import ValidationError
    good = {"parts": [{"name": "P", "notes": [{"start": 1.0, "end": 2.0, "pitch": 60}, {"start": 0.0, "end": 0.5, "pitch": 62}]}]}
    out = validate_score(good)
    assert [n["start"] for n in out["parts"][0]["notes"]] == [0.0, 1.0] and out["duration"] == 2.0   # sorted, duration derived
    for bad in ({"start": 1.0, "end": 1.0, "pitch": 60}, {"start": 0.0, "end": 1.0, "pitch": 200}, {"start": -1.0, "end": 1.0, "pitch": 60}):
        with pytest.raises(ValidationError):
            validate_score({"parts": [{"name": "P", "notes": [bad]}]})


def test_midi_round_trip_preserves_notes_exactly(tmp_path):
    import fixtures
    truth = fixtures.truth_score().model_dump()
    mid = tmp_path / "t.mid"
    n = sio.score_to_midi(truth, mid)
    back = sio.midi_to_score(mid)
    assert n == sum(len(p["notes"]) for p in truth["parts"]) == sum(len(p["notes"]) for p in back["parts"])
    assert len(back["parts"]) == 3
    for a, b in zip(truth["parts"], back["parts"]):
        ka = sorted((x["pitch"], x["start"], x["end"]) for x in a["notes"])
        kb = sorted((x["pitch"], x["start"], x["end"]) for x in b["notes"])
        for (pa, sa, ea), (pb, sb, eb) in zip(ka, kb):
            assert pa == pb and abs(sa - sb) < 0.010 and abs(ea - eb) < 0.010
    assert back["tempo_bpm"] == pytest.approx(100.0, abs=0.01)
    assert back["parts"][0]["role"] == "vocal"            # choir patch is recognised as a vocal part
    assert back["parts"][1]["role"] == "instrument"


def test_midi_export_can_select_parts_and_override_program(tmp_path):
    import pretty_midi
    import fixtures
    truth = fixtures.truth_score().model_dump()
    pid = truth["parts"][2]["id"]
    sio.score_to_midi(truth, tmp_path / "b.mid", part_ids=[pid], program_override={pid: 42})
    pm = pretty_midi.PrettyMIDI(str(tmp_path / "b.mid"))
    assert len(pm.instruments) == 1 and pm.instruments[0].program == 42
    assert len(pm.instruments[0].notes) == len(truth["parts"][2]["notes"])


def test_musicxml_export_then_import(tmp_path):
    from music21 import converter
    import fixtures
    truth = fixtures.truth_score().model_dump()
    truth["parts"][0]["notes"][0]["lyric"] = "A-"
    xml = tmp_path / "t.musicxml"
    stats = sio.score_to_musicxml(truth, xml, "Fixture")
    assert stats["parts"] == 3 and stats["notes"] == sum(len(p["notes"]) for p in truth["parts"])
    parsed = converter.parse(str(xml))
    assert len(parsed.parts) == 3
    assert "A-" in xml.read_text() or "<text>A</text>" in xml.read_text()
    back = sio.musicxml_to_score(xml)
    assert len(back["parts"]) == 3 and back["tempo_bpm"] == pytest.approx(100, abs=1)
    mel_truth = [n["pitch"] for n in truth["parts"][0]["notes"]]
    mel_back = [n["pitch"] for n in back["parts"][0]["notes"]]
    assert mel_back == mel_truth                                   # same melody, in order
    # quantised to 16ths at 100 BPM: onsets land within one 16th (0.15 s) of the performance
    first_truth, first_back = truth["parts"][0]["notes"][0]["start"], back["parts"][0]["notes"][0]["start"]
    assert abs(first_truth - first_back) < 0.16


def test_musicxml_import_small_handwritten_file(tmp_path):
    xml = tmp_path / "s.musicxml"
    xml.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1"><part-list><score-part id="P1"><part-name>Soprano</part-name></score-part></part-list>
<part id="P1"><measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
<direction><sound tempo="120"/><direction-type><metronome><beat-unit>quarter</beat-unit><per-minute>120</per-minute></metronome></direction-type></direction>
<note><pitch><step>G</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><lyric><text>A</text></lyric></note>
<note><pitch><step>A</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type><tie type="start"/></note>
<note><pitch><step>A</step><octave>4</octave></pitch><duration>2</duration><type>half</type><tie type="stop"/></note>
</measure></part></score-partwise>""")
    s = sio.musicxml_to_score(xml)
    part = s["parts"][0]
    assert part["role"] == "vocal" and part["voice"] == "soprano"
    assert [(n["pitch"], round(n["start"], 2), round(n["end"], 2)) for n in part["notes"]] == [(67, 0.0, 0.5), (69, 0.5, 2.0)]  # tie merged
    assert part["notes"][0]["lyric"] == "A"


# ------------------------------------------------------------------ language
@pytest.mark.parametrize("word,expected", [
    ("adoremus", ["a", "do", "re", "mus"]), ("triumphantes", ["tri", "um", "phan", "tes"]), ("venite", ["ve", "ni", "te"]),
    ("laeti", ["lae", "ti"]), ("Bethlehem", ["Beth", "le", "hem"]), ("Dominum", ["Do", "mi", "num"]), ("angelorum", ["an", "ge", "lo", "rum"]),
])
def test_latin_syllabification(word, expected):
    assert lang.syllabify(word, "la") == expected


def test_line_syllables_mark_word_continuations():
    assert lang.line_syllables("Venite adoremus", "la") == ["Ve-", "ni-", "te", "a-", "do-", "re-", "mus"]
    assert lang.line_syllables("", "la") == []


def test_latin_pronunciation_rules():
    p = lang.latin_pronunciation
    assert p("caelestium").startswith("ch")          # c before ae is soft
    assert p("cantet").startswith("k")               # c before a is hard
    assert "ny" in p("agnus")                        # gn
    assert p("regem").count("j") == 1                # g before e is soft
    assert p("gloria").startswith("g")
    assert "ts" in p("gratia")                       # ti + vowel
    assert "h" not in p("hodierna").split("oh")[0]   # initial h is silent
    assert p("Jesu").startswith("y")


def test_pronunciation_guide_dedupes_words():
    g = lang.pronunciation_guide(["Venite adoremus,", "Venite adoremus Dominum."], "la")
    assert [e["word"].lower() for e in g] == ["venite", "adoremus", "dominum"]
    assert all(e["guide"] and e["syllables"] for e in g)
