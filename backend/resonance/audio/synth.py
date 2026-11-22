"""MIDI -> audio rendering through FluidSynth and a SoundFont. This is the only path that produces
'reconstructed' instrument audio; if FluidSynth or the SoundFont is missing we raise, never fake."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..config import settings


class RenderUnavailable(Exception):
    pass


def engine_info() -> dict:
    exe = shutil.which("fluidsynth")
    sf = Path(settings.soundfont)
    version = ""
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
            version = (out.splitlines() or [""])[0].replace("FluidSynth runtime version", "").strip()
        except Exception:
            pass
    return {
        "available": bool(exe) and sf.exists(),
        "fluidsynth": exe or None,
        "version": version,
        "soundfont": str(sf) if sf.exists() else None,
        "soundfont_name": sf.name,
        "reason": None if (exe and sf.exists()) else ("FluidSynth not installed" if not exe else f"SoundFont not found at {sf}"),
    }


def render_midi(midi_path: Path, wav_path: Path, sr: int = 44100, gain: float = 0.6) -> None:
    info = engine_info()
    if not info["available"]:
        raise RenderUnavailable(info["reason"])
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [info["fluidsynth"], "-ni", "-q", "-g", str(gain), "-r", str(sr), "-R", "0", "-C", "0",
           "-F", str(wav_path), "-T", "wav", "-O", "float", info["soundfont"], str(midi_path)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if out.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size < 100:
        raise RenderUnavailable(f"FluidSynth failed: {(out.stderr or out.stdout).strip()[:300]}")
