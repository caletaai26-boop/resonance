import { useEffect, useState } from "react";
import { Doc, api, fmtTime } from "../api";
import { Empty, Panel, StatusChip } from "../components/ui";
import { VersionBar } from "./StructureTab";
import { Transport } from "./Timeline";
import { useStudio } from "./context";

type Line = { text: string; start: number | null; end: number | null; kind: string; block: string; status: string; uncertain: boolean };
type Lyrics = { language: string; source: string; lines: Line[]; pronunciation: { word: string; syllables: string[]; guide?: string }[]; notes: string[] };

export default function LyricsTab() {
  const s = useStudio();
  const [doc, setDoc] = useState<Doc<Lyrics> | null>(null);
  const [lines, setLines] = useState<Line[]>([]);
  const [dirty, setDirty] = useState(false);
  const [text, setText] = useState("");
  const [refs, setRefs] = useState<any[]>([]);
  const load = () => api.get(`/api/projects/${s.project.id}/doc/lyrics`).then((d: Doc<Lyrics>) => { setDoc(d); setLines(d.current?.data.lines || []); setDirty(false); });
  useEffect(() => { if (!dirty) load().catch((e) => s.setError(e.message)); }, [s.project.id, s.version]);
  useEffect(() => { api.get("/api/reference-texts").then(setRefs).catch(() => {}); }, []);
  const asr = s.caps?.engines.find((e: any) => e.area === "Lyric recognition");
  const set = (i: number, patch: Partial<Line>) => { setLines((l) => l.map((x, k) => (k === i ? { ...x, ...patch } : x))); setDirty(true); };

  async function save() {
    try {
      const r = await api.put(`/api/projects/${s.project.id}/doc/lyrics`, { data: { ...(doc?.current?.data || { language: s.project.language, source: "user-typed", notes: [] }), lines }, note: "Lyrics edited" });
      setDirty(false); s.flash(`Lyrics saved as version ${r.current.version}.`); await s.refresh(); load();
    } catch (e: any) { s.setError(e.message); }
  }
  async function underlay() {
    try { const r = await api.post(`/api/projects/${s.project.id}/lyrics/apply-to-score`); s.flash(`${r.stats.syllables_placed} syllables placed under the vocal line (${r.stats.syllables_unplaced} could not be placed). ${r.message}`); await s.refresh(); }
    catch (e: any) { s.setError(e.message); }
  }

  if (!doc) return null;
  const has = lines.length > 0;
  return (
    <div className="space-y-4">
      {asr?.status !== "ready" && <div className="callout"><span className="text-amber font-medium">Automatic lyric recognition is not available.</span> {asr?.detail} The words must come from you: type what you hear, or load a published text and correct it against the recording. Nothing is ever replaced silently.</div>}
      {!has && (
        <Panel title="Provide the words">
          <div className="grid md:grid-cols-2 gap-5">
            <div>
              <label className="label" htmlFor="ly">Type or paste the words as sung. Leave a blank line between verses and refrains.</label>
              <textarea id="ly" className="input font-display text-[15px] leading-relaxed" rows={10} value={text} onChange={(e) => setText(e.target.value)} placeholder={"Adeste fideles…\n\nVenite adoremus…"} />
              <button className="btn-primary mt-2" disabled={!text.trim() || s.working} onClick={() => s.runAgent("lyrics", { text })}>Create lyric draft</button>
            </div>
            <div>
              <div className="label">Or start from a published reference text</div>
              {refs.filter((r) => r.language === s.project.language).map((r) => (
                <div key={r.key} className="rounded-lg border border-ink-600 p-3 mb-2">
                  <div className="text-sm">{r.title}</div>
                  <p className="note mt-1">{r.note}</p>
                  <button className="btn btn-sm mt-2" disabled={s.working} onClick={() => s.runAgent("lyrics", { reference: r.key })}>Load as a draft to correct</button>
                </div>
              ))}
              {refs.filter((r) => r.language === s.project.language).length === 0 && <p className="note">No reference text is bundled for this language.</p>}
            </div>
          </div>
        </Panel>
      )}
      {has && (
        <>
          {(doc.current?.data.notes || []).map((n, i) => <div key={i} className="callout">{n}</div>)}
          <Panel title="Lines" action={<div className="flex items-center gap-2">{dirty && <span className="text-[12px] text-amber">Unsaved changes</span>}<button className="btn-primary btn-sm" disabled={!dirty} onClick={save}>Save as new version</button></div>}>
            <div className="mb-3"><Transport player={s.player} /></div>
            <div className="space-y-1">
              {lines.map((l, i) => (
                <div key={i}>
                  {(i === 0 || lines[i - 1].block !== l.block) && <div className="panel-title mt-3 mb-1">{l.block}</div>}
                  <div className="flex flex-wrap items-center gap-2">
                    <button className="font-mono text-[12px] text-copper-soft hover:underline w-14 text-left" disabled={l.start == null} onClick={() => { if (l.start != null) { s.player.setLoop(l.end != null ? [l.start, l.end] : null); s.player.seek(l.start); s.player.play(); } }}>{l.start != null ? fmtTime(l.start) : "no time"}</button>
                    <input className="input !py-1 flex-1 min-w-[240px] font-display text-[15px]" value={l.text} onChange={(e) => set(i, { text: e.target.value })} aria-label={`Line ${i + 1}`} />
                    <button className="btn-ghost btn-sm" title="Set this line's start to the playhead" onClick={() => set(i, { start: Number(s.player.time.toFixed(2)), end: l.end != null && l.end > s.player.time ? l.end : Number((s.player.time + 4).toFixed(2)) })}>⇤ start</button>
                    <button className="btn-ghost btn-sm" title="Set this line's end to the playhead" onClick={() => set(i, { end: Number(s.player.time.toFixed(2)) })}>end ⇥</button>
                    <label className="flex items-center gap-1.5 text-[12px] text-ivory-mute cursor-pointer"><input type="checkbox" className="accent-[#8fb69a]" checked={l.status === "verified"} onChange={(e) => set(i, { status: e.target.checked ? "verified" : "draft", uncertain: !e.target.checked })} />matches recording</label>
                    <button className="btn-ghost btn-sm !text-rose/70" aria-label="Remove line" onClick={() => { setLines((x) => x.filter((_, k) => k !== i)); setDirty(true); }}>✕</button>
                  </div>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-2 mt-4">
              <button className="btn btn-sm" onClick={() => { setLines((l) => [...l, { text: "", start: null, end: null, kind: "verse", block: l[l.length - 1]?.block || "Block 1", status: "draft", uncertain: true }]); setDirty(true); }}>Add line</button>
              <button className="btn btn-sm" disabled={dirty} title={dirty ? "Save first" : ""} onClick={underlay}>Place syllables under the vocal line</button>
              <button className="btn-ghost btn-sm" onClick={() => { if (confirm("Start over with new text? The current version stays in history.")) { setLines([]); setDirty(false); setDoc({ ...doc, current: doc.current && { ...doc.current, data: { ...doc.current.data, lines: [] } } }); } }}>Replace text…</button>
            </div>
            <p className="note mt-3">Line times were spread arithmetically across the vocal sections; they were not found by listening. Play each line, set its start and end from the playhead, correct the words to what is actually sung, then tick “matches recording”.</p>
            <div className="mt-2"><VersionBar doc={doc} kind="lyrics" onRestore={load} /></div>
          </Panel>
          <Panel title="Pronunciation guide (rule-based draft)">
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-1 text-[13px]">
              {(doc.current?.data.pronunciation || []).map((p) => (
                <div key={p.word} className="flex justify-between gap-3 border-b border-ink-700/40 py-1"><span className="font-display text-[15px]">{p.syllables.join("·")}</span><span className="text-ivory-mute font-mono text-[12px]">{p.guide}</span></div>
              ))}
            </div>
            <p className="note mt-3">Ecclesiastical (Roman) Latin as normally sung in church: soft c and g before e and i, “gn” as “ny”. Generated by rules, so check unusual words.</p>
          </Panel>
        </>
      )}
    </div>
  );
}
