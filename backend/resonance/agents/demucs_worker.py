"""Runs Demucs in a separate process so that an out-of-memory kill or a crash only ends this child, not the
web server. Usage: python -m resonance.agents.demucs_worker <in.wav> <out_dir> <progress_file> <model> [repo] [shifts]
Writes one float WAV per source into out_dir and 'k total' lines into progress_file."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    inp, out_dir, prog = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    model_name, repo = sys.argv[4], (sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None)
    shifts = int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else 0  # test-time augmentation: cleaner, slower
    import numpy as np
    import soundfile as sf
    import torch
    import demucs.apply as dapply
    from demucs.pretrained import get_model

    model = get_model(model_name, repo=Path(repo) if repo else None)
    model.eval()
    y, sr = sf.read(str(inp), dtype="float32", always_2d=True)
    if sr != model.samplerate:
        import librosa
        y = librosa.resample(y.T, orig_sr=sr, target_sr=model.samplerate).T
        sr = model.samplerate
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    wav = torch.from_numpy(np.ascontiguousarray(y[:, :2].T)).float()
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8

    def reporting(iterable, *args, **kwargs):
        items = list(iterable)
        total = max(1, len(items))
        for k, item in enumerate(items, 1):
            yield item
            prog.write_text(f"{k} {total}")

    dapply.tqdm.tqdm = reporting
    with torch.no_grad():
        out = dapply.apply_model(model, ((wav - mean) / std)[None], device="cpu", shifts=shifts, split=True, overlap=0.25, progress=True)[0]
    out = (out * std + mean).numpy()
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(model.sources):
        sf.write(str(out_dir / f"{name}.wav"), out[i].T, sr, subtype="FLOAT")
    (out_dir / "samplerate").write_text(str(sr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
