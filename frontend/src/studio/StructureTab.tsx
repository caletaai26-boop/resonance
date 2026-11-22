import { useEffect, useState } from "react";
import { Doc, Section, api, fmtTime } from "../api";
import { Empty, Panel, StatusChip } from "../components/ui";
import { Transport, Waveform } from "./Timeline";
import { useStudio } from "./context";

const PRESETS = ["Instrumental introduction", "Verse 1", "Verse 2", "Verse 3", "Verse 4", "Refrain", "Instrumental interlude", "Final refrain", "Ending"];

export function VersionBar({ doc, kind, onRestore }: { doc: Doc | null; kind: string; onRestore: () => void }) {
  const s = useStudio();
  const [open, setOpen] = useState(false);
  if (!doc?.current) return null;
  return (
    <div className="text-[12px] text-ivory-mute">
      <button className="hover:text-ivory" onClick={() => setOpen((o) => !o)}>Version {doc.current.version} · {doc.current.origin} · {doc.versions.length} in history {open ? "▴" : "▾"}</button>
      {open && (
        <ul className="mt-2 panel p-2 space-y-1 max-h-56 overflow-auto">
          {doc.versions.map((v) => (
            <li key={v.id} className="flex items-center justify-between gap-3 px-1.5 py-1 rounded hover:bg-ink-800">
              <span className="truncate"><span className="text-ivory font-mono">v{v.version}</span> · {v.origin} · {v.note || "no note"} · {new Date(v.created_at).toLocaleString()}</span>
              {v.id === doc.head_id ? <StatusChip status={v.verification === "approved" ? "approved" : "info"} label={v.verification === "approved" ? "Current · approved" : "Current"} />
                : <button className="btn-ghost btn-sm" onClick={async () => { try { const r = await api.post(`/api/projects/${s.project.id}/doc/${kind}/${v.id}/make-current`); if (r.invalidation?.gates_invalidated?.length) s.flash(`Restored. Checkpoints re-opened: ${r.invalidation.gates_invalidated.join(", ")}.`); await s.refresh(); onRestore(); } catch (e: any) { s.setError(e.message); } }}>Restore as new version</button>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function StructureTab() {
  const s = useStudio();
  const [doc, setDoc] = useState<Doc<{ sections: Section[] }> | null>(null);
  const [rows, setRows] = useState<Section[]>([]);
  const [dirty, setDirty] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const load = () => api.get(`/api/projects/${s.project.id}/doc/sections`).then((d) => { setDoc(d); setRows(d.current?.data.sections || []); setDirty(false); });
  useEffect(() => { if (!dirty) load().catch((e) => s.setError(e.message)); }, [s.project.id, s.version]);
  const art = s.agent("forensics")?.output?.data?.artifacts;
  const duration = s.agent("forensics")?.output?.data?.metrics?.duration_s || s.player.duration;
  const set = (i: number, patch: Partial<Section>) => { setRows((r) => r.map((x, k) => (k === i ? { ...x, ...patch, status: "verified" } : x))); setDirty(true); };

  function splitAtPlayhead() {
    const t = s.player.time;
    const i = rows.findIndex((r) => t > r.start + 0.5 && t < r.end - 0.5);
    if (i < 0) return s.setError("Move the playhead inside a section (at least half a second from its edges) to split it there.");
    const r = rows[i];
    setRows([...rows.slice(0, i), { ...r, end: t, status: "verified" }, { ...r, start: t, label: "New section", status: "verified" }, ...rows.slice(i + 1)]);
    setDirty(true);
  }
  function remove(i: number) {
    const next = rows.filter((_, k) => k !== i);
    if (i > 0 && next[i - 1]) next[i - 1] = { ...next[i - 1], end: rows[i].end };
    else if (next[0]) next[0] = { ...next[0], start: rows[i].start };
    setRows(next); setDirty(true);
  }
  function setBoundary(i: number, t: number) { // moving the start of section i also moves the end of section i-1
    setRows((r) => r.map((x, k) => (k === i ? { ...x, start: t, status: "verified" } : k === i - 1 ? { ...x, end: t } : x)));
    setDirty(true);
  }
  async function save() {
    try {
      const r = await api.put(`/api/projects/${s.project.id}/doc/sections`, { data: { sections: rows.map((x) => ({ ...x, status: "verified" })), duration }, note: "Section timeline edited" });
      setDirty(false);
      s.flash(r.invalidation?.gates_invalidated?.length ? `Saved as v${r.current.version}. Checkpoints re-opened: Gate ${r.invalidation.gates_invalidated.join(", ")}.` : `Saved as version ${r.current.version}.`);
      await s.refresh(); load();
    } catch (e: any) { s.setError(e.message); }
  }

  if (!doc) return null;
  if (!doc.current && rows.length === 0)
    return <Empty title="No section timeline yet">Run the analysis on the Recording tab, or run Structural Analysis on its own.
      <div className="mt-4 flex gap-2 justify-center"><button className="btn-primary" disabled={s.working} onClick={() => s.runAgent("structure")}>Detect sections</button>
        <button className="btn" onClick={() => { setRows([{ start: 0, end: duration, label: "Whole piece", kind: "unknown", group: "A", status: "verified" }]); setDirty(true); }}>Start a timeline by hand</button></div></Empty>;
  const how = s.agent("structure")?.output?.data;
  return (
    <div className="space-y-4">
      <Panel title="Section timeline" action={<div className="flex items-center gap-3"><span className="text-[11px] text-ivory-mute">dashed = detected · solid = verified by you</span></div>}>
        <Waveform peaksUrl={art ? `/api/assets/${art.waveform}/file` : null} duration={duration} player={s.player} sections={rows} onSectionClick={setSel} />
        <div className="mt-3"><Transport player={s.player} extra={<button className="btn btn-sm" onClick={splitAtPlayhead}>Split section at playhead ({fmtTime(s.player.time)})</button>} /></div>
      </Panel>
      {how?.uncertainty && doc.current?.origin === "auto-structure" && <div className="callout">{how.uncertainty}</div>}
      <Panel title="Sections" action={<div className="flex items-center gap-2">
        {dirty && <span className="text-[12px] text-amber">Unsaved changes</span>}
        <button className="btn btn-sm" disabled={s.working} onClick={() => s.runAgent("structure")}>Detect again</button>
        <button className="btn-primary btn-sm" disabled={!dirty} onClick={save}>Save as new version</button></div>}>
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead><tr className="text-left text-[11px] uppercase tracking-wider text-ivory-mute"><th className="py-1.5 pr-2 font-medium">Starts</th><th className="pr-2 font-medium">Ends</th><th className="pr-2 font-medium">Name</th><th className="pr-2 font-medium">Content</th><th className="pr-2 font-medium" title="Sections with the same letter sound alike harmonically">Sounds like</th><th className="pr-2 font-medium">Status</th><th /></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className={`table-row ${sel === i ? "bg-ink-800/60" : ""}`}>
                  <td className="py-1.5 pr-2 whitespace-nowrap">
                    <input type="number" step={0.1} min={0} disabled={i === 0} className="input !w-20 !py-1 font-mono" value={Number(r.start.toFixed(2))} onChange={(e) => setBoundary(i, Number(e.target.value))} />
                    {i > 0 && <button className="btn-ghost btn-sm ml-1" title="Set this boundary to the current playhead position" onClick={() => setBoundary(i, Number(s.player.time.toFixed(2)))}>⇤ playhead</button>}
                  </td>
                  <td className="pr-2 font-mono text-ivory-mute">{fmtTime(r.end)}</td>
                  <td className="pr-2"><input list="section-names" className="input !py-1" value={r.label} onChange={(e) => set(i, { label: e.target.value })} /></td>
                  <td className="pr-2"><select className="input !py-1 !w-36" value={r.kind} onChange={(e) => set(i, { kind: e.target.value as any })}><option value="vocal">Voices</option><option value="instrumental">Instruments only</option><option value="unknown">Not sure</option></select></td>
                  <td className="pr-2"><input className="input !py-1 !w-14 text-center font-mono" maxLength={2} value={r.group} onChange={(e) => set(i, { group: e.target.value.toUpperCase() })} /></td>
                  <td className="pr-2"><StatusChip status={r.status} /></td>
                  <td className="text-right whitespace-nowrap"><button className="btn-ghost btn-sm" onClick={() => { s.player.setLoop([r.start, r.end]); s.player.seek(r.start); s.player.play(); }}>Loop</button>
                    {rows.length > 1 && <button className="btn-ghost btn-sm" title="Merge into the neighbouring section" onClick={() => remove(i)}>Merge</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <datalist id="section-names">{PRESETS.map((p) => <option key={p} value={p} />)}</datalist>
        </div>
        <div className="mt-3"><VersionBar doc={doc} kind="sections" onRestore={load} /></div>
      </Panel>
    </div>
  );
}
