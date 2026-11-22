import { useCallback, useEffect, useMemo, useState } from "react";
import { api, fmtTime } from "../api";
import { Empty, Panel, StatusChip } from "../components/ui";
import { useSections } from "./OverviewTab";
import { useReconstructions } from "./ProduceTab";
import { Waveform } from "./Timeline";
import { usePlayer, useStudio } from "./context";

/** Piecewise-linear lookup through the alignment map produced by the Fidelity Inspector. */
function mapper(path: number[][] | null, reverse = false) {
  if (!path || path.length < 2) return (t: number) => t;
  const src = path.map((p) => p[reverse ? 1 : 0]), dst = path.map((p) => p[reverse ? 0 : 1]);
  return (t: number) => {
    if (t <= src[0]) return dst[0] + (t - src[0]);
    for (let i = 1; i < src.length; i++) if (t <= src[i]) { const f = src[i] === src[i - 1] ? 0 : (t - src[i - 1]) / (src[i] - src[i - 1]); return dst[i - 1] + f * (dst[i] - dst[i - 1]); }
    return dst[dst.length - 1] + (t - src[src.length - 1]);
  };
}

function usePeaksFromAudio(url: string | null) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!url) return setBlobUrl(null);
    let live = true, made: string | null = null;
    (async () => {
      try {
        const buf = await (await fetch(url, { credentials: "same-origin" })).arrayBuffer();
        const audio = await new AudioContext().decodeAudioData(buf);
        const ch = audio.getChannelData(0), n = 2400, k = Math.max(1, Math.floor(ch.length / n)), peaks: number[] = [];
        for (let i = 0; i < n; i++) { let m = 0; for (let j = i * k; j < Math.min(ch.length, (i + 1) * k); j += 8) m = Math.max(m, Math.abs(ch[j])); peaks.push(m); }
        made = URL.createObjectURL(new Blob([JSON.stringify({ peaks })], { type: "application/json" }));
        if (live) setBlobUrl(made);
      } catch { if (live) setBlobUrl(null); }
    })();
    return () => { live = false; if (made) URL.revokeObjectURL(made); };
  }, [url]);
  return blobUrl;
}

const TYPE_LABEL: Record<string, string> = { melody_pitch: "Melody note", melody_octave: "Melody octave", harmony: "Harmony", missing_section: "Missing section", ending: "Ending", entry: "Entry", tempo: "Tempo" };

export default function CompareTab() {
  const s = useStudio();
  const recs = useReconstructions();
  const sections = useSections();
  const [reports, setReports] = useState<any[]>([]);
  const [which, setWhich] = useState<"mix" | "master">("master");
  const [active, setActive] = useState<"A" | "B">("A");
  const loadReports = useCallback(() => api.get(`/api/projects/${s.project.id}/fidelity`).then(setReports).catch(() => {}), [s.project.id]);
  useEffect(() => { loadReports(); }, [s.version, loadReports]);

  const rec = recs?.find((r) => r.status !== "stale" && (r.mix || r.master)) || recs?.find((r) => r.mix || r.master);
  const asset = rec ? (which === "master" && rec.master ? rec.master : rec.mix || rec.master) : null;
  const B = usePlayer(asset?.url || null);
  const A = s.player;
  const report = reports.find((r) => rec && r.reconstruction_id === rec.id) || null;
  const d = report?.data;
  const shared = d ? d.shared_timeline : true;
  const a2b = useMemo(() => mapper(shared ? null : d?.alignment_map), [d, shared]);
  const b2a = useMemo(() => mapper(shared ? null : d?.alignment_map, true), [d, shared]);
  const peaksB = usePeaksFromAudio(asset?.url || null);
  const art = s.agent("forensics")?.output?.data?.artifacts;
  const durA = s.agent("forensics")?.output?.data?.metrics?.duration_s || A.duration;

  const switchTo = useCallback((to: "A" | "B") => {
    if (to === active) return;
    const from = active === "A" ? A : B, dest = to === "A" ? A : B;
    const t = active === "A" ? a2b(A.time) : b2a(B.time);
    const was = from.playing;
    from.pause(); dest.seek(Math.max(0, t));
    if (was) dest.play();
    setActive(to);
  }, [active, A, B, a2b, b2a]);

  // keep loop and speed in step across both players
  useEffect(() => { B.setRate(A.rate); }, [A.rate]);
  useEffect(() => { B.setLoop(A.loop ? [a2b(A.loop[0]), a2b(A.loop[1])] : null); }, [A.loop, a2b]);
  // a seek on the inactive track moves the active one to the equivalent musical position
  const playerA = useMemo(() => ({ ...A, seek: (t: number) => { A.seek(t); B.seek(a2b(t)); }, toggle: () => (active === "A" ? A.toggle() : B.toggle()), setLoop: A.setLoop }), [A, B, a2b, active]);
  const playerB = useMemo(() => ({ ...B, seek: (t: number) => { B.seek(t); A.seek(b2a(t)); }, toggle: () => (active === "B" ? B.toggle() : A.toggle()), setLoop: (l: [number, number] | null) => A.setLoop(l ? [b2a(l[0]), b2a(l[1])] : null) }), [A, B, b2a, active]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) return;
      if (e.key.toLowerCase() === "a") switchTo("A");
      if (e.key.toLowerCase() === "b") switchTo("B");
      if (e.key === " ") { e.preventDefault(); (active === "A" ? A : B).toggle(); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [switchTo, active, A, B]);
  useEffect(() => () => { B.pause(); }, []);

  async function decide(id: string, action: string) {
    try {
      const r = await api.post(`/api/projects/${s.project.id}/fidelity/${report.id}/discrepancies/${id}`, { action });
      if (r.applied) s.flash(`Correction applied as score v${r.score_version.version}. The render and this report are now out of date: render again to hear it.`);
      await s.refresh(); loadReports();
    } catch (e: any) { s.setError(e.message); }
  }
  const jump = (t: number | null) => { if (t == null) return; const st = Math.max(0, t - 1.5); A.seek(st); B.seek(a2b(st)); A.setLoop([st, t + 3]); (active === "A" ? A : B).play(); };

  if (!recs) return null;
  if (!rec) return <Empty title="Nothing to compare yet">Render the arrangement (or run the restoration) first. The original and the result then appear here side by side.</Empty>;
  const cur = active === "A" ? A : B;
  const open = (d?.discrepancies || []).filter((x: any) => x.status === "open").length;
  return (
    <div className="space-y-4">
      {rec.status === "stale" && <div className="callout">This render is out of date: the score or arrangement changed after it was made. You are hearing an old version. Render again on the Render tab.</div>}
      <Panel title="A / B comparison" action={
        <div className="flex items-center gap-2">
          {rec.master && rec.mix && <select className="input !py-1 !w-auto !text-xs" value={which} onChange={(e) => setWhich(e.target.value as any)}><option value="master">Reconstruction: master</option><option value="mix">Reconstruction: mix (before mastering)</option></select>}
          <StatusChip status={shared ? "ready" : "warn"} label={!d ? "No alignment yet: same clock assumed" : shared ? "Same clock" : "Aligned by map"} />
        </div>}>
        <div className="space-y-3">
          <div className={`rounded-lg p-2 transition ${active === "A" ? "bg-copper/5 ring-1 ring-copper/40" : ""}`} onDoubleClick={() => switchTo("A")}>
            <Waveform label="A · Original recording" peaksUrl={art ? `/api/assets/${art.waveform}/file` : null} duration={durA} player={playerA} sections={sections} height={96} timeMap={active === "B" ? () => b2a(B.time) : undefined} />
          </div>
          <div className={`rounded-lg p-2 transition ${active === "B" ? "bg-[#7fa7d9]/5 ring-1 ring-[#7fa7d9]/40" : ""}`} onDoubleClick={() => switchTo("B")}>
            <Waveform label={`B · ${s.project.mode === "restore" ? "Restoration" : "Reconstruction"} v${rec.version}`} peaksUrl={peaksB} duration={B.duration} player={playerB} height={96} colour="#7fa7d9" timeMap={active === "A" ? () => a2b(A.time) : undefined} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3 mt-3">
          <button className="btn-primary !rounded-full w-10 h-10 !px-0" onClick={cur.toggle} aria-label={cur.playing ? "Pause" : "Play"}>
            {cur.playing ? <svg width="14" height="14" viewBox="0 0 14 14"><rect x="2" y="1" width="3.5" height="12" rx="1" fill="currentColor" /><rect x="8.5" y="1" width="3.5" height="12" rx="1" fill="currentColor" /></svg> : <svg width="14" height="14" viewBox="0 0 14 14"><path d="M3 1.5v11l9-5.5z" fill="currentColor" /></svg>}
          </button>
          <div className="flex rounded-lg border border-ink-600 overflow-hidden" role="group" aria-label="Which recording is audible">
            <button className={`px-4 py-2 text-sm font-medium transition ${active === "A" ? "bg-copper text-ink-950" : "text-ivory-dim hover:bg-ink-800"}`} onClick={() => switchTo("A")}>A · Original</button>
            <button className={`px-4 py-2 text-sm font-medium transition ${active === "B" ? "bg-[#7fa7d9] text-ink-950" : "text-ivory-dim hover:bg-ink-800"}`} onClick={() => switchTo("B")}>B · {s.project.mode === "restore" ? "Restored" : "Reconstruction"}</button>
          </div>
          <span className="font-mono text-sm text-ivory-dim">{fmtTime(cur.time)}</span>
          <div className="flex gap-1">{[0.5, 0.75, 1].map((r) => <button key={r} className={`btn btn-sm ${A.rate === r ? "!border-copper/70 !text-copper-soft" : ""}`} onClick={() => A.setRate(r)}>{r}×</button>)}</div>
          {A.loop && <button className="btn btn-sm !border-champagne/60 !text-champagne" onClick={() => A.setLoop(null)}>Looping {fmtTime(A.loop[0])}–{fmtTime(A.loop[1])} ✕</button>}
          <span className="text-[12px] text-ivory-mute">Keys: A / B switch instantly, Space plays. Drag on a waveform to loop a passage in both.</span>
        </div>
        <p className="note mt-3">{!d ? "No fidelity report exists for this render, so both are assumed to share one clock (true for renders made from a transcription of this recording)."
          : shared ? `Both follow the same timeline (median offset ${d.checks.find((c: any) => c.key === "alignment")?.data.median_time_deviation_s}s), so switching keeps the musical position.`
            : "These two performances do not share a clock. Switching uses the alignment map computed by the Fidelity Inspector to jump to the equivalent musical moment; expect it to be approximate, not sample-accurate."} Loudness is not matched between A and B; the louder one tends to sound “better”, so judge the notes rather than the impact.</p>
      </Panel>

      {s.project.mode !== "restore" && (
        <Panel title="Fidelity report" action={<div className="flex items-center gap-2">{report && <StatusChip status={report.stale ? "stale" : "info"} label={report.stale ? "Out of date" : `${open} open item(s)`} />}<button className="btn btn-sm" disabled={s.working || !rec.mix || rec.status === "stale"} onClick={() => s.runAgent("fidelity")}>{report ? "Inspect again" : "Run the Fidelity Inspector"}</button></div>}>
          {!d ? <p className="note">The inspector compares the reconstruction with the original using several independent checks and lists every difference it can detect.</p> : (
            <>
              <div className="callout-info mb-3">{d.verdict}</div>
              <div className="divide-y divide-ink-700/60">
                {d.checks.map((c: any) => (
                  <div key={c.key} className="py-2.5 grid md:grid-cols-[220px_1fr_auto] gap-x-4 gap-y-1 items-start">
                    <div className="text-[13px]">{c.title}</div><div className="note">{c.detail}</div><StatusChip status={c.status} />
                  </div>
                ))}
              </div>
              {d.checks.find((c: any) => c.key === "harmony")?.data.sections?.length > 0 && (
                <div className="mt-3"><div className="panel-title mb-1.5">Harmonic similarity by section (relative guide only)</div>
                  <div className="flex gap-1 items-end h-16">{d.checks.find((c: any) => c.key === "harmony").data.sections.map((x: any, i: number) => (
                    <button key={i} className="flex-1 group" title={`${x.label}: ${x.harmonic_similarity}`} onClick={() => jump(x.start + 1.5)}>
                      <div className="rounded-t bg-copper/70 group-hover:bg-copper transition" style={{ height: `${Math.max(4, x.harmonic_similarity * 56)}px` }} />
                      <div className="text-[10px] text-ivory-mute truncate mt-0.5">{x.label}</div></button>))}</div></div>
              )}
              <div className="panel-title mt-5 mb-2">Discrepancies ({d.discrepancies.length})</div>
              {d.discrepancies.length === 0 ? <p className="note">None detected by the checks that could run. That is not proof there are none.</p> : (
                <div className="overflow-x-auto"><table className="w-full text-[13px]">
                  <thead><tr className="text-left text-[11px] uppercase tracking-wider text-ivory-mute"><th className="py-1.5 font-medium">Time</th><th className="font-medium">Type</th><th className="font-medium">Original</th><th className="font-medium">Reconstruction</th><th className="font-medium">Confidence</th><th className="font-medium">Suggested correction</th><th /></tr></thead>
                  <tbody>{d.discrepancies.map((x: any) => (
                    <tr key={x.id} className={`table-row align-top ${x.status !== "open" ? "opacity-60" : ""}`}>
                      <td className="py-2 pr-3">{x.time != null ? <button className="font-mono text-copper-soft hover:underline" onClick={() => jump(x.time)}>{fmtTime(x.time)}</button> : "–"}</td>
                      <td className="pr-3 whitespace-nowrap">{TYPE_LABEL[x.type] || x.type}</td><td className="pr-3 text-ivory-dim">{x.original}</td><td className="pr-3 text-ivory-dim">{x.reconstructed}</td>
                      <td className="pr-3 font-mono text-ivory-mute">{x.confidence != null ? `${Math.round(x.confidence * 100)}%` : "–"}</td><td className="pr-3 text-ivory-dim max-w-xs">{x.suggestion}</td>
                      <td className="whitespace-nowrap text-right">{x.status === "open" ? <>
                        <button className="btn btn-sm" disabled={report.stale && !!x.fix} title={x.fix ? "Apply this correction to the score as a new version" : "Mark as seen"} onClick={() => decide(x.id, "approve")}>{x.fix ? "Apply" : "Acknowledge"}</button>
                        <button className="btn-ghost btn-sm" onClick={() => decide(x.id, "reject")}>Reject</button></> : <StatusChip status={x.status === "approved" ? "approved" : x.status === "rejected" ? "rejected" : "info"} label={x.status} />}</td>
                    </tr>))}</tbody></table></div>
              )}
              <div className="panel-title mt-5 mb-1.5">What this report cannot tell you</div>
              <ul className="note list-disc pl-4 space-y-1">{d.limits.map((l: string, i: number) => <li key={i}>{l}</li>)}</ul>
            </>
          )}
        </Panel>
      )}
    </div>
  );
}
