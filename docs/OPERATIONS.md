# Operations

## Dependencies

| Component | Version tested | Licence | Purpose |
|---|---|---|---|
| Python | 3.13 | | backend |
| FastAPI / Uvicorn / SQLAlchemy / Pydantic | see `backend/requirements.txt` | MIT / BSD | API, persistence, validation |
| FFmpeg (with libmp3lame) | system | LGPL/GPL | decode, MP3 encode |
| librosa, numpy, scipy, soundfile, pyloudnorm | pinned | ISC / BSD / MIT | analysis and DSP |
| Demucs 4 + PyTorch (CPU) | pinned | MIT / BSD | source separation; weights downloaded on first use |
| Basic Pitch 0.4 + onnxruntime | pinned | Apache-2.0 / MIT | polyphonic transcription; install with `--no-deps` |
| pretty_midi, mido, music21 | pinned | MIT / BSD | MIDI and MusicXML |
| FluidSynth 2 + FluidR3_GM SoundFont | system | LGPL / MIT | instrument rendering |
| anthropic SDK | pinned | MIT | optional narration |
| Node 22, React 18, Vite 5, Tailwind 3, OpenSheetMusicDisplay | `frontend/package.json` | MIT / BSD | studio UI |

## Configuration

All settings are environment variables; see `.env.example`. The provider key for the optional language model is entered in **Engines & settings**, kept server-side and never returned to the browser.

## Security notes

- Sessions are random 256-bit tokens stored hashed (SHA-256); the cookie is HttpOnly, SameSite=Lax, and Secure when `RESONANCE_COOKIE_SECURE=1`. Passwords use scrypt.
- Every project route checks ownership and returns 404 for projects that belong to someone else.
- Uploads: extension allow-list, streamed size limit, sanitised filenames, storage keys derived from generated IDs (no user-controlled paths), and audio must pass `ffprobe` before it is accepted.
- No audio leaves the server. The language model is opt-in per project and receives findings as text only.
- Not yet in place: rate limiting, CSRF tokens beyond SameSite cookies, password reset, encrypted secret storage, audit of dependency CVEs. Put the server behind HTTPS and close registration before exposing it to the internet.

## Backup and recovery

Everything lives in `RESONANCE_DATA_DIR`: `resonance.db` and `objects/`.

```bash
scripts/backup.sh /path/to/backups     # consistent SQLite snapshot + tar of all files; safe while running
```

Restore: stop the server, copy the `.db` snapshot to `$RESONANCE_DATA_DIR/resonance.db`, extract the tarball into `$RESONANCE_DATA_DIR`, start the server. Jobs that were mid-run when the backup was taken are re-queued automatically. Each project can also be exported from the UI as a self-contained archive (original recording, every document version, current audio, reports, provenance manifest). Because originals are immutable and every derived file records how it was made, anything except the original and your manual edits can be regenerated.

Per-project retention: set "delete automatically after N days" in project settings; expired projects are removed at server start.

## Evaluating a real recording

The synthetic fixture tells you the algorithms work; it does not tell you how they perform on your recording. For the Adeste Fideles project:

1. Upload and run the analysis. Note what separation reports for the voice stem's share of energy and listen to each stem.
2. Obtain the score the choir used (or a standard SATB Adeste Fideles in the same key) as MusicXML/MIDI and import it. The Fidelity Inspector then reports precision and recall of the automatic transcription against it. That is the first real accuracy measurement; record it in `docs/STATUS.md`.
3. Correct section boundaries and lyrics by ear using the loop tools; these are the ground truth for later runs.
4. Render, then work through the discrepancy list with A/B looping. Count how many flagged items were real: that false-alarm rate tells you how much to trust the inspector on the next piece.

Do not quote an accuracy figure for a real recording unless it was measured against a reference a person verified.

## Running the tests

```bash
cd backend && python -m pytest
```

The suite generates its own audio; nothing is downloaded except the Demucs weights if reachable (tests accept separation being blocked). `node e2e/journey.mjs <audio> [screenshot-dir]` drives a real browser through the full workflow against a running server.
