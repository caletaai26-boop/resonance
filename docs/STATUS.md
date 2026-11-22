# Status, limitations and roadmap

Verified against the automated suite, the browser journey, and the first real recording (the Gibraltar Christmas Mass *Adeste Fideles*, 3 min 44 s phone recording). Synthetic-fixture figures are an upper bound; the real-recording findings in section 2b are what to plan around.

## 1. Feature-completion matrix

Legend: **Works** = implemented, real processing, covered by tests. **Partial** = works with a stated restriction. **Not available** = honest stub: the UI says so and nothing is simulated.

| Area | Capability | State | Notes |
|---|---|---|---|
| Foundation | Accounts, private projects, per-owner access control | Works | Other users get 404, not 403 |
| | Upload MP3/WAV/FLAC/M4A with validation, size limit, checksum | Works | Original is immutable |
| | Durable job queue, retries, crash recovery, live progress (SSE) | Works | One worker process |
| | Version history for score, sections, lyrics, arrangement, mix, master | Works | Append-only; restore creates a new version |
| | 7 review gates + dependency invalidation | Works | |
| Agent 1 Forensics | Loudness (LUFS), true peak, clipping, noise estimate, silence, spectrum, stereo, waveform, spectrogram | Works | Noise figure is an estimate |
| Agent 2 Separation | Voices / accompaniment / low-register stems (Demucs htdemucs) | Works | One voice stem for all singers; no SATB or per-instrument separation |
| Agent 3 Transcription | Tempo, beats, local tempo curve | Works | Can lock to 2/3 or 3/2 of the pulse; alternatives shown |
| | Key | Works | |
| | Metre | Partial | Only distinguishes 3 from 4 |
| | Chords | Partial | Major/minor triads only |
| | Melody from the voice stem (pYIN) | Works | Needs separation to have found voices |
| | Polyphonic notes (Basic Pitch) with clean-up | Partial | See measured accuracy; many errors expected on real choir audio |
| | SATB voice assignment | Partial | Register sorting (top/middle/bottom), not true voice separation |
| | Import MIDI / MusicXML, takes priority over auto-transcription | Works | |
| Agent 3b Alignment | Place an imported score on the recording's timing, in the key sung, section by section | Works | Fixture: key found; 78% of melody notes within 0.2 s, median error 0.07 s. Needs a verified section timeline |
| | Read notes from a PDF score (OMR) | Not available | PDF stored as reference only |
| Agent 4 Lyrics | Automatic recognition of sung words | Not available | Needs Whisper + model; even then sung Latin is unreliable |
| | Typed text or reference text → timed draft, pronunciation guide, syllable underlay | Works | Timings are arithmetic, flagged unverified; Latin reference: Adeste Fideles |
| Agent 5 Structure | Section boundaries | Works | ±2 s without voice stem, <0.1 s at voice entries with it (fixture) |
| | Voices vs instruments-only tagging | Works | Only with a usable voice stem |
| | Verse vs refrain naming | Not available | Sections are grouped by similarity; you name them |
| Agent 6 Arrangement | One track per score part, faithful-mode guard against additions | Works | |
| | Instrument identification from audio | Not available | Defaults to organ until you confirm |
| Agent 7 Choir | Realistic choir synthesis with Latin text | **Not available** | No engine connected, none faked |
| | Rehearsal guide tracks, pronunciation guide, human stem import | Works | The supported path to real voices today |
| Agent 8 Instruments | MIDI rendering per track (FluidSynth + FluidR3 GM) | Works | Accurate notes; basic realism |
| | Professional sampled instruments | Not available | Point `RESONANCE_SOUNDFONT` at a better library, or export MIDI to a DAW |
| Agent 9 Mixing | Gain staging, pan, EQ, compression, church reverb presets, width | Works | Reverb is an algorithmic room model |
| Agent 10 Mastering | BS.1770 loudness targets, true-peak limiter, WAV 24-bit + MP3 320 | Works | 4 selectable targets |
| Agent 11 Fidelity | Duration, DTW alignment map, per-section harmony, entries, tempo, missing sections | Works | |
| | Melody vs original pitch, with applicable corrections | Works | Only with a voice stem |
| | Imported score vs transcription (precision/recall) | Works | Needs both to exist |
| | Lyric accuracy | Not available | Reported as "not evaluated" |
| | Inner-voice accuracy | Not available | Cannot be verified from a mixed recording |
| Agent 12 Director | Pipelines, dependencies, gate enforcement, status | Works | Deterministic; optional LLM only rewrites findings in plain English |
| Mode A Restore | Spectral noise reduction, corrective EQ, mastering of the original | Works | Does not remove speech, reverb or clipping |
| Mode C Creative | Flag permits additions | Partial | No generative service connected (no official Suno API used) |
| UI | Dashboard, 3-panel studio, waveform/spectrogram, timeline, piano-roll editor, notation view, A/B compare with loop/speed, exports, settings | Works | |
| Exports | WAV, MP3, stems ZIP, MIDI, MusicXML, lyrics, fidelity JSON, project archive | Works | |
| | PDF score | Not available | Needs MuseScore on the server; export MusicXML instead |
| Hardening | 85 backend tests, browser journey, Docker, backup script | Works | See limitations for what is not hardened |

## 2. Measured accuracy (synthetic fixture: 58 s, organ + flute "voice", 100 BPM, C major, noise + MP3 degradation)

| Measure | Result | Truth |
|---|---|---|
| Tempo | 99.4 BPM | 100 |
| Key / metre | C major (0.95) / 4/4 | correct |
| Melody on an isolated voice stem | precision 1.00, recall 1.00 | 46 notes, ±0.2 s |
| Polyphonic notes on the degraded mix | precision 0.71, recall 0.54, F1 0.61 | 142 notes, ±0.2 s |
| Section boundaries, no voice stem | errors 1.9 / 2.3 / 1.1 s | |
| Section boundaries, with voice stem | errors ≤ 0.08 s, all tags correct | |
| Master loudness / peak | within 0.5 LU of target, ≤ −1 dBTP | re-measured on the exported file |

**How to read this.** Even on a clean synthetic organ, automatic polyphonic transcription misses about half the notes and one in four reported notes is wrong. A real choir in a reverberant church, recorded on a phone with talking, will be materially worse. This is why the product is built around human review and why an imported, trusted score always takes priority. The single most valuable input you can provide is the sheet music the choir sang from (as MusicXML or MIDI).

## 2b. First real recording: Adeste Fideles, Christmas Mass (phone, 224 s)

| Step | What happened |
|---|---|
| Forensics | −12.7 LUFS, no clipping, background only ~7 dB below the music, almost nothing above 2.2 kHz (phone microphone + compression) |
| Separation | Worked: voices 21% of energy, accompaniment 66% |
| Key | D major at 69%, with G major and A major almost tied: **not trustworthy** |
| Metre | 3/4: **wrong** (the hymn is in duple time) |
| Melody (pYIN on voice stem) | 361 notes, 40% shorter than 0.2 s, 175 semitone wobbles, 72 leaps of a fifth or more. A monophonic tracker cannot follow a four-part choir in a reverberant church. After the new clean-up: 278 notes, 54 wobbles, 34 leaps. Less chaotic, **still not the melody** |
| Polyphonic notes | 126 notes for 224 s: far too sparse; thresholds tuned on the synthetic organ are too strict for this audio |
| Sections | 19 fragments instead of intro / verses / refrains / interlude |
| Render | Faithfully reproduced those wrong notes, which is why it sounded "crazy" |

**Conclusion.** Automatic transcription is not a viable source of the score for a live choir recording of this quality. The workable path is: import a trusted score of the hymn (MusicXML/MIDI) → correct the section timeline → align the score to the recording → listen and fix differences. The alignment step was built in response to this result.

## 3. Known limitations

**Musical**
- The polyphonic model reports organ overtones as notes. They are flagged "uncertain", not deleted, because real octave doublings look the same.
- A repeated note sung legato on the same pitch can merge into one long note.
- A melody is only tracked when separation finds voices (voice stem ≥ 3% of energy). Otherwise the "melody" is the top line of the polyphonic result.
- Lyric timings and syllable underlay are drafts produced by arithmetic, not by listening.
- Alignment assumes the choir performed what the imported score says. Where they did something else (different harmony, descant, verse order) the result follows the score until you correct it. It also depends on a sensible section timeline: each section must lie within one pass of the hymn.
- Noise reduction estimates noise from the quietest 10% of frames; on a recording with no pause at all it can slightly dull sustained tones.

**Sound quality**
- General MIDI SoundFont instruments: correct notes, limited realism, no legato or expressive articulation.
- No choir. Vocal parts are a pad sound. This is the gap between today's build and the project's goal.

**Fidelity inspection**
- Where the score came from automatic transcription of the same recording, several checks are partly circular; the report says so. It never certifies a result as exact.
- A/B playback is not loudness-matched.

**Engineering**
- One in-process worker: jobs run one at a time, and the API must run as a single process.
- SQLite by default. `RESONANCE_DATABASE_URL` accepts PostgreSQL but that path is untested, and there are no schema migrations yet (tables are created on start).
- No password reset, e-mail verification, rate limiting or multi-user sharing. Registration should be closed (`RESONANCE_ALLOW_REGISTRATION=0`) after your account exists on any internet-facing server, and the server should sit behind HTTPS.
- Provider API keys are stored in the database in plain text (server-side only, masked in the API). Use disk encryption or a secrets manager for a shared deployment.
- Only local-disk storage is implemented; the storage interface is ready for S3-style backends.
- Demucs on CPU takes roughly the length of the recording.

## 4. External services and estimated costs

| Need | Option | Cost (indicative) | Status |
|---|---|---|---|
| Everything in the matrix marked Works | Open-source stack on your own server | Free software; a 4-core / 16 GB machine | Included |
| Better instrument sound, cheaply | A higher-quality SoundFont/SFZ organ and strings set | Free to ~£100 | Config change only |
| Professional instruments | Hauptwerk/GrandOrgue sample sets, Spitfire/VSL in a DAW, fed by the exported MIDI | ~£150–£600 per library | Manual hand-off |
| Real choir | Session singers recorded to the rehearsal tracks, stems imported | ~£150–£400 per singer per session; SATB quartet overdubbed is the minimum | Supported now |
| Synthetic choir with words | Word-building choir libraries (desktop plug-ins) or a licensed singing-synthesis API, if one proves able to sing Latin convincingly | ~£300–£700 one-off, or per-minute API pricing | Needs evaluation; none assumed |
| Lyric recognition | Whisper (open source, local) | Free; model download | Integration boundary only |
| Plain-English summaries | Anthropic API (optional, text-only) | Pennies per project | Implemented, off by default |
| PDF engraving | MuseScore on the server | Free | Detected automatically if installed |
| PDF score reading | Audiveris (open source) run separately | Free | Manual |

## 5. Recommended next milestones

1. **Process the real Adeste Fideles recording** and record how far automatic analysis gets. Everything after this should be prioritised by what that shows.
2. **Score-to-audio alignment: built.** Next: test it on the real recording once a MusicXML score is imported; tune section detection for hymns (it fragments into ~19 pieces); detect where the performance departs from the score (extra verses, a descant, changed harmony).
3. **Human choir session** using the rehearsal pack, to produce the first genuinely faithful reconstruction end to end.
4. **Instrument quality**: a proper pipe-organ sample set and SFZ support; velocity/expression curves derived from the original's level envelope.
5. **Whisper integration** for a first-pass lyric draft, with forced alignment to give real line timings.
6. **Loudness-matched A/B** and per-section fidelity drill-down in the piano roll (overlay the original's pitch track under the notes).
7. **Evaluate choir synthesis candidates** against a short Latin test phrase before committing to any.
8. Production hardening: PostgreSQL + migrations, separate worker process, object storage, password reset, rate limits, encrypted secrets.
