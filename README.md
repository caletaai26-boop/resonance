# RESONANCE — AI Music Reconstruction Studio

RESONANCE takes an imperfect recording of a live performance and helps you rebuild **that same performance** with clean new sound: same melody, harmony, words, structure and timing. It is not a music generator. The recording decides *what* was performed; a score you verify decides *how* it is reproduced; a fidelity inspector checks the result against the original.

**Current state, in one paragraph.** The platform runs end to end with real audio processing: upload, forensic measurement, neural source separation (Demucs), melody and polyphonic transcription, section detection, an editable score, MIDI rendering (FluidSynth), mixing, mastering, an A/B fidelity comparison, seven human approval gates and genuine exports. What it does **not** yet do is produce a professional-sounding choir: no choir synthesis engine is connected and none is faked. Sung parts render as a wordless guide tone, and the instrument sound is a General MIDI SoundFont. See [docs/STATUS.md](docs/STATUS.md) for the honest feature matrix.

## Quick start

**Docker (simplest)**

```bash
cp .env.example .env
docker compose up --build
# open http://localhost:8000
```

**Local (Debian/Ubuntu, Python 3.13, Node 22)**

```bash
scripts/setup.sh      # ffmpeg, fluidsynth, SoundFont, Python deps, frontend build
scripts/run.sh        # http://127.0.0.1:8000
```

Frontend development with hot reload: `cd frontend && npm run dev` (proxies `/api` to port 8000).

The first source-separation run downloads the Demucs weights (~80 MB) from `dl.fbaipublicfiles.com`. On an offline server set `RESONANCE_DEMUCS_REPO` to a folder holding them. Without the weights the pipeline still runs and says clearly that separation was skipped.

## The workflow

1. Create an account, create a project (choose Restore / Faithful / Creative), confirm you have the rights to the recording.
2. Upload the recording (MP3, WAV, FLAC, M4A). The original is stored untouched with a checksum.
3. **Start analysis.** Agents measure the audio, separate voices from accompaniment, estimate tempo/key/chords/notes and find sections.
4. Review and correct, tab by tab: **Structure**, **Score** (piano roll + notation), **Lyrics**, **Arrangement**. Each has an approval gate.
5. **Render**: every track is performed from the score, mixed in a church acoustic and mastered.
6. **Compare & fidelity**: instant A/B against the original and a discrepancy report you approve or reject item by item.
7. **Export**: WAV/MP3 master, stems, MIDI, MusicXML, lyrics, fidelity report, full project archive. Files are named `DRAFT` until all seven gates are approved.

Editing anything upstream (for example a note in an approved score) automatically re-opens every gate that depended on it and marks old renders and reports out of date. Nothing is ever overwritten: each save is a new version.

## Tests

```bash
cd backend && python -m pytest            # 85 tests, about 3 minutes
node e2e/journey.mjs path/to/audio.mp3    # browser journey against a running server (needs Playwright)
```

The backend suite builds a synthetic recording with known notes, tempo, key and section boundaries and measures the algorithms against it. Measured numbers are in [docs/STATUS.md](docs/STATUS.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/STATUS.md](docs/STATUS.md) | Feature-completion matrix, measured accuracy, known limitations, external services and costs, next milestones |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, audio-processing pipeline, the twelve agents, data model, gates and invalidation |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Configuration, dependencies, security notes, backup and recovery, evaluating a real recording |

## Repository layout

```
backend/resonance/
  audio/      decoding, measurements, DSP (EQ, compressor, reverb, limiter, denoise), FluidSynth rendering
  music/      tempo, key, chords, pitch tracking, polyphonic clean-up, structure, alignment, Latin/Spanish text helpers
  score/      canonical score model, MIDI and MusicXML import/export
  agents/     the specialist agents (each a function doing real work through the modules above)
  director.py orchestration, pipeline dependencies, plain-English status
  gates.py    review gates and dependency invalidation
  jobs.py     durable database-backed job queue with retries
  main.py     HTTP API
backend/tests/  synthetic ground-truth fixture and test suite
frontend/src/   React + TypeScript studio
e2e/            browser journey test
```
