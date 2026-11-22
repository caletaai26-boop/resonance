from __future__ import annotations

import json

from ..audio import analysis as an
from ..audio import io as aio
from ..config import settings
from .. import gates
from .base import Blocked, Ctx, agent


@agent("forensics", 1, "Audio Forensics", "Inspects the uploaded file and measures its technical quality.")
def run(ctx: Ctx) -> None:
    src = ctx.source()
    path = ctx.path(src)
    ctx.progress(0.05, "Reading file header and stream information")
    try:
        probe = aio.ffprobe(path)
    except aio.AudioDecodeError as e:
        raise Blocked(f"The uploaded file could not be read as audio: {e}")

    ctx.progress(0.15, "Decoding to an untouched-copy working WAV (the original stays immutable)")
    wav = ctx.tmp / "working.wav"
    try:
        aio.decode_to_wav(path, wav, sr=settings.work_sr)
    except aio.AudioDecodeError as e:
        raise Blocked(f"The audio could not be decoded: {e}")
    y, sr = aio.load(wav)
    if y.shape[0] < sr // 2:
        raise Blocked("The recording is shorter than half a second; there is nothing to analyse.")
    mono = y.mean(axis=1)
    duration = y.shape[0] / sr

    ctx.progress(0.35, "Measuring loudness, peaks and clipping")
    lufs = an.integrated_lufs(y, sr)
    clip = an.clipping(y, sr)
    metrics = {
        "duration_s": round(duration, 2), "sample_rate": probe["sample_rate"], "channels": probe["channels"],
        "codec": probe["codec"], "container": probe["container"], "bit_rate": probe["bit_rate"],
        "working_sample_rate": sr,
        "integrated_lufs": round(lufs, 1) if lufs is not None else None,
        "sample_peak_db": round(an.sample_peak_db(y), 2), "true_peak_db": round(an.true_peak_db(y, sr), 2),
        "clipping": clip, "dc_offset": round(float(mono.mean()), 5),
    }
    ctx.progress(0.5, "Estimating noise floor and finding silent regions")
    metrics["noise"] = an.noise_floor(mono, sr)
    metrics["silent_regions"] = an.silent_regions(mono, sr)
    metrics["loudness_profile"] = an.loudness_profile(mono, sr)
    ctx.progress(0.65, "Analysing frequency balance and stereo image")
    metrics["spectrum"] = an.spectral_summary(mono, sr)
    metrics["stereo"] = an.stereo_summary(y)

    ctx.progress(0.8, "Drawing waveform and spectrogram")
    peaks = ctx.tmp / "waveform.json"
    peaks.write_text(json.dumps({"duration": duration, "peaks": an.waveform_peaks(mono, 2400)}))
    spec = ctx.tmp / "spectrogram.png"
    an.spectrogram_png(mono, sr, spec)

    # ---- observations (time-coded where possible) and recommendations, all derived from the numbers above
    obs, rec = [], []

    def note(sev: str, text: str, time: float | None = None):
        obs.append({"severity": sev, "text": text, "time": time})

    n = metrics["noise"]
    if clip["events"]:
        note("warning", f"{clip['events']} clipped passage(s) found: the microphone or phone input overloaded. Clipping cannot be fully undone.", clip["times"][0])
        for t in clip["times"][:12]:
            note("info", "Clipping", t)
    if n["snr_estimate_db"] < 25:
        note("warning", f"High background level: the quietest moments are only about {n['snr_estimate_db']:.0f} dB below the music (estimate). Expect room noise and talking to be audible.")
        rec.append({"op": "denoise", "reason": "Steady background noise is high relative to the music.", "suggested_strength": 0.6})
    elif n["snr_estimate_db"] < 40:
        note("info", f"Moderate background level (about {n['snr_estimate_db']:.0f} dB between music and the quietest moments, estimate).")
        rec.append({"op": "denoise", "reason": "Light noise reduction would help.", "suggested_strength": 0.35})
    sp = metrics["spectrum"]
    if sp["rolloff_99_hz"] < 12000:
        note("info", f"Very little content above {sp['rolloff_99_hz'] / 1000:.1f} kHz. Typical of phone microphones and lossy compression; the top 'air' of the choir is missing from the recording.")
    if sp["band_energy_db"]["lowmid_250_1k"] > -3:
        note("info", "Most of the energy sits between 250 Hz and 1 kHz, which sounds boxy or muffled.")
        rec.append({"op": "tonal_eq", "reason": "Reduce low-mid build-up, restore some high-frequency clarity."})
    lr = metrics["loudness_profile"]["range_db"]
    if lr > 14:
        note("info", f"Uneven volume: loud and quiet passages differ by about {lr:.0f} dB.")
        rec.append({"op": "level_balance", "reason": "Wide level swings between passages."})
    st = metrics["stereo"]
    if not st["is_stereo"] or st.get("effectively_mono"):
        note("info", "The recording is mono (or two identical channels). There is no real stereo image to preserve; a reconstruction will create one.")
    if lufs is not None and lufs < -30:
        note("info", f"The recording is very quiet overall ({lufs:.1f} LUFS).")
    if abs(metrics["dc_offset"]) > 0.01:
        rec.append({"op": "dc_remove", "reason": "DC offset present."})
    for s in metrics["silent_regions"][:10]:
        note("info", f"Silence from {s['start']:.1f}s to {s['end']:.1f}s", s["start"])
    if duration < 20:
        note("warning", "The recording is very short; structure and tempo estimates will be unreliable.")
    if not obs:
        note("info", "No significant technical problems detected.")

    prov = dict(method="ffmpeg decode + numpy/scipy/pyloudnorm measurements", engine="ffmpeg+pyloudnorm")
    w = ctx.store(wav, "working", "Working copy (decoded, unprocessed)", parent=src, settings={"sr": sr}, meta={"duration": duration, "channels": int(y.shape[1])}, **prov)
    pk = ctx.store(peaks, "waveform", "Waveform peaks", parent=w, **prov)
    sg = ctx.store(spec, "spectrogram", "Spectrogram", parent=w, method="librosa mel spectrogram", engine="librosa")

    inv = gates.invalidate(ctx.db, ctx.project, "analysis", "The recording was re-analysed.") if ctx.params.get("reanalysis") else None
    lufs_txt = f"{lufs:.1f} LUFS" if lufs is not None else "too quiet to measure"
    ctx.output(
        f"{duration:.0f}-second {probe['codec']} recording, {probe['channels']} channel(s) at {probe['sample_rate']} Hz. "
        f"Loudness {lufs_txt}, peak {metrics['true_peak_db']:.1f} dBTP, "
        f"{clip['events']} clipped passage(s), estimated noise floor {n['noise_floor_db']:.0f} dB.",
        {"metrics": metrics, "observations": obs, "recommendations": rec,
         "artifacts": {"working": w.id, "waveform": pk.id, "spectrogram": sg.id}, "invalidation": inv,
         "uncertainty": "Noise floor and SNR are estimates: in a live recording the music rarely stops, so the true noise level may be lower than measured."},
    )
