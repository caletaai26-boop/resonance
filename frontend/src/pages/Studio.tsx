import { useMemo, useRef, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { AgentView, Gate, MODE_LABEL, api, fmtBytes, fmtDate } from "../api";
import { ErrorNote, Explain, Modal, StatusChip } from "../components/ui";
import { StudioCtx, useStudio, usePlayer, useStudioState } from "../studio/context";
import OverviewTab from "../studio/OverviewTab";
import StructureTab from "../studio/StructureTab";
import ScoreTab from "../studio/ScoreTab";
import LyricsTab from "../studio/LyricsTab";
import ArrangementTab from "../studio/ArrangementTab";
import ProduceTab from "../studio/ProduceTab";
import CompareTab from "../studio/CompareTab";
import ExportTab from "../studio/ExportTab";

const GATE_TAB: Record<number, string> = { 1: "", 2: "structure", 3: "score", 4: "arrangement", 5: "produce", 6: "compare", 7: "export" };

function UploadSource() {
  const s = useStudio();
  const input = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [over, setOver] = useState(false);
  async function send(f: File) {
    s.setError(null);
    setProgress(0);
    try { await api.upload(`/api/projects/${s.project.id}/source`, f, {}, setProgress); s.flash("Recording uploaded. The original is stored untouched."); await s.refresh(); }
    catch (e: any) { s.setError(e.message); } finally { setProgress(null); }
  }
  return (
    <div className="max-w-2xl mx-auto mt-10 rise">
      <div className={`panel p-10 text-center border-dashed transition ${over ? "!border-copper bg-copper/5" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files[0]; if (f) send(f); }}>
        <div className="chip-warn mb-4">Awaiting source recording</div>
        <h2 className="h-display text-2xl">Upload the recording</h2>
        <p className="note mt-2 max-w-md mx-auto">This project has no audio yet. Upload your original file: MP3, WAV, FLAC or M4A, up to {s.caps?.limits.max_upload_mb ?? 200} MB. It is kept exactly as uploaded and becomes the reference of truth for everything that follows.</p>
        <input ref={input} type="file" accept=".mp3,.wav,.flac,.m4a,audio/*" className="hidden" onChange={(e) => e.target.files?.[0] && send(e.target.files[0])} />
        <div className="mt-6">
          {progress === null ? <button className="btn-primary" onClick={() => input.current?.click()}>Choose file</button>
            : <div className="max-w-xs mx-auto"><div className="progress-track"><div className="progress-bar" style={{ width: `${progress * 100}%` }} /></div><div className="text-[12px] text-ivory-mute mt-2">Uploading… {Math.round(progress * 100)}%</div></div>}
        </div>
        <p className="text-[12px] text-ivory-mute mt-4">or drop the file here</p>
      </div>
    </div>
  );
}

function GateRow({ g, onOpen }: { g: Gate; onOpen: () => void }) {
  const icon = g.state === "approved" ? "●" : g.state === "invalidated" ? "◐" : g.state === "rejected" ? "✕" : "○";
  const tone = g.state === "approved" ? "text-sage" : g.state === "invalidated" ? "text-amber" : g.state === "rejected" ? "text-rose" : "text-ivory-mute";
  return (
    <button onClick={onOpen} className="w-full flex items-start gap-2.5 py-1.5 text-left hover:bg-ink-800/60 rounded-md px-1.5 -mx-1.5 transition">
      <span className={`${tone} text-xs mt-0.5 w-3`} aria-hidden>{icon}</span>
      <span className="flex-1 min-w-0"><span className="text-[13px] block truncate">{g.title}</span>
        <span className={`text-[11px] ${tone}`}>Gate {g.gate} · {g.state === "invalidated" ? "needs re-approval" : g.state}</span></span>
    </button>
  );
}

function LeftPanel() {
  const s = useStudio();
  const nav = useNavigate();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [edit, setEdit] = useState(false);
  const p = s.project;
  const probe = p.source?.meta?.probe;
  return (
    <aside className="border-r border-ink-700/70 bg-ink-900/50 overflow-y-auto p-4 space-y-5">
      <div>
        <Link to="/" className="text-[12px] text-ivory-mute hover:text-ivory">← Projects</Link>
        <h1 className="h-display text-xl leading-snug mt-1.5">{p.title}</h1>
        <div className="mt-1.5 flex flex-wrap gap-1.5"><span className="chip-live">{MODE_LABEL[p.mode]}</span><span className="chip-idle">{{ la: "Latin", es: "Spanish", en: "English" }[p.language]}</span></div>
      </div>
      <div>
        <div className="panel-title mb-2">Source recording</div>
        {p.source ? (
          <div className="text-[13px] space-y-0.5">
            <div className="truncate" title={p.source.filename}>{p.source.filename}</div>
            <div className="text-ivory-mute">{fmtBytes(p.source.size_bytes)}{probe && ` · ${probe.codec} · ${(probe.sample_rate / 1000).toFixed(1)} kHz · ${probe.channels === 1 ? "mono" : "stereo"}`}</div>
            <div className="text-ivory-mute font-mono text-[11px]" title={`SHA-256 ${p.source.sha256}`}>Immutable · {p.source.sha256.slice(0, 12)}…</div>
          </div>
        ) : <div className="note">Not uploaded yet.</div>}
      </div>
      <div>
        <div className="panel-title mb-1.5 flex items-center gap-2">Production stages <Explain>Seven checkpoints where the work stops for your approval. If you later change something earlier in the chain, every checkpoint that depended on it is re-opened automatically.</Explain></div>
        {s.status.gates.map((g) => <GateRow key={g.gate} g={g} onOpen={() => nav(`/p/${p.id}/${GATE_TAB[g.gate]}`)} />)}
      </div>
      <div>
        <div className="panel-title mb-1.5">Agents</div>
        <ul className="space-y-0.5">
          {s.agents.map((a) => (
            <li key={a.key}>
              <button onClick={() => s.selectAgent(a.key === s.selectedAgent ? null : a.key)}
                className={`w-full flex items-center gap-2 px-1.5 py-1.5 -mx-1.5 rounded-md text-left transition ${s.selectedAgent === a.key ? "bg-ink-800" : "hover:bg-ink-800/60"}`}>
                <span className="font-mono text-[10px] text-ivory-mute w-4 text-right">{a.number || "A"}</span>
                <span className="flex-1 text-[13px] truncate">{a.name}</span>
                {a.status === "running" || a.status === "queued" ? <span className="live-dot" /> : <span className={`w-1.5 h-1.5 rounded-full ${a.status === "succeeded" ? (a.output?.stale ? "bg-amber" : "bg-sage") : a.status === "blocked" ? "bg-amber" : a.status === "failed" ? "bg-rose" : "bg-ink-500"}`} />}
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="pt-3 border-t border-ink-700/60 space-y-1">
        <button className="btn-ghost btn-sm w-full !justify-start" onClick={() => setEdit(true)}>Project settings &amp; rights</button>
        <button className="btn-ghost btn-sm w-full !justify-start !text-rose/80 hover:!text-rose" onClick={() => setConfirmDelete(true)}>Delete project</button>
      </div>
      {confirmDelete && (
        <Modal title="Delete this project?" onClose={() => setConfirmDelete(false)}>
          <p className="note">This permanently removes the original recording, every stem, score version, render, master and report for “{p.title}”. It cannot be undone.</p>
          <div className="flex justify-end gap-2 mt-5">
            <button className="btn" onClick={() => setConfirmDelete(false)}>Keep project</button>
            <button className="btn !border-rose/60 !text-rose" onClick={async () => { try { await api.del(`/api/projects/${p.id}`); nav("/"); } catch (e: any) { s.setError(e.message); setConfirmDelete(false); } }}>Delete permanently</button>
          </div>
        </Modal>
      )}
      {edit && <ProjectSettings onClose={() => setEdit(false)} />}
    </aside>
  );
}

function ProjectSettings({ onClose }: { onClose: () => void }) {
  const s = useStudio();
  const p = s.project;
  const [f, setF] = useState({ title: p.title, description: p.description, mode: p.mode, language: p.language, retention_days: p.retention_days ?? 0, allow_external_ai: p.allow_external_ai,
    melody: p.rights?.melody || "", arrangement: p.rights?.arrangement || "", recording: p.rights?.recording || "", notes: p.rights?.notes || "" });
  const set = (k: string, v: any) => setF((x) => ({ ...x, [k]: v }));
  async function save() {
    try {
      await api.patch(`/api/projects/${p.id}`, { title: f.title, description: f.description, mode: f.mode, language: f.language, retention_days: Number(f.retention_days) || 0, allow_external_ai: f.allow_external_ai,
        rights: { melody: f.melody, arrangement: f.arrangement, recording: f.recording, notes: f.notes } });
      await s.refresh(); onClose();
    } catch (e: any) { s.setError(e.message); onClose(); }
  }
  return (
    <Modal title="Project settings" onClose={onClose} wide>
      <div className="space-y-4">
        <div><label className="label">Title</label><input className="input" value={f.title} onChange={(e) => set("title", e.target.value)} /></div>
        <div><label className="label">Notes</label><textarea className="input" rows={2} value={f.description} onChange={(e) => set("description", e.target.value)} /></div>
        <div className="grid sm:grid-cols-2 gap-3">
          <div><label className="label">Reconstruction mode</label>
            <select className="input" value={f.mode} onChange={(e) => set("mode", e.target.value)}>{Object.entries(MODE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
            {f.mode !== p.mode && <p className="text-[12px] text-amber mt-1.5">Changing mode re-opens the arrangement and every later checkpoint.</p>}</div>
          <div><label className="label">Sung language</label><select className="input" value={f.language} onChange={(e) => set("language", e.target.value)}><option value="la">Latin</option><option value="es">Spanish</option><option value="en">English</option></select></div>
        </div>
        <div className="grid sm:grid-cols-3 gap-3">
          <div><label className="label">Rights: melody</label><input className="input" value={f.melody} onChange={(e) => set("melody", e.target.value)} /></div>
          <div><label className="label">Rights: arrangement</label><input className="input" value={f.arrangement} onChange={(e) => set("arrangement", e.target.value)} /></div>
          <div><label className="label">Rights: recording</label><input className="input" value={f.recording} onChange={(e) => set("recording", e.target.value)} /></div>
        </div>
        <div className="grid sm:grid-cols-2 gap-3">
          <div><label className="label">Delete automatically after (days without changes; 0 = keep)</label><input type="number" min={0} className="input" value={f.retention_days} onChange={(e) => set("retention_days", e.target.value)} /></div>
          <label className="flex gap-3 items-start callout-info cursor-pointer mt-5"><input type="checkbox" className="mt-1 accent-[#c98a5a]" checked={f.allow_external_ai} onChange={(e) => set("allow_external_ai", e.target.checked)} />
            <span>Allow an external language model to write plain-English summaries for this project. It receives findings as text and numbers only, never audio.</span></label>
        </div>
        <p className="text-[12px] text-ivory-mute">Rights acknowledged by {p.rights?.acknowledged_by} on {p.rights?.acknowledged_at && fmtDate(p.rights.acknowledged_at)}. Not used for model training.</p>
        <div className="flex justify-end gap-2"><button className="btn" onClick={onClose}>Cancel</button><button className="btn-primary" onClick={save}>Save</button></div>
      </div>
    </Modal>
  );
}

function AgentDetail({ a }: { a: AgentView }) {
  const s = useStudio();
  const [showLog, setShowLog] = useState(false);
  const d = a.output?.data || {};
  const live = a.status === "running" || a.status === "queued";
  const list = (x: any): string[] => (Array.isArray(x) ? x.filter((v) => typeof v === "string") : []);
  return (
    <div className="space-y-3 rise">
      <div className="flex items-start justify-between gap-2">
        <div><div className="text-[11px] text-ivory-mute font-mono">AGENT {a.number || "A"}</div><h3 className="h-display text-lg leading-tight">{a.name}</h3></div>
        <StatusChip status={a.output?.stale && a.status === "succeeded" ? "stale" : a.status} />
      </div>
      <p className="note">{a.role}</p>
      {a.job && live && (
        <div><div className="flex justify-between text-[12px] text-ivory-dim mb-1"><span>{a.job.step || "Waiting in queue"}</span><span className="font-mono">{Math.round(a.job.progress * 100)}%</span></div>
          <div className="progress-track"><div className="progress-bar" style={{ width: `${a.job.progress * 100}%` }} /></div>
          <div className="text-[11px] text-ivory-mute mt-1">Progress is reported by the running job at each real step.</div></div>
      )}
      {a.status === "blocked" && a.job && <div className="callout"><div className="text-amber font-medium mb-1">Blocked</div>{a.job.error}</div>}
      {a.status === "failed" && a.job && <div className="rounded-lg border border-rose/40 bg-rose/10 px-3 py-2.5 text-[13px] text-rose"><div className="font-medium mb-1">Failed after {a.job.attempts} attempt(s)</div><div className="font-mono text-[11px] whitespace-pre-wrap break-words max-h-32 overflow-auto">{a.job.error.split("\n")[0]}</div></div>}
      {a.status === "cancelled" && a.job && <div className="callout-info">{a.job.error}</div>}
      {a.output && (
        <>
          <div><div className="panel-title mb-1">Finding{a.output.stale && " (out of date)"}</div><p className="text-[13px] leading-relaxed">{a.output.summary}</p></div>
          {list(d.warnings).concat(list(d.notes)).length > 0 && <ul className="callout space-y-1.5 list-disc pl-6">{list(d.warnings).concat(list(d.notes)).map((w, i) => <li key={i}>{w}</li>)}</ul>}
          {d.uncertainty && <div><div className="panel-title mb-1">Uncertainty</div><p className="note">{d.uncertainty}</p></div>}
          {list(d.limitations).length > 0 && <div><div className="panel-title mb-1">Limits</div><ul className="note space-y-1 list-disc pl-4">{list(d.limitations).map((w, i) => <li key={i}>{w}</li>)}</ul></div>}
          {Array.isArray(d.engines) && <div><div className="panel-title mb-1">Engines</div>{d.engines.map((e: any) => <div key={e.name} className="note mb-1.5"><span className="text-ivory">{e.name}</span> <StatusChip status={e.available ? "ready" : "needs_setup"} label={e.available ? "Available" : "Not available"} /><div>{e.reason}</div></div>)}</div>}
          <div className="text-[11px] text-ivory-mute">Produced {fmtDate(a.output.created_at)}</div>
        </>
      )}
      {!a.output && !a.job && a.key !== "director" && <p className="note">Has not run yet. {a.next_action}</p>}
      <div className="flex flex-wrap gap-2 pt-1">
        {a.key !== "director" && a.key !== "lyrics" && <button className="btn btn-sm" disabled={live} onClick={() => s.runAgent(a.key)}>{a.output || a.job ? "Run again" : "Run"}</button>}
        {a.job && a.job.log?.length > 0 && <button className="btn-ghost btn-sm" onClick={() => setShowLog((v) => !v)}>{showLog ? "Hide" : "Show"} activity log</button>}
      </div>
      {showLog && a.job && <ol className="font-mono text-[11px] text-ivory-mute space-y-0.5 max-h-48 overflow-auto bg-ink-950/60 rounded-md p-2">{a.job.log.map((l, i) => <li key={i}><span className="text-ivory-mute/60">{new Date(l.t).toLocaleTimeString()} </span>{l.msg}</li>)}</ol>}
    </div>
  );
}

function ApprovalCard() {
  const s = useStudio();
  const nav = useNavigate();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const g = s.status.gates.find((x) => x.state !== "approved");
  if (!s.status.has_source) return null;
  if (!g) return <div className="rounded-lg border border-sage/40 bg-sage/10 px-3 py-3 text-[13px]"><div className="text-sage font-medium">All checkpoints approved</div><div className="note mt-1">The final master is authorised for export.</div></div>;
  async function decide(decision: "approved" | "rejected") {
    setBusy(true);
    try { await api.post(`/api/projects/${s.project.id}/gates/${g!.gate}`, { decision, comment }); setComment(""); s.flash(`Gate ${g!.gate} ${decision}.`); await s.refresh(); }
    catch (e: any) { s.setError(e.message); } finally { setBusy(false); }
  }
  return (
    <div className="rounded-lg border border-copper/40 bg-copper/5 p-3 space-y-2.5">
      <div className="flex items-center justify-between"><span className="panel-title !text-copper-soft">Your approval · Gate {g.gate} of 7</span>{g.state !== "pending" && <StatusChip status={g.state} />}</div>
      <div className="text-sm font-medium">{g.title}</div>
      <p className="note">{g.explain}</p>
      {g.state === "invalidated" && g.comment && <p className="text-[12px] text-amber">Re-opened: {g.comment}</p>}
      <input className="input !py-1.5 !text-[13px]" placeholder="Optional note for the record" value={comment} onChange={(e) => setComment(e.target.value)} />
      <div className="flex gap-2">
        <button className="btn-primary btn-sm flex-1" disabled={busy || s.working} onClick={() => decide("approved")}>Approve</button>
        <button className="btn btn-sm" disabled={busy} onClick={() => decide("rejected")}>Reject</button>
        <button className="btn-ghost btn-sm" onClick={() => nav(`/p/${s.project.id}/${GATE_TAB[g.gate]}`)}>Review →</button>
      </div>
    </div>
  );
}

function RightPanel() {
  const s = useStudio();
  const [narr, setNarr] = useState<any>(null);
  const sel = s.selectedAgent ? s.agent(s.selectedAgent) : null;
  const live = s.agents.filter((a) => a.key !== "director" && (a.status === "running" || a.status === "queued"));
  return (
    <aside className="border-l border-ink-700/70 bg-ink-900/50 overflow-y-auto p-4 space-y-5">
      <div>
        <div className="panel-title mb-1.5 flex items-center gap-2">Production Director {s.working && <span className="live-dot" />}</div>
        <div className="h-display text-lg leading-snug">{s.status.stage}</div>
        <p className="text-[13px] text-ivory-dim mt-1 leading-relaxed">{s.status.next_action}</p>
        {narr?.text && <p className="callout-info mt-2">{narr.text}<span className="block text-[11px] text-ivory-mute mt-1">Written by {narr.source.replace("llm:", "")} from the findings above.</span></p>}
        {narr && !narr.text && <p className="text-[12px] text-ivory-mute mt-2">{narr.reason}</p>}
        {s.project.allow_external_ai && !narr && <button className="btn-ghost btn-sm mt-1.5 !px-0" onClick={async () => setNarr((await api.get(`/api/projects/${s.project.id}/status?narrate=1`)).narration)}>Explain this in more detail</button>}
      </div>
      {live.length > 0 && (
        <div className="space-y-2">
          {live.map((a) => (
            <button key={a.key} onClick={() => s.selectAgent(a.key)} className="w-full text-left">
              <div className="flex justify-between text-[12px]"><span>{a.name}</span><span className="text-ivory-mute">{a.status === "queued" ? "queued" : `${Math.round((a.job?.progress || 0) * 100)}%`}</span></div>
              <div className="text-[11px] text-ivory-mute truncate">{a.job?.step}</div>
              <div className="progress-track mt-1"><div className="progress-bar" style={{ width: `${(a.job?.progress || 0) * 100}%` }} /></div>
            </button>
          ))}
        </div>
      )}
      {s.status.blockers.length > 0 && (
        <div><div className="panel-title mb-1.5">Needs attention</div>
          {s.status.blockers.map((b, i) => <div key={i} className="callout mb-2"><span className="text-amber font-medium">{b.agent}</span> is {b.status}. {b.message.slice(0, 220)}{b.message.length > 220 && "…"}</div>)}</div>
      )}
      <ApprovalCard />
      {sel ? <div className="pt-4 border-t border-ink-700/60"><AgentDetail a={sel} /></div>
        : <p className="note pt-4 border-t border-ink-700/60">Select an agent on the left to see what it found, the evidence, and what it is unsure about.</p>}
    </aside>
  );
}

export default function Studio() {
  const { id = "" } = useParams();
  const st = useStudioState(id);
  const [selectedAgent, selectAgent] = useState<string | null>(null);
  const working = st.agents.find((a) => a.key === "forensics")?.output?.data?.artifacts?.working;
  const src = st.project?.source ? (working ? `/api/assets/${working}/file` : st.project.source.url) : null;
  const player = usePlayer(src);
  const value = useMemo(() => (st.project && st.project.status ? {
    project: st.project, status: st.project.status, agents: st.agents, caps: st.caps, jobs: st.jobs, refresh: st.refresh, version: st.version,
    runAgent: st.runAgent, runPipeline: st.runPipeline, error: st.error, setError: st.setError, flash: st.flash, notice: st.notice, player,
    working: st.agents.some((a) => a.status === "running" || a.status === "queued"), agent: (k: string) => st.agents.find((a) => a.key === k), selectedAgent, selectAgent,
  } : null), [st, player, selectedAgent]);

  if (st.missing) return <div className="p-10 text-center"><h2 className="h-display text-2xl">Project not found</h2><Link className="btn mt-4" to="/">Back to projects</Link></div>;
  if (!value) return <div className="p-10 text-center text-ivory-mute text-sm">Opening studio…</div>;
  const p = value.project;
  const applicable = new Set(value.status.gates.map((g) => g.gate));
  const tabs = [["", "Recording"], ...(p.mode === "restore" ? [] : [["structure", "Structure"], ["score", "Score"], ["lyrics", "Lyrics"], ["arrangement", "Arrangement"]]),
    ["produce", p.mode === "restore" ? "Restore & master" : "Render & mix"], ...(applicable.has(6) ? [["compare", "Compare & fidelity"]] : [["compare", "Compare"]]), ["export", "Export"]];
  return (
    <StudioCtx.Provider value={value}>
      <div className="grid grid-cols-1 lg:grid-cols-[250px_minmax(0,1fr)] xl:grid-cols-[250px_minmax(0,1fr)_330px] xl:h-[calc(100vh-3.5rem)]">
        <LeftPanel />
        <section className="xl:overflow-y-auto min-w-0">
          <div className="sticky top-0 z-20 bg-ink-950/90 backdrop-blur border-b border-ink-700/70 px-5 flex gap-1 overflow-x-auto">
            {tabs.map(([path, label]) => <NavLink key={path} end={path === ""} to={`/p/${p.id}/${path}`} className={({ isActive }) => `tab ${isActive ? "tab-active" : ""}`}>{label}</NavLink>)}
          </div>
          <div className="p-5 space-y-4">
            <ErrorNote error={value.error} onClose={() => value.setError(null)} />
            {value.notice && <div className="rounded-lg border border-sage/40 bg-sage/10 px-3 py-2 text-[13px] text-sage rise" role="status">{value.notice}</div>}
            {!p.source ? <UploadSource /> : (
              <Routes>
                <Route index element={<OverviewTab />} />
                <Route path="structure" element={<StructureTab />} />
                <Route path="score" element={<ScoreTab />} />
                <Route path="lyrics" element={<LyricsTab />} />
                <Route path="arrangement" element={<ArrangementTab />} />
                <Route path="produce" element={<ProduceTab />} />
                <Route path="compare" element={<CompareTab />} />
                <Route path="export" element={<ExportTab />} />
                <Route path="*" element={<Navigate to="" replace />} />
              </Routes>
            )}
          </div>
        </section>
        <div className="lg:col-span-2 xl:col-span-1 xl:overflow-hidden border-t xl:border-t-0 border-ink-700/70"><div className="h-full grid"><RightPanel /></div></div>
      </div>
    </StudioCtx.Provider>
  );
}
