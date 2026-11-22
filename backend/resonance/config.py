"""Runtime configuration. Everything is overridable via environment variables (see .env.example)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(_env("RESONANCE_DATA_DIR", str(ROOT / "data"))).resolve()
        self.database_url = _env("RESONANCE_DATABASE_URL", f"sqlite:///{self.data_dir / 'resonance.db'}")
        self.soundfont = _env("RESONANCE_SOUNDFONT", "/usr/share/sounds/sf2/FluidR3_GM.sf2")
        self.max_upload_mb = int(_env("RESONANCE_MAX_UPLOAD_MB", "200"))
        self.work_sr = int(_env("RESONANCE_WORK_SR", "44100"))
        self.job_max_attempts = int(_env("RESONANCE_JOB_MAX_ATTEMPTS", "2"))
        self.run_worker = _env("RESONANCE_RUN_WORKER", "1") == "1"
        self.allow_registration = _env("RESONANCE_ALLOW_REGISTRATION", "1") == "1"
        self.cookie_secure = _env("RESONANCE_COOKIE_SECURE", "0") == "1"
        self.frontend_dist = Path(_env("RESONANCE_FRONTEND_DIST", str(ROOT / "frontend" / "dist")))
        self.demucs_model = _env("RESONANCE_DEMUCS_MODEL", "htdemucs")
        self.data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
