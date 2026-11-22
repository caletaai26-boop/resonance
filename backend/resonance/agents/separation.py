from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..audio import io as aio
from ..audio.analysis import db
from ..config import settings
from .base import Blocked, Ctx, agent

SETUP_HELP = (
    "Source separation needs the Demucs model weights (~80 MB), which could not be loaded. "
    "Either allow network access to dl.fbaipublicfiles.com so they download automatically, or download the "
    "'htdemucs' weights on another machine and set RESONANCE_DEMUCS_REPO to the folder containing them. "
    "Until then the pipeline continues on the full mix: melody and structure estimates will be less reliable."
)


def engine_status() -> dict:
    try:
        import demucs
        import torch

        return {"installed": True, "version": demucs.__version__, "torch": torch.__version__,
                "model": settings.demucs_model, "local_repo": os.environ.get("RESONANCE_DEMUCS_REPO")}
    except Exception as e:
        return {"installed": False, "reason": f"{e.__class__.__name__}: {e}"}


def load_model():
    from demucs.pretrained import get_model

    repo = os.environ.get("RESONANCE_DEMUCS_REPO")
    model = get_model(settings.demucs_model, repo=Path(repo) if repo else None)
    model.eval()
    return model


@agent("separation", 2, "Source Separation", "Splits the recording into voices and accompaniment with a neural separation model.")
def run(ctx: Ctx) -> None:
    work = ctx.working()
    st = engine_status()
    if not st["installed"]:
        raise Blocked("Demucs is not installed on the server. " + SETUP_HELP)
    ctx.progress(0.05, f"Loading separation model '{settings.demucs_model}'")
    try:
        model = load_model()
    except Exception as e:
        raise Blocked(f"{SETUP_HELP} (Technical detail: {str(e)[:160]})")

    import torch
    from demucs.apply import apply_model

    y, sr = aio.load(ctx.path(work))
    if sr != model.samplerate:
        import librosa

        y = librosa.resample(y.T, orig_sr=sr, target_sr=model.samplerate).T
        sr = model.samplerate
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    wav = torch.from_numpy(np.ascontiguousarray(y[:, :2].T)).float()
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    ctx.progress(0.15, "Separating (this is the slow step: roughly as long as the recording itself on CPU)")
    with torch.no_grad():
        out = apply_model(model, ((wav - mean) / std)[None], device="cpu", shifts=0, split=True, overlap=0.25, progress=False)[0]
    out = (out * std + mean).numpy()  # [sources, channels, n]
    names = list(model.sources)
    ctx.progress(0.85, "Writing stems")

    stems = {n: out[i].T for i, n in enumerate(names)}
    vocals = stems.get("vocals")
    accomp = sum(v for k, v in stems.items() if k != "vocals")
    total = float(np.sum(y**2)) + 1e-12
    result, ids = [], {}
    prov = dict(method="Demucs hybrid transformer separation", engine=f"demucs:{settings.demucs_model}", engine_version=st["version"],
                settings={"shifts": 0, "overlap": 0.25, "device": "cpu"})
    for key, label, data, warn in [
        ("vocals", "Voices (choir + any solo voices, as one stem)", vocals,
         "All singers come out as ONE stem. Individual choir sections (soprano/alto/tenor/bass) are not separated. Speech from the congregation usually lands in this stem too."),
        ("accompaniment", "Accompaniment (everything that is not voice)", accomp,
         "Contains all instruments together plus most room noise. Individual instruments are not separated. Organ and sustained instruments can leak into the voice stem."),
        ("bass", "Low register (bass line estimate)", stems.get("bass"),
         "The model's 'bass' output was trained on pop/rock bass guitar. On organ or choir music treat it only as a rough low-register guide."),
    ]:
        if data is None:
            continue
        p = ctx.tmp / f"{key}.wav"
        aio.save_wav(p, data, sr, subtype="PCM_16")
        share = float(np.sum(data**2)) / total
        a = ctx.store(p, "stem", label, parent=work, meta={"stem": key, "energy_share": round(share, 3)}, **prov)
        ids[key] = a.id
        result.append({"stem": key, "label": label, "asset_id": a.id, "energy_share": round(share, 3),
                       "rms_db": round(db(float(np.sqrt(np.mean(data**2)))), 1), "warning": warn})
    if result and result[0]["stem"] == "vocals" and result[0]["energy_share"] < 0.03:
        result[0]["warning"] = "This stem is almost silent: the model found no singing. It is ignored by the later steps. " + result[0]["warning"]
    ctx.output(
        f"Separated into {len(result)} stems with Demucs ({settings.demucs_model}). Voices carry {result[0]['energy_share'] * 100:.0f}% of the energy.",
        {"engine": prov["engine"], "engine_version": st["version"], "stems": result, "artifacts": ids,
         "limitations": [
             "This is a 4-stem model trained mostly on studio pop/rock. Live church recordings with reverberation are outside its comfort zone; expect leakage between stems.",
             "Background noise is not a separable stem: talking ends up mostly with the voices, room rumble with the accompaniment.",
             "No confidence score is produced by the model. Listen to each stem against the original before trusting it.",
         ]},
    )
