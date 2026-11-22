# Architecture

## Overview

```
Browser (React + TypeScript + Tailwind)
   │  HTTPS, HttpOnly session cookie, Server-Sent Events for job progress
   ▼
FastAPI application (backend/resonance/main.py)
   ├── auth.py        accounts, sessions, per-owner project access
   ├── gates.py       7 review gates, dependency invalidation
   ├── director.py    pipelines, job dependencies, status in plain English
   ├── jobs.py        durable job queue (database table) + worker thread
   ├── agents/        specialist agents; each calls deterministic tools ▼
   ├── audio/         FFmpeg decode · measurements · DSP · FluidSynth
   ├── music/         tempo/key/chords · pYIN · Basic Pitch clean-up · structure · DTW · language
   ├── score/         canonical score model · MIDI · MusicXML
   ├── llm.py         optional provider boundary (text-only narration)
   └── storage.py     object-storage abstraction (local disk implemented)
SQLAlchemy → SQLite (default) or PostgreSQL        Files → DATA_DIR/objects/<project>/<kind>/<asset>
```

**Design decisions**

- **Agents are code, not prompts.** Each agent is a Python function that runs real tools and returns a structured result with provenance and an explicit statement of uncertainty. The optional LLM only rewrites already-computed findings into plain English, receives text and numbers only, and is off unless enabled per project.
- **One process, database-backed queue.** Audio jobs are CPU-bound and minutes long; a jobs table gives durability, retries and history without Redis. `jobs.enqueue` / `Worker.run_one` is the seam for moving to Celery/RQ.
- **Time in seconds on the recording's own clock.** Notes are stored at the times they occur in the source recording, not as quantised beats, so the performers' expressive timing is the reference. A beat grid is stored alongside for notation export.
- **Append-only documents.** Score, sections, lyrics, arrangement, mix and master settings are versioned rows; the project holds a "head" pointer per document. Restoring an old version creates a new one.

## Audio-processing pipeline

| Step | Module | Method |
|---|---|---|
| Decode | `audio/io.py` | FFmpeg → 44.1 kHz float WAV working copy; original untouched, SHA-256 recorded |
| Measure | `audio/analysis.py` | ITU-R BS.1770 loudness (pyloudnorm); 4× oversampled true peak; run-length clipping detection; percentile noise floor; Welch spectrum; stereo correlation; mel spectrogram |
| Separate | `agents/separation.py` | Demucs `htdemucs` (hybrid transformer), CPU, 25% overlap → vocals, accompaniment, bass |
| Tempo / metre | `music/analysis.py` | librosa onset strength + dynamic-programming beat tracker (90 BPM prior); beat-synchronous autocorrelation for 3 vs 4 |
| Key / chords | | Krumhansl–Schmuckler profiles on CQT chroma; beat-synchronous triad templates |
| Melody | | pYIN on the voice stem → median-filtered pitch contour → notes; repeated notes split at level dips |
| Polyphony | | Basic Pitch (ICASSP 2022, ONNX) → merge fragments → confidence/duration filter → flag overtone suspects → register split |
| Structure | | Foote novelty on smoothed MFCC + spectral contrast + level; voice entries/exits from the stem; snap to beats; group by chroma similarity |
| Render | `audio/synth.py` | Per-track MIDI → FluidSynth → WAV stems |
| Mix | `audio/dsp.py`, `agents/production.py` | Stage each stem to −24 dBFS RMS → fader → constant-power pan → RBJ biquad EQ → optional 2:1 compressor → sum → synthetic church impulse response (convolution) → 3 dB headroom |
| Master | | Loudness normalise to target → look-ahead true-peak limiter with oversampled detection → 24-bit WAV + 320 kbps MP3 |
| Restore (Mode A) | | STFT spectral gating from a quiet-frame noise profile → corrective EQ from measured band energies |
| Compare | `agents/fidelity.py` | Chroma DTW alignment map; per-section cosine similarity; note-by-note pitch check against the voice stem; symbolic precision/recall |

Thresholds for the polyphonic clean-up and structure detector were tuned against the synthetic fixture in `backend/tests/fixtures.py`, which is also what the accuracy tests assert against.

## The agents

| # | Agent | Reads | Produces |
|---|---|---|---|
| 1 | Audio Forensics | source | working copy, waveform, spectrogram, metrics, observations, recommendations |
| 2 | Source Separation | working copy | stems with warnings; **blocked** (not faked) if the model is unavailable |
| 3 | Musical Transcription | working copy, stems | score version (unverified), pitch track; never replaces an imported/edited/approved score |
| 4 | Lyrics and Language | typed or reference text, sections | timed draft, pronunciation guide; syllable underlay on request |
| 5 | Structural Analysis | working copy, voice stem, beats | section set (detected) |
| 6 | Arrangement Reconstruction | current score | tracks, one per part; carries instrument choices forward |
| 7 | Choir Production | arrangement, lyrics | engine availability, rehearsal tracks, pronunciation sheet |
| 8 | Instrumental Production | arrangement, score | rendered stems, a Reconstruction record |
| 9 | Mixing Engineer | stems, mix settings | stereo mix with measurements |
| 10 | Mastering Engineer | mix, master settings | WAV + MP3 masters marked draft |
| 11 | Fidelity Inspector | original, mix, score, sections | checks, alignment map, discrepancies with optional fixes |
| 12 | Production Director | everything | pipelines, status, next action, gate enforcement |
| A | Restoration (Mode A) | working copy | restored audio |

Job states: `queued → running → succeeded | failed | blocked | cancelled`. A failing job is retried up to `RESONANCE_JOB_MAX_ATTEMPTS`. `blocked` means an engine or input is missing; it is not retried and its message is shown verbatim. A job whose dependency failed is cancelled; one whose dependency was merely blocked still runs (transcription proceeds on the full mix when separation is unavailable). Jobs left `running` by a crash are re-queued at start-up.

## Review gates and invalidation

| Gate | Approves | Requires |
|---|---|---|
| 1 | Recording analysed | forensics output |
| 2 | Musical structure | a section set |
| 3 | Lyrics and transcription | a score |
| 4 | Arrangement | arrangement built from the current score; no additions in faithful mode |
| 5 | Rendered performance | an up-to-date mix |
| 6 | Fidelity comparison | a report for the current render |
| 7 | Final master | an up-to-date master |

Gates must be approved in order. Restore mode uses gates 1, 5 and 7 only.

| Change | Gates re-opened | Marked out of date |
|---|---|---|
| Re-analysis | 1–7 | arrangements, renders, masters, reports |
| Sections edited | 2, 6, 7 | fidelity reports |
| Lyrics edited | 3, 6, 7 | fidelity reports |
| Score edited / imported / correction applied | 3–7 | arrangements, renders, masters, reports |
| Arrangement edited | 4–7 | renders, masters, reports |
| New render or re-mix | 5–7 | previous reports |
| New master | 7 | |

A stale arrangement blocks rendering until rebuilt. Audio exports are named `DRAFT` until every applicable gate is approved.

## Data model

`User`, `AuthSession`, `Project` (mode, rights metadata, retention, external-AI consent, head pointers), `Asset` (every file: source, working, stem, render_stem, rehearsal, human_stem, mix, master, score_import…), `Job`, `AgentOutput`, `ScoreVersion`, `SectionSet`, `LyricsVersion`, `Arrangement` (instrument and vocal tracks), `MixSettings`, `MasterSettings`, `Reconstruction` (binds exact score/arrangement/mix/master versions to their audio), `FidelityReport`, `ReviewDecision`, `ProviderSetting`, `AuditEvent`.

Every `Asset` records: source recording, parent asset, producing job, method, engine and version, settings, checksum, creation time and verification status (`unverified · stale · draft · final · approved`). Combined with versioned documents this makes any output reproducible from its inputs.

## Frontend

`frontend/src/studio/context.tsx` holds project state and subscribes to the job event stream; a change in any job triggers a refetch. One shared audio player (`usePlayer`) drives the waveform, the piano-roll playhead and the lyric/section tools. The waveform is drawn on canvas from peak data computed server-side. The piano roll is SVG. Notation uses OpenSheetMusicDisplay, loaded on demand, from the same MusicXML the export produces.
