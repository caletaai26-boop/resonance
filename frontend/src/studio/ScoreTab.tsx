import { useCallback, useEffect, useRef, useState } from "react";
import { Doc, Note, Part, Score, api, pitchName } from "../api";
import { Empty, Explain, Panel, StatusChip } from "../components/ui";
import { PART_COLOURS, PianoRoll, beep } from "./PianoRoll";
import { useSections } from "./OverviewTab";
import { VersionBar } from "./StructureTab";
import { Transport } from "./Timeline";
import { useStudio } from "./context";

const uid = () => Math.random().toString(16).slice(2, 12);
const VOICES = ["melody", "soprano", "alto", "tenor", "bass", "harmony", "accompaniment"];

function Notation({ projectId, versionId }: { projectId: string; versionId: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  useEffect(() => {
    let live = true;
    setState("loading");
    (async () => {
      try {
        const [{ OpenSheetMusicDisplay }, xml] = await Promise.all([import("opensheetmusicdisplay"), fetch(`/api/projects/${projectId}/score.musicxml?version_id=${versionId}`, { credentials: "same-origin" }).then((r) => { if (!r.ok) throw new Error(); return r.text(); })]);
        if (!live || !host.current) return;
        host.current.innerHTML = "";
        const osmd = new OpenSheetMusicDisplay(host.current, { autoResize: true, backend: "svg", drawTitle: false, drawComposer: false, drawPartNames: true, drawingParameters: "compacttight" });
        await osmd.load(xml);
        osmd.render();
        if (live) setState("ready");
      } catch { if (live) setState("error"); }
    })();
    return () => { live = false; };
  }, [projectId, versionId]);
  return (
    <div>
      {state === "loading" && <p className="note">Engraving notation…</p>}
      {state === "error" && <p className="note text-rose">The notation could not be drawn for this version. The piano roll and MIDI export are unaffected.</p>}
      <div ref={host} className="bg-[#f6f1e4] rounded-lg p-3 overflow-auto max-h-[560px]" style={{ display: state === "ready" ? "block" : "none" }} />
      <p className="note mt-2">Notation is a quantised reading: rhythms are rounded to sixteenth notes against the detected beat grid. The exact performance timing lives in the piano roll and MIDI. Shows the last saved version.</p>
    </div>
  );
}

export default function ScoreTab() {
  const s = useStudio();
  const sections = useSections();
  const [doc, setDoc] = useState<Doc<Score> | null>(null);
  const [score, setScore] = useState<Score | null>(null);
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [active, setActive] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [pps, setPps] = useState(60);
  const [view, setView] = useState<"roll" | "notation">("roll");
  const [note, setNote] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [importMsg, setImportMsg] = useState<string | null>(null);

  const load = useCallback(() => api.get(`/api/projects/${s.project.id}/doc/score`).then((d: Doc<Score>) => {
    setDoc(d); setScore(d.current ? structuredClone(d.current.data) : null); setDirty(false); setSelected(new Set());
    setActive((a) => (d.current?.data.parts.some((p) => p.id === a) ? a : d.current?.data.parts[0]?.id || null));
  }), [s.project.id]);
  useEffect(() => { if (!dirty) load().catch((e) => s.setError(e.message)); }, [s.version, load]);

  const mutate = (fn: (sc: Score) => void, markDirty = true) => { setScore((prev) => { if (!prev) return prev; const next = structuredClone(prev); fn(next); return next; }); if (markDirty) setDirty(true); };
  const onChange = useCallback((partId: string, n: Note, commit: boolean) => {
    setScore((prev) => prev && { ...prev, parts: prev.parts.map((p) => (p.id !== partId ? p : { ...p, notes: p.notes.map((x) => (x.id === n.id ? n : x)) })) });
    if (commit) setDirty(true);
  }, []);
  const onSelect = useCallback((ids: Set<string>, partId?: string) => { setSelected(ids); if (partId) setActive(partId); }, []);
  const onAdd = useCallback((partId: string, start: number, pitch: number) => {
    const id = uid();
    mutate((sc) => { const beat = sc.tempo_bpm ? 60 / sc.tempo_bpm : 0.5; sc.parts.find((p) => p.id === partId)?.notes.push({ id, start, end: start + beat, pitch, velocity: 80, confidence: null, lyric: "", uncertain: false, verified: true }); });
    setSelected(new Set([id])); beep(pitch);
  }, []);

  const selNotes: { n: Note; part: Part }[] = score ? score.parts.flatMap((p) => p.notes.filter((n) => selected.has(n.id)).map((n) => ({ n, part: p }))) : [];
  const editSel = (patch: Partial<Note>) => mutate((sc) => sc.parts.forEach((p) => p.notes.forEach((n) => { if (selected.has(n.id)) Object.assign(n, patch); })));
  const deleteSel = () => { mutate((sc) => sc.parts.forEach((p) => { p.notes = p.notes.filter((n) => !selected.has(n.id)); })); setSelected(new Set()); };

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName) || selected.size === 0) return;
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSel(); }
      const step = (dp: number, dt: number) => { e.preventDefault(); mutate((sc) => sc.parts.forEach((p) => p.notes.forEach((n) => { if (selected.has(n.id)) { n.pitch = Math.max(0, Math.min(127, n.pitch + dp)); const st = Math.max(0, n.start + dt); n.end += st - n.start; n.start = st; n.verified = true; n.uncertain = false; } }))); };
      if (e.key === "ArrowUp") step(e.shiftKey ? 12 : 1, 0);
      if (e.key === "ArrowDown") step(e.shiftKey ? -12 : -1, 0);
      if (e.key === "ArrowLeft") step(0, -0.05);
      if (e.key === "ArrowRight") step(0, 0.05);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [selected]);

  async function save() {
    if (!score) return;
    try {
      const r = await api.put(`/api/projects/${s.project.id}/doc/score`, { data: score, note: note || "Score edited" });
      setDirty(false); setNote("");
      const g = r.invalidation?.gates_invalidated || [];
      const st = r.invalidation?.stale || {};
      s.flash(`Saved as version ${r.current.version}.` + (g.length || st.reconstructions ? ` Because the score changed: ${g.length ? `Gate ${g.join(", ")} re-opened; ` : ""}${st.reconstructions || 0} render(s) and ${st.fidelity_reports || 0} fidelity report(s) marked out of date.` : ""));
      await s.refresh(); load();
    } catch (e: any) { s.setError(e.message); }
  }
  async function importFile(f: File) {
    try {
      const r = await api.upload(`/api/projects/${s.project.id}/score/import`, f);
      setImportMsg([r.message, ...(r.warnings || [])].join(" "));
      setDirty(false); await s.refresh(); load();
    } catch (e: any) { s.setError(e.message); }
  }

  const importer = (
    <>
      <input ref={fileRef} type="file" accept=".mid,.midi,.musicxml,.xml,.mxl,.pdf" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = ""; }} />
      <button className="btn btn-sm" onClick={() => fileRef.current?.click()}>Import MIDI / MusicXML / PDF</button>
    </>
  );

  if (!doc) return null;
  if (!score)
    return <div className="space-y-4">{importMsg && <div className="callout-info">{importMsg}</div>}
      <Empty title="No score yet">A score is the list of notes that will be performed. Best: import the score the choir sang from (MusicXML or MIDI), then align it to the recording. Alternatively let the Transcription agent guess one from the audio (unreliable on a choir), or enter notes by hand.
        <div className="mt-4 flex flex-wrap gap-2 justify-center"><button className="btn-primary" disabled={s.working} onClick={() => s.runAgent("transcription")}>Transcribe from the recording</button>{importer}
          <button className="btn btn-sm" onClick={() => { const id = uid(); setScore({ key: null, key_confidence: null, time_signature: [4, 4], meter_confidence: null, tempo_bpm: 90, tempo_curve: [], beats: [], downbeat_offset: 0, duration: s.player.duration, chords: [], warnings: [], parts: [{ id, name: "Melody", role: "vocal", voice: "melody", program: 52, source: "user", notes: [] }] }); setActive(id); setDirty(true); }}>Start an empty score</button></div></Empty></div>;

  const total = score.parts.reduce((a, p) => a + p.notes.length, 0);
  const uncertain = score.parts.reduce((a, p) => a + p.notes.filter((n) => n.uncertain && !n.verified).length, 0);
  const activePart = score.parts.find((p) => p.id === active);
  const one = selNotes.length === 1 ? selNotes[0] : null;

  return (
    <div className="space-y-4">
      {importMsg && <div className="callout-info flex justify-between gap-3"><span>{importMsg}</span><button onClick={() => setImportMsg(null)}>✕</button></div>}
      {doc.current?.origin.startsWith("import") && !dirty && (
        <div className="rounded-lg border border-copper/50 bg-copper/5 px-3 py-3 flex flex-wrap items-center justify-between gap-3">
          <div className="text-[13px] max-w-2xl"><span className="text-copper-soft font-medium">This imported score is not yet on the recording's timing.</span>{" "}
            <span className="text-ivory-dim">Align it: RESONANCE finds the key the choir actually sang in, matches every section of your timeline to the right part of the score, and moves each note to where the performers placed it. Check the Structure tab first: sung parts are placed only in sections marked “Voices”.</span></div>
          <button className="btn-primary btn-sm shrink-0" disabled={s.working} onClick={() => s.runAgent("alignment", { score_version_id: doc.current!.id })}>Align to the recording</button>
        </div>
      )}
      {doc.current?.origin.startsWith("auto-transcription") && (
        <div className="callout"><span className="text-amber font-medium">This score was guessed from the recording.</span> On a live choir, automatic transcription is unreliable: several voices sing at once in a reverberant room, and the result has many wrong and missing notes. For a faithful result, import the score the choir sang from (MusicXML or MIDI, e.g. a public-domain SATB edition) and align it to the recording.</div>
      )}
      {score.warnings.length > 0 && <ul className="callout space-y-1 list-disc pl-6">{score.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
      <Panel title={<span className="flex items-center gap-2">Score editor <StatusChip status={doc.current?.verification === "approved" && !dirty ? "approved" : "unverified"} label={dirty ? "Unsaved edits" : doc.current?.verification === "approved" ? "Approved" : "Not yet approved"} /></span>}
        action={<div className="flex items-center gap-2">
          <div className="flex rounded-md border border-ink-600 overflow-hidden text-xs">{(["roll", "notation"] as const).map((v) => <button key={v} className={`px-2.5 py-1 ${view === v ? "bg-ink-700 text-ivory" : "text-ivory-mute hover:text-ivory"}`} onClick={() => setView(v)}>{v === "roll" ? "Piano roll" : "Notation"}</button>)}</div>
          {importer}</div>}>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 mb-3 text-[13px]">
          <label className="flex items-center gap-2">Tempo <input type="number" min={20} max={300} step={0.5} className="input !w-24 !py-1 font-mono" value={score.tempo_bpm != null ? Math.round(score.tempo_bpm * 10) / 10 : ""} onChange={(e) => mutate((sc) => { sc.tempo_bpm = Number(e.target.value) || null; })} /> BPM</label>
          <label className="flex items-center gap-2">Metre <select className="input !w-20 !py-1" value={score.time_signature.join("/")} onChange={(e) => mutate((sc) => { sc.time_signature = e.target.value.split("/").map(Number) as [number, number]; })}>{["2/4", "3/4", "4/4", "6/8", "2/2"].map((t) => <option key={t}>{t}</option>)}</select></label>
          <label className="flex items-center gap-2">Key <input className="input !w-28 !py-1" value={score.key ?? ""} placeholder="e.g. G major" onChange={(e) => mutate((sc) => { sc.key = e.target.value || null; })} />
            {score.key_confidence != null && <span className="text-ivory-mute text-[12px]">auto: {Math.round(score.key_confidence * 100)}% sure</span>}</label>
          <span className="text-ivory-mute">{total} notes · <span className={uncertain ? "text-amber" : "text-sage"}>{uncertain} uncertain</span></span>
          <label className="flex items-center gap-2 ml-auto text-ivory-mute">Zoom <input type="range" min={20} max={220} value={pps} onChange={(e) => setPps(Number(e.target.value))} /></label>
        </div>
        {view === "roll" ? (
          <>
            <PianoRoll score={score} sections={sections} player={s.player} hidden={hidden} activePart={active} selected={selected} onSelect={onSelect} onChange={onChange} onAdd={onAdd} pps={pps} />
            <div className="mt-3"><Transport player={s.player} extra={<span className="text-[12px] text-ivory-mute">The playhead plays the <b className="text-ivory-dim">original recording</b>, so you can check notes against what you hear.</span>} /></div>
            <p className="note mt-2">Solid = confident or verified · dashed outline = uncertain. Click a note to hear its pitch. Drag to move, drag the right edge to lengthen, double-click empty space to add to the highlighted part, Delete to remove, arrow keys to nudge (Shift+↑↓ = octave).</p>
          </>
        ) : doc.current ? <Notation projectId={s.project.id} versionId={doc.current.id} /> : <p className="note">Save the score first to see notation.</p>}
      </Panel>

      <div className="grid lg:grid-cols-[1fr_300px] gap-4">
        <Panel title="Parts" action={<button className="btn btn-sm" onClick={() => { const id = uid(); mutate((sc) => sc.parts.push({ id, name: `Part ${sc.parts.length + 1}`, role: "instrument", voice: "harmony", program: 19, source: "user", notes: [] })); setActive(id); }}>Add part</button>}>
          <div className="space-y-1.5">
            {score.parts.map((p, i) => {
              const unc = p.notes.filter((n) => n.uncertain && !n.verified).length;
              return (
                <div key={p.id} className={`flex flex-wrap items-center gap-2 rounded-lg border px-2.5 py-2 transition ${active === p.id ? "border-copper/60 bg-copper/5" : "border-ink-700/70"}`} onClick={() => setActive(p.id)}>
                  <span className="w-3 h-3 rounded-sm shrink-0" style={{ background: PART_COLOURS[i % PART_COLOURS.length] }} />
                  <input className="input !py-1 !w-52" value={p.name} onChange={(e) => mutate((sc) => { sc.parts[i].name = e.target.value; })} aria-label="Part name" />
                  <select className="input !py-1 !w-28" value={p.role} onChange={(e) => mutate((sc) => { sc.parts[i].role = e.target.value as any; })} aria-label="Voice or instrument"><option value="vocal">Sung</option><option value="instrument">Instrument</option></select>
                  <select className="input !py-1 !w-32" value={p.voice} onChange={(e) => mutate((sc) => { sc.parts[i].voice = e.target.value; })} aria-label="Voice type">{VOICES.map((v) => <option key={v}>{v}</option>)}</select>
                  <span className="text-[12px] text-ivory-mute">{p.notes.length} notes{unc > 0 && <span className="text-amber"> · {unc} uncertain</span>} · from {p.source}</span>
                  <span className="ml-auto flex gap-1">
                    <button className="btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); setHidden((h) => { const n = new Set(h); n.has(p.id) ? n.delete(p.id) : n.add(p.id); return n; }); }}>{hidden.has(p.id) ? "Show" : "Hide"}</button>
                    {unc > 0 && <button className="btn-ghost btn-sm" title="Remove every uncertain, unverified note in this part" onClick={(e) => { e.stopPropagation(); mutate((sc) => { sc.parts[i].notes = sc.parts[i].notes.filter((n) => !(n.uncertain && !n.verified)); }); }}>Remove uncertain</button>}
                    <button className="btn-ghost btn-sm" title="Mark every note in this part as checked by you" onClick={(e) => { e.stopPropagation(); mutate((sc) => sc.parts[i].notes.forEach((n) => { n.verified = true; n.uncertain = false; })); }}>Mark all verified</button>
                    <button className="btn-ghost btn-sm !text-rose/80" onClick={(e) => { e.stopPropagation(); if (confirm(`Delete the part “${p.name}” and its ${p.notes.length} notes?`)) mutate((sc) => { sc.parts.splice(i, 1); }); }}>Delete</button>
                  </span>
                </div>
              );
            })}
          </div>
        </Panel>
        <Panel title={selNotes.length > 1 ? `${selNotes.length} notes selected` : "Selected note"}>
          {selNotes.length === 0 && <p className="note">Click a note in the piano roll to edit it. Shift-click selects several.</p>}
          {one && (
            <div className="space-y-2.5 text-[13px]">
              <div className="flex items-center gap-2"><span className="h-display text-2xl">{pitchName(one.n.pitch)}</span><button className="btn-ghost btn-sm" onClick={() => beep(one.n.pitch, 0.6)}>Hear it</button>
                <button className="btn-ghost btn-sm" onClick={() => { s.player.setLoop([Math.max(0, one.n.start - 0.6), one.n.end + 0.6]); s.player.seek(Math.max(0, one.n.start - 0.6)); s.player.play(); }}>Loop original here</button></div>
              <div className="grid grid-cols-3 gap-2">
                <label><span className="label">Pitch</span><input type="number" min={0} max={127} className="input !py-1 font-mono" value={one.n.pitch} onChange={(e) => editSel({ pitch: Number(e.target.value), verified: true, uncertain: false })} /></label>
                <label><span className="label">Start (s)</span><input type="number" step={0.01} min={0} className="input !py-1 font-mono" value={Number(one.n.start.toFixed(3))} onChange={(e) => editSel({ start: Math.min(Number(e.target.value), one.n.end - 0.02) })} /></label>
                <label><span className="label">End (s)</span><input type="number" step={0.01} className="input !py-1 font-mono" value={Number(one.n.end.toFixed(3))} onChange={(e) => editSel({ end: Math.max(Number(e.target.value), one.n.start + 0.02) })} /></label>
              </div>
              <label className="block"><span className="label">Syllable sung on this note</span><input className="input !py-1" value={one.n.lyric} onChange={(e) => editSel({ lyric: e.target.value })} /></label>
              <label className="block"><span className="label">Belongs to part</span>
                <select className="input !py-1" value={one.part.id} onChange={(e) => { const to = e.target.value; mutate((sc) => { const from = sc.parts.find((p) => p.id === one.part.id)!; const n = from.notes.find((x) => x.id === one.n.id)!; from.notes = from.notes.filter((x) => x.id !== n.id); sc.parts.find((p) => p.id === to)!.notes.push(n); }); setActive(to); }}>
                  {score.parts.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
              {one.n.confidence != null && <div className="text-ivory-mute text-[12px]">Machine confidence {Math.round(one.n.confidence * 100)}% <Explain>How sure the detection model was. It is not a probability that the note is right: confident wrong notes are common on choir recordings.</Explain></div>}
            </div>
          )}
          {selNotes.length > 0 && (
            <div className="flex flex-wrap gap-2 mt-3">
              <button className="btn btn-sm" onClick={() => editSel({ verified: true, uncertain: false })}>Mark verified</button>
              <button className="btn btn-sm" onClick={() => editSel({ verified: false, uncertain: true })}>Mark uncertain</button>
              <button className="btn btn-sm !text-rose" onClick={deleteSel}>Delete</button>
            </div>
          )}
          {activePart && <p className="note mt-3 pt-3 border-t border-ink-700/60">New notes go into <span className="text-ivory">{activePart.name}</span>.</p>}
        </Panel>
      </div>

      <Panel title="Save">
        <div className="flex flex-wrap items-center gap-2">
          <input className="input !w-80" placeholder="What did you change? (kept in version history)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="btn-primary" disabled={!dirty} onClick={save}>Save as new version</button>
          {dirty && <button className="btn" onClick={() => { setDirty(false); load(); }}>Discard edits</button>}
          <button className="btn btn-sm ml-auto" disabled={s.working} onClick={() => s.runAgent("transcription")}>Transcribe again from the recording</button>
        </div>
        <p className="note mt-2">Saving never overwrites: every save is a new version you can return to. Saving a changed score marks any existing render and fidelity report as out of date and re-opens the checkpoints that depended on it.</p>
        <div className="mt-2"><VersionBar doc={doc} kind="score" onRestore={load} /></div>
      </Panel>
    </div>
  );
}
