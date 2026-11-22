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
    import os
    import subprocess
    import sys
    import time

    import soundfile as sf

    duration = float((work.meta or {}).get("duration") or 180)
    limit = max(900.0, duration * 12)  # generous: CPU separation inside Docker can run several times real time
    out_dir, prog = ctx.tmp / "demucs", ctx.tmp / "demucs.progress"
    cmd = [sys.executable, "-m", "resonance.agents.demucs_worker", str(ctx.path(work)), str(out_dir), str(prog),
           settings.demucs_model, os.environ.get("RESONANCE_DEMUCS_REPO", "")]
    ctx.progress(0.05, f"Loading separation model '{settings.demucs_model}' (a separate process, so a crash cannot stop the studio)")
    t0 = time.monotonic()
    err_path = ctx.tmp / "demucs.stderr"
    with open(err_path, "wb") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err, cwd=str(Path(__file__).resolve().parents[2]))
        last = ""
        while proc.poll() is None:
            time.sleep(2.0)
            elapsed = time.monotonic() - t0
            if elapsed > limit:
                proc.kill()
                proc.wait()
                raise Blocked(f"Separation was stopped after {int(elapsed // 60)} minutes without finishing. The computer may be too slow or short of memory. "
                              "Give Docker more CPU and memory (Docker Desktop → Settings → Resources) and run it again. The rest of the analysis continues without stems.")
            if prog.exists():
                try:
                    k, total = (int(x) for x in prog.read_text().split())
                except ValueError:
                    continue
                txt = f"{k}/{total}"
                if txt != last:
                    last = txt
                    done = k / total
                    eta = (elapsed / done) * (1 - done)
                    ctx.progress(0.15 + 0.7 * done, f"Separating: chunk {k} of {total}, about {int(eta // 60)} min {int(eta % 60)} s left")
            elif elapsed > 20 and not last:
                ctx.progress(0.1, f"Separating: model loaded, processing ({int(elapsed)} s so far)")
    stderr_tail = err_path.read_text(errors="replace")[-600:]
    if proc.returncode in (-9, 137) or "Killed" in stderr_tail or "MemoryError" in stderr_tail:
        raise Blocked("Separation ran out of memory and was stopped. Docker currently has too little memory for this recording. "
                      "In Docker Desktop → Settings → Resources set Memory to at least 8 GB, click Apply & Restart, then run Source Separation again. "
                      "The rest of the analysis continues without stems.")
    if proc.returncode != 0:
        if "urlopen" in stderr_tail or "Tunnel" in stderr_tail or "HTTP Error" in stderr_tail or "getaddrinfo" in stderr_tail:
            raise Blocked(f"{SETUP_HELP} (Technical detail: {stderr_tail.strip().splitlines()[-1][:160]})")
        raise RuntimeError(f"Demucs process failed (exit {proc.returncode}): {stderr_tail.strip().splitlines()[-1] if stderr_tail.strip() else 'no message'}")
    ctx.log(f"Separation took {time.monotonic() - t0:.0f} s")
    sr = int((out_dir / "samplerate").read_text())
    y, _ = aio.load(ctx.path(work))
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    sources = sorted(p.stem for p in out_dir.glob("*.wav"))
    out = np.stack([sf.read(str(out_dir / f"{n}.wav"), dtype="float32", always_2d=True)[0].T for n in sources])
    n = min(out.shape[-1], y.shape[0])
    out, y = out[..., :n], y[:n, :2]
    ctx.progress(0.88, "Writing stems")

    class _M:  # minimal stand-in for the model object used below
        pass
    model = _M()
    model.sources = sources
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
