#!/usr/bin/env bash
# Local setup on Debian/Ubuntu. Run from the repository root.
set -euo pipefail
sudo apt-get update && sudo apt-get install -y ffmpeg fluidsynth fluid-soundfont-gm libsndfile1
python3 -m venv .venv && . .venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
pip install --no-deps basic-pitch==0.4.0
(cd frontend && npm ci && npm run build)
echo "Done. Start with: scripts/run.sh"
