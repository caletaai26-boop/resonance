import { useEffect, useState } from "react";
import { Doc, api, fmtDate } from "../api";
import { Empty, Explain, Panel, StatusChip } from "../components/ui";
import { useStudio } from "./context";

export function useReconstructions() {
  const s = useStudio();
  const [recs, setRecs] = useState<any[] | null>(null);
  useEffect(() => { api.get(`/api/projects/${s.project.id}/reconstructions`).then(setRecs).catch((e) => s.setError(e.message)); }, [s.project.id, s.version]);
  return recs;
}

function Player({ label, url, note }: { label: string; url: string; note?: string }) {
  const s = useStudio();
  return (
    <div className="grid md:grid-cols-[1fr_340px] gap-3 items-center py-2 border-b border-ink-700/50 last:border-0">
      <div><div className="text-[13px]">{label}</div>{note && <div className="text-[12px] text-ivory-mute">{note}</div>}</div>
      <audio controls preload="none" src={url} className="w-full h-9" onPlay={() => s.player.pause()} />
    </div>
  );
}

export default function ProduceTab() {
  const s = useStudio();
  const recs = useReconstructions();
  const [mix, setMix] = useState<any>(null);
  const [mixDirty, setMixDirty] = useState(false);
  const [master, setMaster] = useState<any>(null);
  const [masterDirty, setMasterDirty] = useState(false);
  const [arr, setArr] = useState<any>(null);
  const [rp, setRp] = useState({ strength: 0.5, voices_db: 2.5, brightness: 0.6, width: 0.5, remove_voices: false });
  const restore = s.project.mode === "restore";
  useEffect(() => {
    if (!mixDirty) api.get(`/api/projects/${s.project.id}/doc/mix`).then((d: Doc) => setMix(d.current?.data || d.default)).catch(() => {});
    if (!masterDirty) api.get(`/api/projects/${s.project.id}/doc/master`).then((d: Doc) => setMaster(d.current?.data || d.default)).catch(() => {});
    api.get(`/api/projects/${s.project.id}/doc/arrangement`).then((d: Doc) => setArr(d.current)).catch(() => {});
  }, [s.project.id, s.version]);
  const caps = s.caps;
  const latest = recs?.[0];
  const current = recs?.find((r) => r.status !== "stale");
  const g4 = s.status.gates.find((g) => g.gate === 4);
  const synthEngine = caps?.engines.find((e: any) => e.area === "Instrument rendering");
  const setTrack = (id: string, patch: any) => { setMix((m: any) => ({ ...m, tracks: { ...m.tracks, [id]: { ...(m.tracks?.[id] || {}), ...patch } } })); setMixDirty(true); };

  async function saveMix(andRun: boolean) {
    try { await api.put(`/api/projects/${s.project.id}/doc/mix`, { data: mix, note: "Mix settings changed" }); setMixDirty(false); if (andRun) await s.runAgent("mixing"); else await s.refresh(); } catch (e: any) { s.setError(e.message); }
  }
  async function saveMaster(andRun: boolean) {
    try { await api.put(`/api/projects/${s.project.id}/doc/master`, { data: master, note: "Mastering target changed" }); setMasterDirty(false); if (andRun) await s.runAgent("mastering"); else await s.refresh(); } catch (e: any) { s.setError(e.message); }
  }

  if (!recs || !caps) return null;
  const tgt = master && caps.master_targets[master.target];
  return (
    <div className="space-y-4">
      {restore ? (
        <Panel title="Enhance the original performance">
          <p className="note">Mode A keeps your actual recording: every voice and note is the original. It separates the voices from the accompaniment, cleans each one, brings the choir forward, restores brightness the phone cut off (built from overtones of the notes actually sung), opens a mono recording into stereo, and masters it. It cannot remove talking that overlaps the singing, undo the church echo, or repair distortion.</p>
          <div className="grid sm:grid-cols-2 gap-x-6 gap-y-4 mt-4">
            <label><span className="label">Noise reduction: {Math.round(rp.strength * 100)}%</span><input type="range" min={0} max={1} step={0.05} value={rp.strength} onChange={(e) => setRp({ ...rp, strength: Number(e.target.value) })} className="w-full" />
              <span className="text-[12px] text-ivory-mute">Too high dulls quiet singing. 40–60% is usually right.</span></label>
            <label className={rp.remove_voices ? "opacity-40" : ""}><span className="label">Bring the choir forward: {rp.voices_db > 0 ? "+" : ""}{rp.voices_db} dB</span><input type="range" min={-3} max={6} step={0.5} disabled={rp.remove_voices} value={rp.voices_db} onChange={(e) => setRp({ ...rp, voices_db: Number(e.target.value) })} className="w-full" />
              <span className="text-[12px] text-ivory-mute">Uses the separated voices. Above +4 dB separation artefacts may become audible.</span></label>
            <label><span className="label">Restore brightness: {Math.round(rp.brightness * 100)}%</span><input type="range" min={0} max={1} step={0.05} value={rp.brightness} onChange={(e) => setRp({ ...rp, brightness: Number(e.target.value) })} className="w-full" />
              <span className="text-[12px] text-ivory-mute">Adds the overtones the phone microphone lost. Too much sounds harsh.</span></label>
            <label><span className="label">Stereo width (for mono recordings): {Math.round(rp.width * 100)}%</span><input type="range" min={0} max={1} step={0.05} value={rp.width} onChange={(e) => setRp({ ...rp, width: Number(e.target.value) })} className="w-full" />
              <span className="text-[12px] text-ivory-mute">Played on one speaker it stays identical to the cleaned mono.</span></label>
          </div>
          <label className="flex gap-3 items-start callout-info cursor-pointer mt-4">
            <input type="checkbox" className="mt-1 accent-[#c98a5a]" checked={rp.remove_voices} onChange={(e) => setRp({ ...rp, remove_voices: e.target.checked })} />
            <span><span className="text-ivory">Make an instrumental: remove the choir.</span> Keeps only the separated accompaniment (talking and coughs mostly go with the voices), then cleans and masters it. Where the separator cannot tell organ from voice, a little organ can go too.</span>
          </label>
          <div className="flex flex-wrap gap-2 mt-4">
            <button className="btn-primary" disabled={s.working} onClick={() => s.runPipeline("restore", { restore: rp })}>{current ? "Enhance again with these settings" : "Enhance and master"}</button>
            {current && <button className="btn" disabled={s.working} onClick={() => s.runAgent("restore", rp)}>Re-enhance only (keep separation)</button>}
          </div>
          {latest?.notes?.steps?.length > 0 && <ul className="note list-disc pl-5 mt-3 space-y-1">{latest.notes.steps.map((t: string, i: number) => <li key={i}>{t}</li>)}</ul>}
        </Panel>
      ) : (
        <Panel title="Render the reconstruction" action={<StatusChip status={synthEngine?.status || "needs_setup"} label={synthEngine?.status === "ready" ? "FluidSynth ready" : "Renderer needs setup"} />}>
          {!arr ? <p className="note">Build an arrangement first (Arrangement tab).</p> : (
            <>
              <p className="note">Every enabled track is performed from the current score by a MIDI instrument engine, then mixed, mastered and inspected against the original. Notes and timing follow the score exactly.</p>
              <div className="callout mt-3"><span className="text-amber font-medium">What this render is, honestly:</span> a General MIDI SoundFont performance. It proves the notes, timing and structure. It is not yet the professional-quality sound this project is aiming for, and sung parts are a wordless guide tone until real voices are attached.</div>
              {g4?.state !== "approved" && <p className="text-[13px] text-amber mt-3">The arrangement (Gate 4) has not been approved. You can still render a preview; it cannot become a final master until the earlier checkpoints are approved.</p>}
              <div className="flex flex-wrap gap-2 mt-3">
                <button className="btn-primary" disabled={s.working} onClick={() => s.runPipeline("produce")}>{current ? "Render again from the current score" : "Render, mix, master and inspect"}</button>
              </div>
            </>
          )}
        </Panel>
      )}

      {recs.length === 0 && !s.working && <Empty title="Nothing rendered yet">When a render finishes, its stems, mix and master appear here to listen to.</Empty>}

      {latest && (
        <Panel title={`${restore ? "Restoration" : "Reconstruction"} v${latest.version}`} action={<div className="flex items-center gap-2"><span className="text-[12px] text-ivory-mute">{fmtDate(latest.created_at)}</span><StatusChip status={latest.status === "stale" ? "stale" : latest.status === "final" ? "final" : "draft"} label={latest.status === "stale" ? "Out of date" : latest.status === "final" ? "Final" : latest.status === "mastered" ? "Mastered · draft" : latest.status === "mixed" ? "Mixed" : "Rendered"} /></div>}>
          {latest.status === "stale" && <div className="callout mb-3">The score or arrangement changed after this was rendered. It no longer represents the current score. Render again.</div>}
          {latest.notes?.guide_tone_vocals?.length > 0 && <p className="text-[12px] text-amber mb-2">Guide-tone vocals (not a choir): {latest.notes.guide_tone_vocals.join(", ")}</p>}
          {latest.master && <Player label={latest.master.label} url={latest.master.url} note={`${latest.master.meta.measurements?.integrated_lufs?.toFixed(1)} LUFS · true peak ${latest.master.meta.measurements?.true_peak_db?.toFixed(1)} dBTP · ${latest.master.verification === "final" ? "FINAL" : "DRAFT"}`} />}
          {latest.mix && <Player label={latest.mix.label} url={latest.mix.url} note={latest.mix.method} />}
          {latest.stems.map((a: any) => <Player key={a.id} label={a.label} url={a.url} note={`${a.engine} ${a.engine_version}${a.settings?.soundfont ? ` · ${a.settings.soundfont}` : ""}${a.meta?.notes ? ` · ${a.meta.notes} notes` : ""}`} />)}
        </Panel>
      )}

      {!restore && mix && arr && (
        <Panel title="Mix" action={<div className="flex items-center gap-2">{mixDirty && <span className="text-[12px] text-amber">Unsaved</span>}<button className="btn-primary btn-sm" disabled={s.working || !current} onClick={() => saveMix(true)}>{mixDirty ? "Save and re-mix" : "Re-mix"}</button></div>}>
          <div className="grid sm:grid-cols-3 gap-4">
            <label><span className="label">Acoustic space <Explain>The room the performance is placed in. These are computed room models, not recordings of real churches. Longer rooms blur fast passages.</Explain></span>
              <select className="input !py-1.5" value={mix.reverb_preset} onChange={(e) => { setMix({ ...mix, reverb_preset: e.target.value, reverb_wet_db: null }); setMixDirty(true); }}>
                {Object.entries(caps.reverb_presets).map(([k, v]: any) => <option key={k} value={k}>{v.label}{v.rt60 ? ` · ${v.rt60}s` : ""}</option>)}</select></label>
            <label><span className="label">Amount of room: {mix.reverb_wet_db ?? caps.reverb_presets[mix.reverb_preset]?.wet_db} dB</span><input type="range" min={-30} max={-6} step={1} className="w-full" disabled={mix.reverb_preset === "dry"} value={mix.reverb_wet_db ?? caps.reverb_presets[mix.reverb_preset]?.wet_db} onChange={(e) => { setMix({ ...mix, reverb_wet_db: Number(e.target.value) }); setMixDirty(true); }} /></label>
            <label><span className="label">Stereo width: {Math.round((mix.width ?? 1) * 100)}%</span><input type="range" min={0} max={1.5} step={0.05} className="w-full" value={mix.width ?? 1} onChange={(e) => { setMix({ ...mix, width: Number(e.target.value) }); setMixDirty(true); }} /></label>
          </div>
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead><tr className="text-left text-[11px] uppercase tracking-wider text-ivory-mute"><th className="py-1.5 font-medium">Track</th><th className="font-medium">Level</th><th className="font-medium">Position</th><th className="font-medium">Clarity</th><th className="font-medium">Even out</th><th className="font-medium">Mute</th></tr></thead>
              <tbody>
                {arr.data.tracks.filter((t: any) => t.enabled).map((t: any) => {
                  const c = mix.tracks?.[t.id] || {};
                  return (
                    <tr key={t.id} className="table-row">
                      <td className="py-2 pr-3">{t.name}<div className="text-[11px] text-ivory-mute">{t.instrument}</div></td>
                      <td className="pr-3"><input type="range" min={-24} max={6} step={0.5} value={c.gain_db ?? t.gain_db} onChange={(e) => setTrack(t.id, { gain_db: Number(e.target.value) })} aria-label={`${t.name} level`} /> <span className="font-mono text-[11px] text-ivory-mute">{c.gain_db ?? t.gain_db} dB</span></td>
                      <td className="pr-3"><input type="range" min={-1} max={1} step={0.05} value={c.pan ?? t.pan} onChange={(e) => setTrack(t.id, { pan: Number(e.target.value) })} aria-label={`${t.name} position`} /></td>
                      <td className="pr-3"><input type="range" min={-4} max={6} step={0.5} value={c.presence_db ?? 0} onChange={(e) => setTrack(t.id, { presence_db: Number(e.target.value) })} aria-label={`${t.name} clarity`} title="Boost or cut around 3 kHz, where consonants and note attacks live" /></td>
                      <td className="pr-3"><input type="checkbox" className="accent-[#c98a5a]" checked={!!c.compress} onChange={(e) => setTrack(t.id, { compress: e.target.checked })} aria-label={`${t.name} gentle compression`} title="Gentle 2:1 compression" /></td>
                      <td><input type="checkbox" className="accent-[#c98a5a]" checked={!!c.mute} onChange={(e) => setTrack(t.id, { mute: e.target.checked })} aria-label={`Mute ${t.name}`} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {master && (
        <Panel title="Mastering" action={<div className="flex items-center gap-2">{masterDirty && <span className="text-[12px] text-amber">Unsaved</span>}<button className="btn-primary btn-sm" disabled={s.working || !current?.mix} onClick={() => saveMaster(true)}>{masterDirty ? "Save and re-master" : "Re-master"}</button></div>}>
          <div className="grid md:grid-cols-2 gap-2">
            {Object.entries(caps.master_targets).map(([k, v]: any) => (
              <label key={k} className={`flex gap-3 rounded-lg border px-3 py-2.5 cursor-pointer transition ${master.target === k ? "border-copper/70 bg-copper/5" : "border-ink-600 hover:border-ink-500"}`}>
                <input type="radio" name="target" className="mt-1 accent-[#c98a5a]" checked={master.target === k} onChange={() => { setMaster({ ...master, target: k }); setMasterDirty(true); }} />
                <span><span className="text-sm font-medium">{v.label}</span><span className="font-mono text-[11px] text-ivory-mute ml-2">{v.lufs} LUFS · {v.ceiling_db} dBTP</span><span className="block note mt-0.5">{v.explain}</span></span>
              </label>
            ))}
          </div>
          <label className="block mt-3 max-w-xs"><span className="label">Tone: {master.tilt_db > 0 ? "brighter" : master.tilt_db < 0 ? "warmer" : "neutral"} ({master.tilt_db ?? 0} dB)</span><input type="range" min={-3} max={3} step={0.5} className="w-full" value={master.tilt_db ?? 0} onChange={(e) => { setMaster({ ...master, tilt_db: Number(e.target.value) }); setMasterDirty(true); }} /></label>
          {tgt && <p className="note mt-2">Louder is not better for sacred choral music: louder targets force the limiter to flatten the difference between quiet and full passages.</p>}
        </Panel>
      )}
    </div>
  );
}
