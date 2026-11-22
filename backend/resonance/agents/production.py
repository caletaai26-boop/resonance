from __future__ import annotations

import numpy as np
from sqlalchemy import func, select

from .. import gates
from .. import models as m
from ..audio import analysis as an
from ..audio import dsp
from ..audio import io as aio
from ..audio import synth
from ..config import settings
from ..music import language as lang
from ..score.io import score_to_midi
from ..score.model import GM_PROGRAMS
from .base import Blocked, Ctx, agent, new_version

PROGRAM_NAMES = {v: k for k, v in GM_PROGRAMS.items()}
DEFAULT_PAN = {"melody": 0.0, "soprano": -0.25, "alto": 0.25, "tenor": -0.12, "bass": 0.0, "harmony": 0.18, "accompaniment": 0.0}

MASTER_TARGETS = {
    "sacred_choral": {"label": "Sacred choral (natural dynamics)", "lufs": -20.0, "ceiling_db": -1.0,
                      "explain": "Quiet passages stay quiet. Recommended for choir and organ."},
    "broadcast_r128": {"label": "Broadcast (EBU R128)", "lufs": -23.0, "ceiling_db": -1.0, "explain": "European broadcast standard."},
    "classical_streaming": {"label": "Classical streaming", "lufs": -18.0, "ceiling_db": -1.0, "explain": "A little louder, still dynamic."},
    "streaming_loud": {"label": "General streaming (-14 LUFS)", "lufs": -14.0, "ceiling_db": -1.0,
                       "explain": "Matches pop loudness. The limiter will reduce the choir's dynamics; not recommended for sacred music."},
}


def build_tracks(score: dict, mode: str) -> list[dict]:
    """Faithful mapping: exactly one track per score part. Nothing is added."""
    tracks = []
    for p in score.get("parts", []):
        confs = [n["confidence"] for n in p["notes"] if n.get("confidence") is not None]
        vocal = p.get("role") == "vocal"
        tracks.append({
            "id": p["id"], "part_id": p["id"], "name": p["name"], "role": p.get("role", "instrument"), "voice": p.get("voice", "harmony"),
            "program": 52 if vocal else p.get("program", 19),
            "instrument": "Choir Aahs (guide tone)" if vocal else PROGRAM_NAMES.get(p.get("program", 19), f"GM program {p.get('program', 19)}"),
            "rendering": "guide-tone" if vocal else "fluidsynth", "human_stem_asset_id": None, "enabled": len(p["notes"]) > 0,
            "gain_db": 0.0 if vocal else -3.0, "pan": DEFAULT_PAN.get(p.get("voice", ""), 0.0), "source": p.get("source", ""),
            "note_count": len(p["notes"]), "mean_confidence": round(float(np.mean(confs)), 2) if confs else None,
            "instrument_confirmed": False,
        })
    return tracks


def faithful_violations(arr: dict, score: dict) -> list[str]:
    part_ids = {p["id"] for p in score.get("parts", [])}
    out = [f"Track '{t['name']}' is not based on any part of the score." for t in arr.get("tracks", []) if t.get("part_id") not in part_ids and t.get("rendering") != "human-stem"]
    if arr.get("additions"):
        out.append("The arrangement contains added material, which is only allowed in Creative Reimagining mode.")
    return out


@agent("arrangement", 6, "Arrangement Reconstruction", "Turns the current score into editable tracks with instrument assignments.")
def run_arrangement(ctx: Ctx) -> None:
    sv = ctx.head(m.ScoreVersion, "score")
    if not sv or not sv.data.get("parts"):
        raise Blocked("There is no score yet. Run Musical Transcription or import a MIDI/MusicXML score first.")
    ctx.progress(0.4, "Mapping score parts to tracks")
    prev = ctx.head(m.Arrangement, "arrangement")
    tracks = build_tracks(sv.data, ctx.project.mode)
    if prev:  # carry the user's instrument choices forward for parts that still exist
        old = {t["part_id"]: t for t in prev.data.get("tracks", [])}
        for t in tracks:
            o = old.get(t["part_id"])
            if o:
                for k in ("program", "instrument", "rendering", "human_stem_asset_id", "enabled", "gain_db", "pan", "instrument_confirmed"):
                    t[k] = o.get(k, t[k])
    data = {"mode": ctx.project.mode, "score_version_id": sv.id, "score_version": sv.version, "tracks": tracks, "additions": []}
    arr = new_version(ctx.db, ctx.project, m.Arrangement, "arrangement", data, origin="auto-arrangement",
                      note=f"Built from score v{sv.version}", provenance={"job_id": ctx.job.id, "score_version_id": sv.id})
    inv = gates.invalidate(ctx.db, ctx.project, "arrangement", "A new arrangement was built.") if prev else None
    ctx.db.commit()
    unconfirmed = [t["name"] for t in tracks if t["role"] == "instrument" and not t["instrument_confirmed"]]
    ctx.output(
        f"Built {len(tracks)} tracks from score v{sv.version}. No parts were added. "
        + (f"{len(unconfirmed)} instrumental track(s) use a default organ sound: the analysis cannot identify which instrument played them, so please confirm or change each one." if unconfirmed else ""),
        {"arrangement_id": arr.id, "tracks": tracks, "invalidation": inv,
         "uncertainty": "Instrument identity is NOT detected from the audio. Default sounds are placeholders until you confirm them. "
                        "Vocal parts are set to a 'guide tone' (a sustained synth pad), which is a pitch reference and not a choir."},
    )


def _current_arrangement(ctx: Ctx) -> tuple[m.Arrangement, m.ScoreVersion]:
    arr = ctx.head(m.Arrangement, "arrangement")
    sv = ctx.head(m.ScoreVersion, "score")
    if not arr or not sv:
        raise Blocked("An arrangement is needed first. Run Arrangement Reconstruction.")
    if arr.data.get("score_version_id") != sv.id or arr.verification == "stale":
        raise Blocked("The score changed after this arrangement was built. Rebuild the arrangement so the render matches the current score.")
    if ctx.project.mode == "faithful":
        v = faithful_violations(arr.data, sv.data)
        if v:
            raise Blocked("Faithful Reconstruction refuses to render: " + " ".join(v))
    return arr, sv


@agent("choir", 7, "Professional Choir Production", "Prepares what real singers (or a future choir engine) need: guide tracks, underlay and pronunciation.")
def run_choir(ctx: Ctx) -> None:
    arr, sv = _current_arrangement(ctx)
    vocal_tracks = [t for t in arr.data["tracks"] if t["role"] == "vocal"]
    lyr = ctx.head(m.LyricsVersion, "lyrics")
    ctx.progress(0.15, "Checking for choir synthesis engines")
    engines = [
        {"name": "Neural/sampled choir with Latin text", "available": False,
         "reason": "No choir engine is connected. Word-building choir libraries are commercial desktop plug-ins without a server API; open singing-synthesis systems do not provide a Latin SATB church choir. None is assumed or simulated."},
        {"name": "Human singers (imported stems)", "available": True,
         "reason": "Record each part against the rehearsal tracks below and import the WAV files as stems on the Arrangement page."},
    ]
    pack = []
    if vocal_tracks and synth.engine_info()["available"]:
        for i, t in enumerate(vocal_tracks):
            ctx.progress(0.2 + 0.6 * i / len(vocal_tracks), f"Rendering rehearsal guide for '{t['name']}'")
            mid, wav = ctx.tmp / f"rehearsal_{t['id']}.mid", ctx.tmp / f"rehearsal_{t['id']}.wav"
            if score_to_midi(sv.data, mid, part_ids=[t["part_id"]], program_override={t["part_id"]: 0}) == 0:
                continue
            synth.render_midi(mid, wav, settings.work_sr)
            a = ctx.store(wav, "rehearsal", f"Rehearsal guide (piano): {t['name']}", method="FluidSynth piano guide of one vocal part",
                          engine="fluidsynth", engine_version=synth.engine_info()["version"], settings={"soundfont": synth.engine_info()["soundfont_name"]},
                          meta={"track_id": t["id"], "score_version_id": sv.id})
            pack.append({"track": t["name"], "asset_id": a.id})
    guide_asset = None
    if lyr and lyr.data.get("lines"):
        txt = ctx.tmp / "pronunciation_guide.txt"
        rows = ["RESONANCE pronunciation guide", f"Language: {lyr.data.get('language')}", "Status: draft, review before use", ""]
        rows += [l["text"] for l in lyr.data["lines"]] + ["", "Word-by-word:"]
        rows += [f"  {p['word']:<14} {'-'.join(p['syllables']):<18} {p.get('guide', '')}" for p in lyr.data.get("pronunciation", [])]
        txt.write_text("\n".join(rows), encoding="utf-8")
        guide_asset = ctx.store(txt, "rehearsal", "Pronunciation guide", method="rule-based ecclesiastical Latin respelling", engine="resonance.language", meta={"lyrics_version_id": lyr.id})
    ctx.output(
        "No automatic choir was produced: no real choir synthesis engine is available. "
        + (f"Prepared {len(pack)} rehearsal guide track(s) for human singers" if pack else "No vocal parts were found to prepare rehearsal tracks for")
        + (" and a pronunciation guide." if guide_asset else "."),
        {"choir_generated": False, "engines": engines, "rehearsal_tracks": pack, "pronunciation_guide_asset": guide_asset.id if guide_asset else None,
         "vocal_tracks": [t["name"] for t in vocal_tracks],
         "uncertainty": "Vocal parts in a render are currently a wordless guide tone. A realistic Latin choir requires human singers or a licensed engine that has not been connected."},
    )


def _load_stem(path, sr: int) -> np.ndarray:
    y, s = aio.load(path)
    mono = y.mean(axis=1)
    if s != sr:
        import librosa

        mono = librosa.resample(mono, orig_sr=s, target_sr=sr)
    return mono.astype(np.float64)


@agent("instrumental", 8, "Instrumental Production", "Renders every enabled track from the score to audio with FluidSynth and a SoundFont.")
def run_render(ctx: Ctx) -> None:
    arr, sv = _current_arrangement(ctx)
    info = synth.engine_info()
    tracks = [t for t in arr.data["tracks"] if t.get("enabled")]
    if not tracks:
        raise Blocked("No tracks are enabled in the arrangement.")
    if any(t["rendering"] != "human-stem" for t in tracks) and not info["available"]:
        raise Blocked(f"The MIDI rendering engine is unavailable: {info['reason']}. Install FluidSynth and a General MIDI SoundFont (see docs/SETUP.md).")
    sr = settings.work_sr
    stems, guide_vocals = [], []
    for i, t in enumerate(tracks):
        ctx.progress(0.05 + 0.85 * i / len(tracks), f"Rendering '{t['name']}' as {t['instrument'] if t['rendering'] != 'human-stem' else 'imported human recording'}")
        if t["rendering"] == "human-stem":
            src = ctx.db.get(m.Asset, t.get("human_stem_asset_id") or "")
            if not src:
                raise Blocked(f"Track '{t['name']}' is set to use an imported recording but none is attached.")
            wav = ctx.tmp / f"{t['id']}.wav"
            aio.save_wav(wav, _load_stem(ctx.path(src), sr), sr)
            a = ctx.store(wav, "render_stem", f"{t['name']} (human recording)", method="imported human stem (resampled only)", engine="import",
                          parent=src, meta={"track_id": t["id"], "score_version_id": sv.id, "arrangement_id": arr.id, "rendering": "human-stem"})
        else:
            mid, wav = ctx.tmp / f"{t['id']}.mid", ctx.tmp / f"{t['id']}.wav"
            n = score_to_midi(sv.data, mid, part_ids=[t["part_id"]], program_override={t["part_id"]: int(t["program"])})
            if n == 0:
                ctx.log(f"Skipped '{t['name']}': it has no notes")
                continue
            synth.render_midi(mid, wav, sr)
            a = ctx.store(wav, "render_stem", f"{t['name']} ({t['instrument']})", method="MIDI rendered with FluidSynth", engine="fluidsynth",
                          engine_version=info["version"], settings={"soundfont": info["soundfont_name"], "program": int(t["program"]), "sample_rate": sr},
                          meta={"track_id": t["id"], "score_version_id": sv.id, "arrangement_id": arr.id, "rendering": t["rendering"], "notes": n})
            if t["rendering"] == "guide-tone":
                guide_vocals.append(t["name"])
        stems.append(a.id)
    if not stems:
        raise Blocked("Nothing was rendered: all enabled tracks are empty.")
    n = ctx.db.execute(select(func.max(m.Reconstruction.version)).where(m.Reconstruction.project_id == ctx.project.id)).scalar() or 0
    rec = m.Reconstruction(project_id=ctx.project.id, version=n + 1, mode=ctx.project.mode, score_version_id=sv.id, arrangement_id=arr.id,
                           stem_asset_ids=stems, status="rendered",
                           notes={"guide_tone_vocals": guide_vocals, "engine": f"fluidsynth {info['version']}", "soundfont": info["soundfont_name"]})
    ctx.db.add(rec)
    ctx.db.flush()
    inv = gates.invalidate(ctx.db, ctx.project, "render", "A new render was produced.")
    ctx.db.commit()
    ctx.output(
        f"Rendered {len(stems)} stem(s) from score v{sv.version} with FluidSynth ({info['soundfont_name']}). "
        + (f"Vocal part(s) {', '.join(guide_vocals)} are a wordless synthetic guide tone, NOT a choir. " if guide_vocals else "")
        + "This is a General MIDI SoundFont render: accurate to the score, but not yet professional instrument realism.",
        {"reconstruction_id": rec.id, "reconstruction_version": rec.version, "stems": stems, "guide_tone_vocals": guide_vocals, "invalidation": inv,
         "engine": info, "uncertainty": "Note timing and pitch follow the score exactly. Articulation and dynamics are limited to note velocity; a GM SoundFont has no legato or expressive sampling."},
    )


def _target_reconstruction(ctx: Ctx) -> m.Reconstruction:
    rid = ctx.params.get("reconstruction_id")
    rec = ctx.db.get(m.Reconstruction, rid) if rid else ctx.db.execute(
        select(m.Reconstruction).where(m.Reconstruction.project_id == ctx.project.id, m.Reconstruction.status != "stale")
        .order_by(m.Reconstruction.version.desc())).scalars().first()
    if not rec:
        raise Blocked("There is no up-to-date render to work on. Render the arrangement first.")
    if rec.status == "stale":
        raise Blocked("That render is out of date because the score or arrangement changed. Render again.")
    return rec


def default_mix(arr: dict | None) -> dict:
    return {"reverb_preset": "parish_church", "reverb_wet_db": None, "width": 1.0, "bus_compress": False,
            "tracks": {t["id"]: {"gain_db": t.get("gain_db", 0.0), "pan": t.get("pan", 0.0), "mute": False,
                                 "high_pass_hz": 30 if t.get("voice") == "bass" else 80, "presence_db": 1.5 if t.get("role") == "vocal" else 0.0,
                                 "compress": t.get("role") == "vocal"} for t in (arr or {}).get("tracks", [])}}


@agent("mixing", 9, "Mixing Engineer", "Balances, pans, equalises and places the rendered stems in a church acoustic.")
def run_mix(ctx: Ctx) -> None:
    rec = _target_reconstruction(ctx)
    if rec.mode == "restore":
        raise Blocked("Restore mode has no stems to mix; go straight to mastering.")
    arr = ctx.db.get(m.Arrangement, rec.arrangement_id)
    ms = ctx.head(m.MixSettings, "mix")
    if not ms:
        ms = new_version(ctx.db, ctx.project, m.MixSettings, "mix", default_mix(arr.data), origin="default", note="Default mix")
        ctx.db.commit()
    cfg = {**default_mix(arr.data), **ms.data}
    cfg["tracks"] = {**default_mix(arr.data)["tracks"], **(ms.data.get("tracks") or {})}
    sr = settings.work_sr
    assets = [ctx.db.get(m.Asset, i) for i in rec.stem_asset_ids]
    bus, report = None, []
    for i, a in enumerate(assets):
        tid = a.meta.get("track_id")
        tc = cfg["tracks"].get(tid, {"gain_db": 0, "pan": 0, "mute": False, "high_pass_hz": 60, "presence_db": 0, "compress": False})
        ctx.progress(0.05 + 0.5 * i / len(assets), f"Processing '{a.label}'")
        if tc.get("mute"):
            report.append({"track_id": tid, "label": a.label, "muted": True})
            continue
        mono = _load_stem(ctx.path(a), sr)
        rms = float(np.sqrt(np.mean(mono**2))) + 1e-12
        stage = 10 ** (-24 / 20) / rms  # gain staging: every stem to -24 dBFS RMS before the fader
        y = dsp.pan(mono * stage * 10 ** (float(tc.get("gain_db", 0)) / 20), float(tc.get("pan", 0)))
        bands = [{"type": "highpass", "freq": float(tc.get("high_pass_hz") or 40), "q": 0.707}]
        if abs(float(tc.get("presence_db") or 0)) > 0.01:
            bands.append({"type": "peak", "freq": 3200, "gain_db": float(tc["presence_db"]), "q": 0.8})
        y = dsp.eq(y, sr, bands)
        gr = 0.0
        if tc.get("compress"):
            y, gr = dsp.compress(y, sr, threshold_db=-22, ratio=2.0, attack_ms=25, release_ms=300)
        report.append({"track_id": tid, "label": a.label, "staging_gain_db": round(an.db(stage), 1), "fader_db": tc.get("gain_db", 0),
                       "pan": tc.get("pan", 0), "eq": bands, "max_gain_reduction_db": round(gr, 1)})
        if bus is None:
            bus = y
        else:
            n = max(len(bus), len(y))
            b2 = np.zeros((n, 2)); b2[: len(bus)] += bus; b2[: len(y)] += y
            bus = b2
    if bus is None:
        raise Blocked("All tracks are muted.")
    ctx.progress(0.6, f"Adding acoustic space: {dsp.REVERB_PRESETS.get(cfg['reverb_preset'], {}).get('label', cfg['reverb_preset'])}")
    bus = dsp.stereo_width(bus, float(cfg.get("width", 1.0)))
    if cfg.get("bus_compress"):
        bus, _ = dsp.compress(bus, sr, threshold_db=-18, ratio=1.5, attack_ms=40, release_ms=400)
    mix = dsp.reverb(bus, sr, cfg["reverb_preset"], cfg.get("reverb_wet_db"))
    tail = min(len(mix) - len(bus), int(4.0 * sr))  # let the room ring for at most 4 s after the last note, then fade
    mix = mix[: len(bus) + tail]
    if tail > 0:
        mix[-tail:] *= np.linspace(1.0, 0.0, tail)[:, None] ** 2
    peak = float(np.max(np.abs(mix))) + 1e-12
    if peak > 10 ** (-3 / 20):  # leave 3 dB headroom for mastering; pure gain, no clipping
        mix *= 10 ** (-3 / 20) / peak
    ctx.progress(0.9, "Measuring the mix")
    out = ctx.tmp / f"mix_v{rec.version}.wav"
    aio.save_wav(out, mix, sr)
    lufs = an.integrated_lufs(mix, sr)
    a = ctx.store(out, "mix", f"Mix of reconstruction v{rec.version}", method="RESONANCE mix bus (gain staging, EQ, compression, convolution reverb)",
                  engine="resonance.dsp", settings=cfg, meta={"reconstruction_id": rec.id, "mix_settings_id": ms.id, "mode": rec.mode,
                                                                 "lufs": lufs, "true_peak_db": an.true_peak_db(mix, sr)})
    rec.mix_asset_id, rec.mix_settings_id, rec.status = a.id, ms.id, "mixed"
    rec.master_asset_id = None
    inv = gates.invalidate(ctx.db, ctx.project, "render", "The mix was re-made.") if ctx.params.get("remix") else None
    ctx.db.commit()
    ctx.output(f"Mixed {len([r for r in report if not r.get('muted')])} stem(s) into stereo with the '{cfg['reverb_preset']}' acoustic. "
               f"Mix loudness {lufs:.1f} LUFS." if lufs is not None else "Mix created (too quiet to measure loudness).",
               {"reconstruction_id": rec.id, "mix_asset_id": a.id, "tracks": report, "settings": cfg, "invalidation": inv,
                "measurements": {"integrated_lufs": lufs, "true_peak_db": a.meta["true_peak_db"], "stereo": an.stereo_summary(mix)},
                "uncertainty": "The church acoustic is an algorithmic room model, not a measured impulse response of a real building."})


@agent("restore", 0, "Restoration (Mode A)", "Cleans the ORIGINAL performance: noise reduction and tonal correction. No notes or performers are replaced.")
def run_restore(ctx: Ctx) -> None:
    work = ctx.working()
    strength = float(ctx.params.get("strength", 0.5))
    fo = ctx.db.execute(select(m.AgentOutput).where(m.AgentOutput.project_id == ctx.project.id, m.AgentOutput.agent == "forensics")
                        .order_by(m.AgentOutput.created_at.desc())).scalars().first()
    ctx.progress(0.1, "Loading the original performance")
    y, sr = aio.load(ctx.path(work))
    y = dsp.to_stereo(y)
    y -= y.mean(axis=0, keepdims=True)
    ctx.progress(0.3, f"Reducing steady background noise (strength {strength:.0%})")
    y, dn = dsp.spectral_denoise(y, sr, strength)
    bands = dsp.tonal_tilt_bands(((fo.data if fo else {}).get("metrics", {}).get("spectrum", {}) or {}).get("band_energy_db", {})) if ctx.params.get("tonal_eq", True) else []
    ctx.progress(0.7, "Applying gentle tonal correction")
    y = dsp.eq(y, sr, bands)
    peak = float(np.max(np.abs(y))) + 1e-12
    if peak > 10 ** (-3 / 20):
        y *= 10 ** (-3 / 20) / peak
    out = ctx.tmp / "restored.wav"
    aio.save_wav(out, y, sr)
    a = ctx.store(out, "mix", "Restored original performance", parent=work, method="spectral gating noise reduction + corrective EQ", engine="resonance.dsp",
                  settings={"strength": strength, "eq": bands, **dn}, meta={"mode": "restore", "lufs": an.integrated_lufs(y, sr)})
    n = ctx.db.execute(select(func.max(m.Reconstruction.version)).where(m.Reconstruction.project_id == ctx.project.id)).scalar() or 0
    rec = m.Reconstruction(project_id=ctx.project.id, version=n + 1, mode="restore", stem_asset_ids=[], mix_asset_id=a.id, status="mixed",
                           notes={"restore": {"strength": strength, "eq": bands}})
    ctx.db.add(rec)
    ctx.db.flush()
    a.meta = {**a.meta, "reconstruction_id": rec.id}
    gates.invalidate(ctx.db, ctx.project, "render", "A new restoration was produced.")
    ctx.db.commit()
    ctx.output(f"Restored the original recording: up to {dn['max_attenuation_db']} dB of steady-noise reduction and {len(bands)} corrective EQ band(s). The performance itself is untouched.",
               {"reconstruction_id": rec.id, "mix_asset_id": a.id, "denoise": dn, "eq": bands,
                "uncertainty": "Spectral gating lowers constant hiss and room tone. It does not remove talking, coughs, or reverberation, and cannot repair clipping."})


@agent("mastering", 10, "Mastering Engineer", "Sets final loudness and peak level and writes WAV and MP3 masters.")
def run_master(ctx: Ctx) -> None:
    rec = _target_reconstruction(ctx)
    mix = ctx.db.get(m.Asset, rec.mix_asset_id or "")
    if not mix:
        raise Blocked("This render has not been mixed yet. Run the Mixing Engineer first.")
    mset = ctx.head(m.MasterSettings, "master")
    if not mset:
        mset = new_version(ctx.db, ctx.project, m.MasterSettings, "master", {"target": "sacred_choral", "tilt_db": 0.0}, origin="default", note="Default mastering target")
        ctx.db.commit()
    key = mset.data.get("target", "sacred_choral")
    tgt = MASTER_TARGETS.get(key) or MASTER_TARGETS["sacred_choral"]
    ctx.progress(0.1, f"Mastering to '{tgt['label']}' ({tgt['lufs']} LUFS, {tgt['ceiling_db']} dBTP ceiling)")
    y, sr = aio.load(ctx.path(mix))
    y = dsp.to_stereo(y)
    before = {"integrated_lufs": an.integrated_lufs(y, sr), "true_peak_db": an.true_peak_db(y, sr)}
    tilt = float(mset.data.get("tilt_db") or 0.0)
    if abs(tilt) > 0.01:
        y = dsp.eq(y, sr, [{"type": "highshelf", "freq": 8000, "gain_db": tilt}, {"type": "lowshelf", "freq": 120, "gain_db": -tilt / 2}])
    ctx.progress(0.35, "Normalising loudness")
    y, measured = dsp.normalize_loudness(y, sr, tgt["lufs"])
    if measured is None:
        raise Blocked("The mix is silent or too short to measure loudness; nothing to master.")
    ctx.progress(0.55, "Controlling peaks with a look-ahead true-peak limiter")
    pre_peak = an.true_peak_db(y, sr)
    y = dsp.limit(y, sr, tgt["ceiling_db"])
    after = {"integrated_lufs": an.integrated_lufs(y, sr), "true_peak_db": an.true_peak_db(y, sr), "stereo": an.stereo_summary(y),
             "limiter_reduction_db": round(max(0.0, pre_peak - tgt["ceiling_db"]), 1)}
    mono = y.mean(axis=1)
    after["mono_fold_down_loss_db"] = round(an.db(float(np.sqrt(np.mean(y**2)))) - an.db(float(np.sqrt(np.mean(mono**2)))), 1)
    ctx.progress(0.8, "Writing WAV (24-bit) and MP3 (320 kbps)")
    wav, mp3 = ctx.tmp / f"master_v{rec.version}.wav", ctx.tmp / f"master_v{rec.version}.mp3"
    aio.save_wav(wav, y, sr, "PCM_24")
    aio.encode_mp3(wav, mp3)
    meta = {"reconstruction_id": rec.id, "mode": rec.mode, "target": key, "measurements": after, "master_settings_id": mset.id}
    prov = dict(method="RESONANCE mastering chain (BS.1770 loudness normalisation + true-peak limiter)", engine="resonance.dsp+pyloudnorm",
                settings={"target": key, **tgt, "tilt_db": tilt}, parent=mix, verification="draft")
    aw = ctx.store(wav, "master", f"Master v{rec.version} WAV ({tgt['label']})", meta={**meta, "format": "wav"}, **prov)
    am = ctx.store(mp3, "master", f"Master v{rec.version} MP3 ({tgt['label']})", meta={**meta, "format": "mp3"}, **prov)
    rec.master_asset_id, rec.master_settings_id, rec.status = aw.id, mset.id, "mastered"
    gates.invalidate(ctx.db, ctx.project, "master", "A new master was produced.")
    ctx.db.commit()
    warn = []
    if after["limiter_reduction_db"] > 3:
        warn.append(f"The limiter had to remove {after['limiter_reduction_db']} dB of peaks to reach this loudness; dynamics are being squashed. Consider a quieter target.")
    ctx.output(f"Mastered to {after['integrated_lufs']:.1f} LUFS with true peak {after['true_peak_db']:.1f} dBTP ({tgt['label']}). Marked DRAFT until Gate 7 is authorised.",
               {"reconstruction_id": rec.id, "wav_asset_id": aw.id, "mp3_asset_id": am.id, "target": {**tgt, "key": key}, "before": before, "after": after, "warnings": warn})
