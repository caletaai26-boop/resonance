import { useEffect, useRef, useState } from "react";
import { Asset, Doc, api } from "../api";
import { Empty, Meter, Panel, StatusChip } from "../components/ui";
import { VersionBar } from "./StructureTab";
import { useStudio } from "./context";

type Track = { id: string; part_id: string; name: string; role: string; voice: string; program: number; instrument: string; rendering: string; human_stem_asset_id: string | null; enabled: boolean; gain_db: number; pan: number; source: string; note_count: number; mean_confidence: number | null; instrument_confirmed: boolean };
type Arr = { mode: string; score_version_id: string; score_version: number; tracks: Track[]; additions: any[] };

export default function ArrangementTab() {
  const s = useStudio();
  const [doc, setDoc] = useState<Doc<Arr> | null>(null);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [dirty, setDirty] = useState(false);
  const [rehearsal, setRehearsal] = useState<Asset[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const target = useRef<string>("");
  const load = () => api.get(`/api/projects/${s.project.id}/doc/arrangement`).then((d: Doc<Arr>) => { setDoc(d); setTracks(d.current?.data.tracks || []); setDirty(false); });
  useEffect(() => { if (!dirty) load().catch((e) => s.setError(e.message)); api.get(`/api/projects/${s.project.id}/assets?kind=rehearsal`).then(setRehearsal).catch(() => {}); }, [s.project.id, s.version]);
  const instruments: Record<string, number> = s.caps?.instruments || {};
  const set = (i: number, patch: Partial<Track>) => { setTracks((t) => t.map((x, k) => (k === i ? { ...x, ...patch } : x))); setDirty(true); };
  const scoreId = s.status.current.score_version_id;
  const outdated = doc?.current && (doc.current.verification === "stale" || doc.current.data.score_version_id !== scoreId);
  const choir = s.agent("choir");

  async function save() {
    try {
      const r = await api.put(`/api/projects/${s.project.id}/doc/arrangement`, { data: { ...doc!.current!.data, tracks }, note: "Arrangement edited" });
      setDirty(false);
      s.flash(`Arrangement saved as version ${r.current.version}.` + (r.invalidation?.stale?.reconstructions ? " Existing renders are now out of date." : ""));
      await s.refresh(); load();
    } catch (e: any) { s.setError(e.message); }
  }
  async function attach(f: File) {
    try { await api.upload(`/api/projects/${s.project.id}/human-stem`, f, { track_id: target.current }); s.flash("Recording attached to the track. It will be used instead of a synthetic sound."); setDirty(false); await s.refresh(); load(); }
    catch (e: any) { s.setError(e.message); }
  }

  if (!doc) return null;
  if (!doc.current)
    return <Empty title="No arrangement yet">An arrangement says which voice or instrument performs each part of the score. It is built from the current score and adds nothing to it.
      <div className="mt-4"><button className="btn-primary" disabled={s.working || !scoreId} onClick={() => s.runAgent("arrangement")}>Build arrangement from the score</button></div>
      {!scoreId && <p className="mt-2">Create or import a score first.</p>}</Empty>;

  return (
    <div className="space-y-4">
      {outdated && <div className="callout flex items-center justify-between gap-3"><span><span className="text-amber font-medium">The score has changed since this arrangement was built.</span> Rebuild it so tracks match the current notes. Your instrument choices are kept.</span><button className="btn-primary btn-sm shrink-0" disabled={s.working} onClick={() => s.runAgent("arrangement")}>Rebuild</button></div>}
      <div className={`rounded-lg border px-3 py-2.5 text-[13px] ${s.project.mode === "faithful" ? "border-sage/40 bg-sage/5" : "border-amber/40 bg-amber/5"}`}>
        {s.project.mode === "faithful"
          ? <><span className="text-sage font-medium">Faithful mode.</span> <span className="text-ivory-dim">Exactly one track per score part. Adding instruments or harmonies is refused here; change the project to Creative Reimagining if you want that.</span></>
          : <><span className="text-amber font-medium">Creative mode.</span> <span className="text-ivory-dim">Changes to instrumentation are allowed and this result must not be presented as a faithful reconstruction.</span></>}
      </div>
      <Panel title={`Tracks · built from score v${doc.current.data.score_version}`} action={<div className="flex items-center gap-2">{dirty && <span className="text-[12px] text-amber">Unsaved changes</span>}<button className="btn-primary btn-sm" disabled={!dirty} onClick={save}>Save as new version</button></div>}>
        <input ref={fileRef} type="file" accept=".wav,.flac,.mp3,.m4a" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) attach(f); e.target.value = ""; }} />
        <div className="space-y-2">
          {tracks.map((t, i) => (
            <div key={t.id} className={`rounded-lg border border-ink-700/70 p-3 ${t.enabled ? "" : "opacity-55"}`}>
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 cursor-pointer"><input type="checkbox" className="accent-[#c98a5a]" checked={t.enabled} onChange={(e) => set(i, { enabled: e.target.checked })} aria-label={`Include ${t.name}`} /><span className="text-sm font-medium">{t.name}</span></label>
                <span className="chip-idle">{t.role === "vocal" ? "Sung part" : "Instrument"}</span>
                <span className="text-[12px] text-ivory-mute">{t.note_count} notes{t.mean_confidence != null && <> · confidence <Meter value={t.mean_confidence} tone={t.mean_confidence > 0.7 ? "sage" : "copper"} /></>}</span>
                <span className="ml-auto">{t.rendering === "human-stem" ? <StatusChip status="ready" label="Human recording" /> : t.rendering === "guide-tone" ? <StatusChip status="warn" label="Guide tone, not a choir" /> : t.instrument_confirmed ? <StatusChip status="approved" label="Instrument confirmed" /> : <StatusChip status="unverified" label="Instrument not confirmed" />}</span>
              </div>
              <div className="grid sm:grid-cols-[1fr_150px_150px_auto] gap-3 mt-2.5 items-end">
                <label><span className="label">{t.role === "vocal" ? "Stand-in sound until real voices are recorded" : "Instrument that played this in the recording"}</span>
                  <select className="input !py-1.5" disabled={t.rendering === "human-stem"} value={t.program} onChange={(e) => { const prog = Number(e.target.value); const name = Object.entries(instruments).find(([, v]) => v === prog)?.[0] || `GM ${prog}`; set(i, { program: prog, instrument: name, instrument_confirmed: t.role !== "vocal" }); }}>
                    {Object.entries(instruments).map(([name, prog]) => <option key={name} value={prog}>{name}</option>)}</select></label>
                <label><span className="label">Level {t.gain_db > 0 ? "+" : ""}{t.gain_db} dB</span><input type="range" min={-24} max={6} step={0.5} className="w-full" value={t.gain_db} onChange={(e) => set(i, { gain_db: Number(e.target.value) })} /></label>
                <label><span className="label">Position {t.pan === 0 ? "centre" : t.pan < 0 ? `${Math.round(-t.pan * 100)}% left` : `${Math.round(t.pan * 100)}% right`}</span><input type="range" min={-1} max={1} step={0.05} className="w-full" value={t.pan} onChange={(e) => set(i, { pan: Number(e.target.value) })} /></label>
                <div className="flex gap-1.5">
                  {t.role === "instrument" && !t.instrument_confirmed && <button className="btn btn-sm" onClick={() => set(i, { instrument_confirmed: true })}>Confirm</button>}
                  <button className="btn btn-sm" title="Use a real recording of this part instead of a synthetic sound" onClick={() => { if (dirty) return s.setError("Save your changes before attaching a recording."); target.current = t.id; fileRef.current?.click(); }}>{t.rendering === "human-stem" ? "Replace recording" : "Attach human recording"}</button>
                </div>
              </div>
            </div>
          ))}
        </div>
        <p className="note mt-3">The analysis finds notes; it cannot tell which instrument played them. Instrumental tracks default to a church organ until you say otherwise. Levels and positions set here are the starting point for the mix.</p>
        <div className="mt-2"><VersionBar doc={doc} kind="arrangement" onRestore={load} /></div>
      </Panel>

      <Panel title="Choir production" action={<button className="btn btn-sm" disabled={s.working || !!outdated} onClick={() => s.runAgent("choir")}>{choir?.output ? "Rebuild rehearsal pack" : "Prepare rehearsal pack"}</button>}>
        <div className="callout"><span className="text-amber font-medium">No choir synthesis engine is connected, and none is simulated.</span> Sung parts are rendered as a wordless guide tone so you can check pitches and timing. To get real voices, record singers against the rehearsal tracks and attach each recording to its track above.</div>
        {choir?.output && (
          <div className="mt-3 space-y-2">
            {(choir.output.data.engines || []).map((e: any) => <div key={e.name} className="text-[13px]"><span className="text-ivory">{e.name}</span> <StatusChip status={e.available ? "ready" : "needs_setup"} label={e.available ? "Available" : "Not available"} /><div className="note">{e.reason}</div></div>)}
            {rehearsal.length > 0 && <div className="pt-2"><div className="panel-title mb-2">Rehearsal pack</div>
              {rehearsal.map((a) => <div key={a.id} className="flex flex-wrap items-center justify-between gap-3 py-1.5 border-b border-ink-700/50 last:border-0 text-[13px]"><span>{a.label}</span>
                {a.mime.startsWith("audio") ? <audio controls preload="none" src={a.url} className="h-8 w-72" onPlay={() => s.player.pause()} /> : <a className="btn btn-sm" href={`${a.url}?download=1`}>Download</a>}</div>)}</div>}
          </div>
        )}
      </Panel>
    </div>
  );
}
